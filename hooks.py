"""Audit completed MindRoom responses and request one targeted correction."""

from __future__ import annotations

import asyncio

from mindroom.hooks import (
    AfterResponseContext,
    CancelledResponseContext,
    MessageEnrichContext,
    MessageEnvelope,
    SenderKind,
    ToolAfterCallContext,
    hook,
)
from mindroom.judgment.evaluator import create_judgment_evaluator

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


def _eligible(envelope: MessageEnvelope, settings: AuditSettings) -> bool:
    return (
        envelope.agent_name in settings.agents
        and not envelope.hook_source
        and envelope.message_received_depth == 0
        and envelope.origin.requester_kind == SenderKind.USER
        and not envelope.attachment_ids
        and bool(envelope.body.strip())
    )


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
    evidence = evidence_text(envelope.body, ctx.result.response_text, capture.tools, settings.source_tools)
    checks = [check for check in settings.checks if not check.requires_source_tools or settings.source_tools]
    requests = [(check, request_for(check, evidence)) for check in checks]
    if not requests or any(not request.complete for _, request in requests):
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
    text = (
        f"@{envelope.agent_name} — response audit flagged the following for review:\n"
        + "\n".join(f"- {check.id}: {check.feedback}" for check in findings)
        + "\nPlease verify these findings and correct your previous answer where needed."
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
