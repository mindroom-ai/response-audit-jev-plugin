"""Audit completed MindRoom responses and request one targeted correction."""

from __future__ import annotations

import asyncio
from pathlib import Path

from mindroom.attachments.storage import resolve_attachments
from mindroom.hooks import (
    AfterResponseContext,
    CancelledResponseContext,
    MessageEnrichContext,
    MessageEnvelope,
    ToolAfterCallContext,
    hook,
)
from mindroom.judgment.evaluator import create_judgment_evaluator
from mindroom.tool_system.events import is_visible_tool_marker_line

from .audit import AuditSettings, evidence_text, request_for
from .state import Capture, CaptureKey, captures, claim_attempt, prune


def _message_key(
    ctx: MessageEnrichContext | AfterResponseContext | CancelledResponseContext, envelope: MessageEnvelope
) -> CaptureKey:
    return (
        str(ctx.runtime_paths.storage_root),
        ctx.plugin_name,
        envelope.agent_name,
        envelope.room_id,
        ctx.correlation_id,
    )


# Enough of the audited reply to name it when newer replies follow it in the thread.
_OPENING_CHARS = 80


def _eligible(envelope: MessageEnvelope, settings: AuditSettings) -> bool:
    # A person's own request only: never correction turns, other agents, automations, or schedules and webhooks
    # that mention the agent; MindRoom treats a schedule the router hands to an agent as the creator's request.
    return (
        envelope.agent_name in settings.agents
        and envelope.origin.may_answer_interactive_prompt
        and bool(envelope.body.strip())
    )


def _shared_file_names(storage_root: Path, room_id: str, attachment_ids: tuple[str, ...]) -> list[str]:
    # The model reads shared files without tool calls; voice audio is already transcribed into the request.
    # Envelope IDs can fall back to unscoped ones, so another room's file names never reach the judge.
    records = resolve_attachments(storage_root, list(attachment_ids))
    return [
        record.filename or record.kind for record in records if record.kind != "audio" and record.room_id == room_id
    ]


def _reply_opening(response: str) -> str:
    # Without "@", a mention in the quote cannot tag another agent or person in the follow-up.
    text = " ".join(line for line in response.splitlines() if not is_visible_tool_marker_line(line))
    text = " ".join(text.replace("@", "").split())
    return text if len(text) <= _OPENING_CHARS else f"{text[:_OPENING_CHARS]}…"


@hook("message:enrich", name="begin-response-audit", timeout_ms=1000)
async def begin_audit(ctx: MessageEnrichContext) -> None:
    """Observe turn admission without adding anything to the agent's prompt."""
    settings = AuditSettings.model_validate(ctx.settings)
    if (
        ctx.target_member_names is not None
        or ctx.envelope.agent_name not in ctx.config.agents
        or not _eligible(ctx.envelope, settings)
    ):
        return
    prune()
    key = _message_key(ctx, ctx.envelope)
    captures.setdefault(key, Capture())
    prune()


@hook("tool:after_call", name="record-audit-tool", timeout_ms=300)
async def record_tool(ctx: ToolAfterCallContext) -> None:
    """Capture actual completed calls; model prose is never proof a tool ran."""
    if ctx.runtime_paths is None or ctx.room_id is None:
        return
    prune()
    key = (str(ctx.runtime_paths.storage_root), ctx.plugin_name, ctx.agent_name, ctx.room_id, ctx.correlation_id)
    capture = captures.get(key)
    if capture is not None:
        capture.record(ctx.tool_name, ctx.arguments, ctx.result, failed=ctx.error is not None, blocked=ctx.blocked)


@hook("message:cancelled", name="discard-response-audit", timeout_ms=1000)
async def cancel_audit(ctx: CancelledResponseContext) -> None:
    """Incomplete or cancelled replies never trigger a corrective message."""
    captures.pop(_message_key(ctx, ctx.info.envelope), None)


@hook("message:after_response", name="audit-response", timeout_ms=30000)
async def audit_response(ctx: AfterResponseContext) -> None:
    """Judge delivered text and post one combined, agent-addressed follow-up."""
    prune()
    envelope = ctx.result.envelope
    capture = captures.pop(_message_key(ctx, envelope), None)
    settings = AuditSettings.model_validate(ctx.settings)
    if (
        capture is None
        or not capture.complete
        or not _eligible(envelope, settings)
        or ctx.result.response_kind != "ai"
        or not ctx.result.response_text.strip()
        or not ctx.result.response_event_id
        or not ctx.is_active()
    ):
        return
    shared_files = (
        await asyncio.to_thread(
            _shared_file_names, ctx.runtime_paths.storage_root, envelope.room_id, envelope.attachment_ids
        )
        if envelope.attachment_ids
        else []
    )
    evidence = evidence_text(envelope.body, ctx.result.response_text, capture.tools, shared_files)
    requests = [(check, request_for(check, evidence)) for check in settings.checks]
    if any(not request.complete for _, request in requests):
        ctx.logger.info("response_audit_skipped", reason="incomplete_evidence")
        return
    if not await asyncio.to_thread(
        claim_attempt, ctx.state_root, envelope.agent_name, envelope.room_id, ctx.result.response_event_id
    ):
        return
    findings = []
    for check, request in requests:
        evaluate = create_judgment_evaluator(
            settings.judgment,
            ctx.config,
            ctx.runtime_paths,
            owner=f"{ctx.runtime_paths.storage_root}:{envelope.agent_name}",
            question_id=check.id,
        )
        if evaluate is None:
            return
        try:
            result = await evaluate(request)
        except Exception as error:
            # Provider exception text can contain private request data or credentials.
            ctx.logger.warning("response_audit_check_failed", check=check.id, error_type=type(error).__name__)
            continue
        if result.failure is None and result.decision is True:
            findings.append(check)
    if not findings or not ctx.is_active():
        return
    # Naming the audited reply in the closing request keeps agents from correcting a newer reply instead.
    opening = _reply_opening(ctx.result.response_text)
    text = (
        f"@{envelope.agent_name} — response audit flagged the following for review:\n"
        + "\n".join(f"- {check.id}: {check.feedback}" for check in findings)
        + f'\nPlease verify these findings in your reply that starts "{opening}" and correct that reply where needed; '
        "later replies are not part of this audit."
    )
    await ctx.send_message(
        envelope.room_id,
        text,
        thread_id=envelope.target.resolved_thread_id,
        extra_content={
            "com.mindroom.response_audit": {
                "response_event_id": ctx.result.response_event_id,
                "checks": [check.id for check in findings],
            }
        },
        trigger_dispatch=True,
    )
