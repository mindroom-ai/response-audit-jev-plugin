"""Behavioral contracts for post-response audits through public MindRoom hooks."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.hooks import (
    AfterResponseContext,
    CancelledResponseContext,
    CancelledResponseInfo,
    HookRegistry,
    HookRegistryState,
    MessageEnrichContext,
    MessageEnvelope,
    ResponseResult,
    SenderKind,
    ToolAfterCallContext,
    TurnIntent,
    TurnOrigin,
    TurnTrust,
)
from mindroom.judgment.answers import JudgmentResult
from mindroom.message_target import MessageTarget
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("audit_plugin", ROOT / "hooks.py", submodule_search_locations=[str(ROOT)])
assert spec and spec.loader
hooks = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hooks
spec.loader.exec_module(hooks)


@pytest.fixture
def turn(tmp_path, monkeypatch):
    registry = HookRegistry.empty()
    settings = {"agents": ["research"], "source_tools": ["search_data"]}
    origin = TurnOrigin(
        transport_sender_id="@human:example.org",
        requester_id="@human:example.org",
        sender_entity_name=None,
        requester_entity_name=None,
        sender_kind=SenderKind.USER,
        requester_kind=SenderKind.USER,
        intent=TurnIntent.USER_MESSAGE,
        source_kind="message",
        trust=TurnTrust.EXTERNAL,
    )
    envelope = MessageEnvelope(
        source_event_id="$request",
        target=MessageTarget.resolve("!room:example.org", "$thread", "$request"),
        body="Look up the current figure and cite the source.",
        attachment_ids=(),
        mentioned_agents=("research",),
        agent_name="research",
        origin=origin,
    )
    common = dict(
        plugin_name="response-audit-jev",
        settings=settings,
        config=Config.model_validate(
            {"agents": {"research": {"display_name": "Research"}, "other": {"display_name": "Other"}}}
        ),
        runtime_paths=resolve_runtime_paths(
            config_path=tmp_path / "config.yaml", storage_path=tmp_path / "data", process_env={}
        ),
        logger=MagicMock(),
        correlation_id="turn-one",
        message_sender=AsyncMock(return_value="$feedback"),
        _hook_registry_state=HookRegistryState(registry),
        _hook_registry_snapshot=registry,
    )
    start = MessageEnrichContext(
        **common,
        event_name="message:enrich",
        envelope=envelope,
        target_entity_name="research",
        target_member_names=None,
    )
    end = AfterResponseContext(
        **common,
        event_name="message:after_response",
        result=ResponseResult("The value is 42.", "$answer", "send", "ai", envelope),
    )
    calls = []

    async def evaluate(request):
        calls.append(json.loads(request.body))
        return JudgmentResult(True, 0.99, None, "test", 1, 10, 1, request.state_bytes)

    factory = MagicMock(return_value=evaluate)
    monkeypatch.setattr(hooks, "create_judgment_evaluator", factory)
    return start, end, calls, factory


def tool(start, **kwargs):
    fields = dict(
        tool_name="search_data",
        arguments={"query": "current figure"},
        result="Official report: value 42 (https://data.example.org/report)",
        error=None,
        blocked=False,
        duration_ms=20,
        agent_name="research",
        room_id=start.envelope.room_id,
        thread_id="$thread",
        requester_id=start.envelope.requester_id,
        session_id="session",
        settings=start.settings,
        config=start.config,
        runtime_paths=start.runtime_paths,
        correlation_id=start.correlation_id,
        plugin_name=start.plugin_name,
    )
    return ToolAfterCallContext(**(fields | kwargs))


@pytest.mark.asyncio
async def test_records_evidence_and_posts_one_combined_same_thread_followup(turn):
    start, end, calls, factory = turn
    await hooks.begin_audit(start)
    await hooks.record_tool(tool(start))
    await hooks.audit_response(end)
    assert len(calls) == 2
    evidence = json.loads(calls[0]["state"]["conversation"][0]["text"])
    assert evidence["request"] == start.envelope.body
    assert evidence["response"] == end.result.response_text
    assert evidence["tools"][0]["name"] == "search_data"
    assert evidence["tools"][0]["status"] == "succeeded"
    assert "Official report" in evidence["tools"][0]["result"]
    end.message_sender.assert_awaited_once()
    sent = end.message_sender.call_args
    assert sent.args[0] == start.envelope.room_id
    assert "@research" in sent.args[1]
    assert "citations" in sent.args[1] and "source_use" in sent.args[1]
    assert sent.args[2] == "$thread"
    assert sent.kwargs["trigger_dispatch"] is True
    assert factory.call_args.args[0].provider == "typesafe"
    assert factory.call_args.args[0].threshold == 0.9


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["disabled", "other", "team", "hook", "attachment", "non_ai", "empty", "missing_capture"]
)
async def test_ineligible_responses_do_not_judge_or_send(turn, reason):
    start, end, calls, _ = turn
    if reason == "disabled":
        start.settings["agents"] = []
    if reason == "other":
        start.envelope = replace(start.envelope, agent_name="other")
    if reason == "team":
        start.target_member_names = ("research",)
    if reason == "hook":
        start.envelope = replace(start.envelope, hook_source="response-audit-jev:message:after_response")
    if reason == "attachment":
        start.envelope = replace(start.envelope, attachment_ids=("image",))
    end.result = replace(end.result, envelope=start.envelope)
    if reason == "non_ai":
        end.result = replace(end.result, response_kind="command")
    if reason == "empty":
        end.result = replace(end.result, response_text="")
    if reason != "missing_capture":
        await hooks.begin_audit(start)
    await hooks.audit_response(end)
    assert calls == []
    end.message_sender.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,failure", [(False, None), (None, None), (None, "timeout"), (True, "http_error")])
async def test_only_successful_positive_issue_judgments_post(turn, decision, failure):
    start, end, _, factory = turn

    async def evaluate(request):
        return JudgmentResult(decision, None, failure, "test", 0, None, None, request.state_bytes)

    factory.return_value = evaluate
    await hooks.begin_audit(start)
    await hooks.audit_response(end)
    end.message_sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_source_tool_mapping_skips_source_check(turn):
    start, end, calls, _ = turn
    start.settings["source_tools"] = []
    await hooks.begin_audit(start)
    await hooks.audit_response(end)
    assert [c["question"]["id"] for c in calls] == ["citations"]


@pytest.mark.asyncio
async def test_custom_check_and_llm_backend(turn):
    start, end, calls, factory = turn
    start.settings.update(
        judgment={"provider": "llm", "model": "cheap"},
        checks=[
            {
                "id": "units",
                "instructions": "Are required measurement units missing?",
                "feedback": "Add the measurement units.",
            }
        ],
    )
    start.config = end.config = Config.model_validate(
        {
            "agents": {"research": {"display_name": "Research"}},
            "models": {"cheap": {"provider": "synthetic", "id": "test"}},
        }
    )
    await hooks.begin_audit(start)
    await hooks.audit_response(end)
    assert len(calls) == 1 and calls[0]["question"]["id"] == "units"
    assert factory.call_args.args[0].provider == "llm"
    assert "Add the measurement units." in end.message_sender.call_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["oversized", "sensitive", "unsupported", "too_many"])
async def test_incomplete_evidence_is_not_sent_to_judge(turn, change):
    start, end, calls, _ = turn
    await hooks.begin_audit(start)
    if change == "oversized":
        end.result = replace(end.result, response_text="a" * 17000)
    if change == "sensitive":
        await hooks.record_tool(tool(start, result="Authorization: Bearer sk-" + "a" * 48))
    if change == "unsupported":
        await hooks.record_tool(tool(start, result=object()))
    if change == "too_many":
        for _ in range(33):
            await hooks.record_tool(tool(start))
    await hooks.audit_response(end)
    assert calls == []
    end.message_sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_agent_and_turn_tools_do_not_leak(turn):
    start, end, calls, _ = turn
    await hooks.begin_audit(start)
    await hooks.record_tool(tool(start, agent_name="other", result="OTHER AGENT"))
    await hooks.record_tool(tool(start, correlation_id="another", result="OTHER TURN"))
    await hooks.audit_response(end)
    assert json.loads(calls[0]["state"]["conversation"][0]["text"])["tools"] == []


@pytest.mark.asyncio
async def test_failed_and_blocked_tools_are_not_recorded_as_success(turn):
    start, end, calls, _ = turn
    await hooks.begin_audit(start)
    await hooks.record_tool(tool(start, error=ValueError("private error"), result=None))
    await hooks.record_tool(tool(start, blocked=True, result="declined"))
    await hooks.audit_response(end)
    tools = json.loads(calls[0]["state"]["conversation"][0]["text"])["tools"]
    assert [t["status"] for t in tools] == ["failed", "blocked"]
    assert "private error" not in json.dumps(calls)


@pytest.mark.asyncio
async def test_durable_claim_blocks_replayed_response_even_with_new_capture(turn):
    start, end, calls, _ = turn
    await hooks.begin_audit(start)
    await hooks.audit_response(end)
    await hooks.begin_audit(start)
    await hooks.audit_response(end)
    assert len(calls) == 2
    end.message_sender.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_response_discards_capture(turn):
    start, end, calls, _ = turn
    await hooks.begin_audit(start)
    cancelled = CancelledResponseContext(
        event_name="message:cancelled",
        plugin_name=start.plugin_name,
        settings=start.settings,
        config=start.config,
        runtime_paths=start.runtime_paths,
        logger=start.logger,
        correlation_id=start.correlation_id,
        info=CancelledResponseInfo(start.envelope),
    )
    await hooks.cancel_audit(cancelled)
    await hooks.audit_response(end)
    assert calls == []


@pytest.mark.asyncio
async def test_inactive_hook_does_not_post_after_judgment(turn):
    start, end, _, factory = turn

    async def evaluate(request):
        end._hook_registry_state.registry = HookRegistry.empty()
        return JudgmentResult(True, 0.99, None, "test", 0, None, None, request.state_bytes)

    factory.return_value = evaluate
    await hooks.begin_audit(start)
    await hooks.audit_response(end)
    end.message_sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_backend_exception_and_cancellation_never_post(turn):
    start, end, _, factory = turn
    factory.return_value = AsyncMock(side_effect=RuntimeError("private provider error"))
    await hooks.begin_audit(start)
    await hooks.audit_response(end)
    end.message_sender.assert_not_awaited()
    end.result = replace(end.result, response_event_id="$answer2")
    factory.return_value = AsyncMock(side_effect=asyncio.CancelledError)
    await hooks.begin_audit(start)
    with pytest.raises(asyncio.CancelledError):
        await hooks.audit_response(end)
    end.message_sender.assert_not_awaited()


@pytest.mark.parametrize(
    "settings",
    [
        {"unknown": True},
        {"checks": []},
        {"agents": [""]},
        {"checks": [{"id": "x", "instructions": "Check", "feedback": "Fix"}] * 2},
    ],
)
def test_settings_reject_ambiguous_values(settings):
    with pytest.raises(ValidationError):
        hooks.AuditSettings.model_validate(settings)


@pytest.mark.asyncio
async def test_real_typesafe_adapter_uses_audit_questions_and_probability(turn, monkeypatch):
    """Exercise the shared backend contract without a real network request."""
    import httpx
    from mindroom.judgment import evaluator
    from mindroom.judgment.client import PINNED_MODEL, SystemOneClient

    start, end, _, _ = turn
    wire_requests = []

    def respond(request):
        payload = json.loads(request.content)
        wire_requests.append(payload)
        check_id = next(iter(payload["questions"]))
        assert request.headers["Authorization"] == "Bearer synthetic-key"
        return httpx.Response(
            200,
            json={
                "model": PINNED_MODEL,
                "answers": {check_id: {"type": "noul", "noul": 0.97 if check_id == "citations" else 0.1}},
                "usage": {"input_tokens": 100, "output_tokens": 10},
            },
        )

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(evaluator, "SystemOneClient", lambda **kwargs: SystemOneClient(**kwargs, transport=transport))
    monkeypatch.setattr(hooks, "create_judgment_evaluator", evaluator.create_judgment_evaluator)
    paths = replace(start.runtime_paths, process_env={"TYPESAFE_API_KEY": "synthetic-key"})
    start.runtime_paths = end.runtime_paths = paths
    await hooks.begin_audit(start)
    await hooks.audit_response(end)
    assert len(wire_requests) == 2
    sent = end.message_sender.call_args.args[1]
    assert "citations:" in sent and "source_use:" not in sent


def test_plugin_loads_through_mindroom_contract():
    """Manifest and relative modules load through the actual runtime checker."""
    from mindroom.plugin_check import check_plugin

    result = check_plugin(ROOT)
    assert result.name == "response-audit-jev"
    assert result.hook_names == (
        "audit-response",
        "begin-response-audit",
        "discard-response-audit",
        "record-audit-tool",
    )


@pytest.mark.asyncio
async def test_tool_evidence_is_snapshotted_before_caller_mutation(turn):
    start, end, calls, _ = turn
    result = {"value": 42}
    await hooks.begin_audit(start)
    await hooks.record_tool(tool(start, result=result))
    result["value"] = 99
    await hooks.audit_response(end)
    assert json.loads(calls[0]["state"]["conversation"][0]["text"])["tools"][0]["result"] == {"value": 42}


@pytest.mark.asyncio
async def test_missing_credentials_stays_quiet(turn):
    start, end, _, factory = turn
    factory.return_value = None
    await hooks.begin_audit(start)
    await hooks.audit_response(end)
    end.message_sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_delivery_failure_is_not_retried(turn):
    start, end, calls, _ = turn
    end.message_sender.return_value = None
    await hooks.begin_audit(start)
    await hooks.audit_response(end)
    await hooks.begin_audit(start)
    await hooks.audit_response(end)
    end.message_sender.assert_awaited_once()
    assert len(calls) == 2
