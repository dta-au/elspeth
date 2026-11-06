"""Bounded cold join from a checkpoint paid terminal to its public physical audit."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Connection, LargeBinary, Text, case, cast, func, select

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.provider_audit_source import ProviderAuditSource
from elspeth.web.sessions.continuation_audit_membership import public_audit_membership_predicate
from elspeth.web.sessions.continuation_cost import _closed_call
from elspeth.web.sessions.continuation_paid_incomplete import PaidIncompleteReceipt
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowReceipt, encode_paid_overflow_cost_amount
from elspeth.web.sessions.continuation_retained_capacity import ProviderTerminalHoldSource
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.time_normalization import restore_utc

if TYPE_CHECKING:
    from elspeth.web.sessions.continuation_checkpoint_early_return import VerifiedEarlyCompleteReturn


def require_checkpoint_paid_public_audit_on_connection(
    conn: Connection,
    *,
    source: ProviderTerminalHoldSource,
    attempt_id: str,
    requested_model: str,
    physical_started_at: datetime,
    physical_settled_at: datetime,
    response_finished_at: datetime,
    receipt: PaidOverflowReceipt | PaidIncompleteReceipt,
    early_owed: VerifiedEarlyCompleteReturn | None = None,
) -> None:
    """Reprove one bounded original sidecar before granting a cold terminal."""
    try:
        canonical_attempt = str(UUID(attempt_id))
    except (TypeError, ValueError) as exc:
        raise AuditIntegrityError("checkpoint paid audit lost its canonical physical identity") from exc
    if canonical_attempt != attempt_id or conn.dialect.name not in ("sqlite", "postgresql"):
        raise AuditIntegrityError("checkpoint paid audit has unsupported original source")
    text = cast(messages.c.tool_calls, Text)
    if conn.dialect.name == "postgresql":
        byte_length = func.octet_length(text)
    else:
        byte_length = func.length(cast(messages.c.tool_calls, LargeBinary))
    # SQL bounds Python projection; exact declared identity selects candidates
    # while _closed_call still proves complete identity and source.
    bounded = case((byte_length <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES, messages.c.tool_calls), else_=None).label("tool_calls")
    rows = conn.execute(
        select(
            messages.c.id,
            messages.c.session_id,
            messages.c.role,
            messages.c.content,
            messages.c.raw_content,
            bounded,
            messages.c.tool_call_id,
            messages.c.parent_assistant_id,
            messages.c.composition_state_id,
            messages.c.writer_principal,
            byte_length.label("actual_byte_length"),
        )
        .where(
            messages.c.session_id == source.session_id,
            messages.c.role == "audit",
            messages.c.content == "Provider call result recorded.",
            public_audit_membership_predicate(conn, attempt_id=attempt_id),
        )
        .limit(2)
    ).all()
    if len(rows) != 1 or rows[0].actual_byte_length is None or rows[0].actual_byte_length > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("checkpoint paid audit lost its one bounded original public row")
    row = rows[0]
    if row.session_id != source.session_id or row.tool_calls is None:
        raise AuditIntegrityError("checkpoint paid audit changed its bounded original JSON")
    if len(canonical_json(row.tool_calls).encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("checkpoint paid audit exceeds its unchanged private-document cap")
    call = _closed_call(
        row,
        ProviderAuditSource(source.session_id, source.session_operation_id, source.session_operation_epoch),
        attempt_id,
    )
    if early_owed is not None:
        from elspeth.web.sessions.continuation_checkpoint_early_return import (
            VerifiedEarlyCompleteReturn,
            read_early_overflow_return_history_on_connection,
        )

        # Only the independently proved EARLY H arm supplies this DATA. Re-read
        # it here as well: a constructible DTO cannot waive the status rule.
        if (
            type(early_owed) is not VerifiedEarlyCompleteReturn
            or type(receipt) is not PaidOverflowReceipt
            or early_owed.source.phase != "early"
            or early_owed.source.kind != "iteration_early"
            or early_owed.attempt_id != attempt_id
            or early_owed.new_hold != source
            or receipt.representation != "retained_tail"
        ):
            raise AuditIntegrityError("EARLY owed public audit lacks its exact original H source")
        hold_state = conn.execute(
            select(holds.c.state).where(
                holds.c.session_id == source.session_id,
                holds.c.operation_id == source.operation_id,
                holds.c.effect_key == source.effect_key,
            )
        ).scalar_one()
        if hold_state not in ("active", "consumed"):
            raise AuditIntegrityError("EARLY owed public audit lost its original hold state")
        fresh = read_early_overflow_return_history_on_connection(
            conn,
            session_id=source.session_id,
            operation_id=source.operation_id,
            selected=early_owed.selected,
            names=early_owed.source.env_ref_names,
            hold_state="active" if hold_state == "active" else "consumed",
        )
        if fresh != early_owed or llm_call_audit_envelope(call) != llm_call_audit_envelope(fresh.call):
            raise AuditIntegrityError("EARLY owed public audit changed its full original public projection")
    # SQLite's database clock records whole seconds; the physical audit keeps
    # microseconds. Compare the audit finish at SQLite's stored precision while
    # retaining the original timestamp and exact PostgreSQL ordering.
    audit_finished_for_response = call.finished_at.replace(microsecond=0) if conn.dialect.name == "sqlite" else call.finished_at
    if (
        call.call_id != attempt_id
        or call.messages_hash != source.source_input_sha256
        or call.model_requested != requested_model
        or call.cost_response_accountable is not True
        or not call.started_at <= restore_utc(physical_started_at) <= call.finished_at
        or not audit_finished_for_response <= restore_utc(response_finished_at) <= restore_utc(physical_settled_at)
    ):
        raise AuditIntegrityError("checkpoint paid audit differs from its original physical fence")
    if type(receipt) is PaidIncompleteReceipt:
        if (
            call.status is not ComposerLLMCallStatus.MALFORMED_RESPONSE
            or call.error_class != "V4ProviderResultIncomplete"
            or call.error_message != "paid_result_incomplete"
        ):
            raise AuditIntegrityError("checkpoint paid incomplete changed its original malformed audit")
    elif type(receipt) is PaidOverflowReceipt:
        if early_owed is not None:
            if call.status not in (ComposerLLMCallStatus.SUCCESS, ComposerLLMCallStatus.MALFORMED_RESPONSE):
                raise AuditIntegrityError("EARLY owed overflow changed its original returned audit")
        elif call.status is not ComposerLLMCallStatus.SUCCESS or call.error_class is not None or call.error_message is not None:
            raise AuditIntegrityError("checkpoint paid overflow changed its original successful audit")
    else:
        raise AuditIntegrityError("checkpoint paid audit crossed terminal receipt types")
    cost = receipt.cost
    if call.provider_cost is None:
        if cost.status != "unknown" or cost.amount is not None or cost.source != "not_available":
            raise AuditIntegrityError("checkpoint paid audit changed its observed unknown cost")
    elif (
        cost.status != "known"
        or cost.amount != encode_paid_overflow_cost_amount(call.provider_cost)
        or cost.source != call.provider_cost_source
    ):
        raise AuditIntegrityError("checkpoint paid audit changed its original provider cost")
    usage = receipt.usage
    if (
        usage is not None
        and usage.status == "observed"
        and (
            usage.model != (call.model_returned or call.model_requested)
            or usage.prompt_tokens != call.prompt_tokens
            or usage.completion_tokens != call.completion_tokens
            or usage.cached_prompt_tokens != call.cached_prompt_tokens
            or usage.reasoning_tokens != call.reasoning_tokens
        )
    ):
        raise AuditIntegrityError("checkpoint paid audit changed its original usage observation")
