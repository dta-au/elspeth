"""Re-prove original provider checkpoints in the current terminal SQL owner.

Source coordinates describe DATA. Only the caller's current locked COMPOSE
transaction authorizes publication; an old provider source never does.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, Row, select

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.provider_audit_source import ProviderAuditSource, provider_attempt_audit_drafts_for_source
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.audit_checkpoint import uncheckpointed_envelopes
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import _require_current_continuation_running
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_segment_attempts_table as creators
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.time_normalization import restore_utc


def continuation_provider_audit_drafts_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, calls: tuple[ComposerLLMCall, ...]
) -> tuple[AuditMessageDraft, ...]:
    """Keep current-source drafts; omit only fully proved older checkpoints.

    The terminal owner calls this before inserting an assistant, under its
    actual current session write lock. Every proof is on that same connection;
    no call-ID set or pre-read receipt grants an omission across an await.
    """
    if type(running) is not ComposerOperationRunning or type(calls) is not tuple:
        raise AuditIntegrityError("Provider checkpoint source lost its actual running owner or call tuple")
    if any(type(call) is not ComposerLLMCall or call.call_id is None for call in calls):
        raise AuditIntegrityError("Provider checkpoint source requires complete original physical calls")
    if len({call.call_id for call in calls}) != len(calls):
        raise AuditIntegrityError("Provider checkpoint source repeats a physical identity")
    _require_current_continuation_running(conn, running, allow_expired_segment=True)
    sid = str(running.claim.session_id)
    principal = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == sid)).scalar_one()
    current_fence = running.session_operation_context.fence
    retained: list[AuditMessageDraft] = []
    for call in calls:
        attempt = conn.execute(select(physical).where(physical.c.attempt_id == call.call_id)).one_or_none()
        if (
            attempt is None
            or attempt.session_id != sid
            or attempt.identity_id != principal
            or attempt.source != "composer"
            or attempt.run_id is not None
            or restore_utc(attempt.started_at) != call.started_at
        ):
            raise AuditIntegrityError("Provider checkpoint source changed its physical session, principal or admission")
        creator = conn.execute(
            select(creators).where(
                creators.c.session_id == sid,
                creators.c.operation_id == running.claim.operation_id,
                creators.c.session_operation_id == attempt.operation_id,
                creators.c.session_operation_epoch == attempt.operation_epoch,
                creators.c.session_operation_lease_token == attempt.lease_token,
            )
        ).one_or_none()
        if creator is None:
            raise AuditIntegrityError("Provider checkpoint source lost its original logical-operation creator")
        current_source = (attempt.operation_id, attempt.operation_epoch, attempt.lease_token) == (
            current_fence.operation_id,
            current_fence.operation_epoch,
            current_fence.lease_token,
        )
        if current_source:
            if (
                (creator.sequence, creator.generation, creator.claim_attempt)
                != (running.claim.segment_sequence, running.claim.ownership_generation, running.claim.attempt)
                or creator.revoked_at is not None
                or creator.outcome is not None
            ):
                raise AuditIntegrityError("Provider checkpoint source differs from the actual current claim")
        elif (
            creator.sequence > running.claim.segment_sequence
            or creator.generation >= running.claim.ownership_generation
            or creator.revoked_at is None
            or creator.outcome not in ("yielded", "interrupted")
        ):
            raise AuditIntegrityError("Provider checkpoint source has no released prior creator")
        source = ProviderAuditSource(attempt.session_id, attempt.operation_id, attempt.operation_epoch)
        drafts = provider_attempt_audit_drafts_for_source(call, source)
        public = drafts[0]
        if public.tool_calls is None:
            raise AuditIntegrityError("Provider checkpoint source lost its complete public pair")
        public_rows = conn.execute(
            select(messages).where(
                messages.c.session_id == sid,
                messages.c.tool_calls[0]["_kind"].as_string() == "llm_call_audit",
                messages.c.tool_calls[0]["call"]["call_id"].as_string() == call.call_id,
            )
        ).all()
        if attempt.settled_at is None:
            if not current_source or attempt.ledger_entry_id is not None or public_rows:
                raise AuditIntegrityError("Provider checkpoint source cannot regenerate an unresolved original call")
            # Only a new current-source call can acquire its first checkpoint.
            # Partial old private evidence is never treated as a fresh call.
            for draft in drafts[1:]:
                if draft.tool_calls is None:
                    raise AuditIntegrityError("Provider checkpoint private cohort lost its complete envelope")
                evidence_id = draft.tool_calls[0]["evidence_id"]
                existing = conn.execute(
                    select(messages.c.id).where(
                        messages.c.session_id == sid,
                        messages.c.tool_calls[0]["evidence_id"].as_string() == evidence_id,
                    )
                ).all()
                if existing:
                    raise AuditIntegrityError("Provider checkpoint private cohort is only partially settled")
            retained.extend(drafts)
            continue
        # This existing verifier checks settled ledger usage and the complete
        # public call/companion pair in one unique durable row. Keep it intact.
        if uncheckpointed_envelopes(conn, session_id=sid, envelopes=public.tool_calls):
            raise AuditIntegrityError("Provider checkpoint companion has no original committed row")
        if len(public_rows) != 1:
            raise AuditIntegrityError("Provider checkpoint companion lost its unique original row")
        _require_original_draft(public_rows[0], public, private=False)
        for draft in drafts[1:]:
            if draft.tool_calls is None or len(draft.tool_calls) != 1:
                raise AuditIntegrityError("Provider checkpoint private cohort lost its complete envelope")
            evidence_id = draft.tool_calls[0]["evidence_id"]
            private_rows = conn.execute(
                select(messages).where(
                    messages.c.session_id == sid,
                    messages.c.tool_calls[0]["evidence_id"].as_string() == evidence_id,
                )
            ).all()
            if len(private_rows) != 1:
                raise AuditIntegrityError("Provider checkpoint private cohort lost its unique original row")
            _require_original_draft(private_rows[0], draft, private=True)
        if current_source:
            # Current-source companions retain the ordinary current-context
            # validation and exact dedupe path. Old evidence stays unchanged.
            retained.extend(drafts)
    return tuple(retained)


def _require_original_draft(row: Row[Any], draft: AuditMessageDraft, *, private: bool) -> None:
    if (
        row.role != draft.role
        or row.writer_principal != "compose_loop"
        or row.content != draft.content
        or row.composition_state_id != draft.effective_composition_state_id(None)
        or row.tool_call_id != draft.tool_call_id
        or row.parent_assistant_id != draft.parent_assistant_id
        or deep_thaw(row.tool_calls) != deep_thaw(draft.tool_calls)
    ):
        kind = "private cohort" if private else "companion"
        raise AuditIntegrityError(f"Provider checkpoint {kind} differs from its complete original source")
