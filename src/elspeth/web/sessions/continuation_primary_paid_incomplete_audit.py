"""Bounded cold join from a PRIMARY incomplete terminal to its public physical audit."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Connection, LargeBinary, Text, case, cast, func, select

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.provider_audit_source import ProviderAuditSource
from elspeth.web.sessions.continuation_audit_membership import public_audit_membership_predicate
from elspeth.web.sessions.continuation_cost import _closed_call
from elspeth.web.sessions.continuation_paid_incomplete import PaidIncompleteReceipt
from elspeth.web.sessions.continuation_paid_overflow import encode_paid_overflow_cost_amount
from elspeth.web.sessions.continuation_primary import RetainedPrimaryInput, _original_wire
from elspeth.web.sessions.continuation_retained_capacity import ProviderTerminalHoldSource
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.time_normalization import restore_utc


def require_primary_incomplete_public_audit_on_connection(
    conn: Connection,
    *,
    source: ProviderTerminalHoldSource,
    attempt_id: str,
    descriptor: RetainedPrimaryInput,
    physical_started_at: datetime,
    physical_settled_at: datetime,
    response_finished_at: datetime,
    receipt: PaidIncompleteReceipt,
) -> None:
    """Reprove one bounded original sidecar before granting a cold terminal."""
    try:
        canonical_attempt = str(UUID(attempt_id))
    except (TypeError, ValueError) as exc:
        raise AuditIntegrityError("PRIMARY incomplete audit lost its canonical physical identity") from exc
    if canonical_attempt != attempt_id or conn.dialect.name not in ("sqlite", "postgresql"):
        raise AuditIntegrityError("PRIMARY incomplete audit has unsupported original source")
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
        raise AuditIntegrityError("PRIMARY incomplete audit lost its one bounded original public row")
    row = rows[0]
    if row.session_id != source.session_id or row.tool_calls is None:
        raise AuditIntegrityError("PRIMARY incomplete audit changed its bounded original JSON")
    if len(canonical_json(row.tool_calls).encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("PRIMARY incomplete audit exceeds its unchanged private-document cap")
    call = _closed_call(
        row,
        ProviderAuditSource(source.session_id, source.session_operation_id, source.session_operation_epoch),
        attempt_id,
    )
    wire, tools = _original_wire(descriptor)
    # SQLite's database_now uses CURRENT_TIMESTAMP, whose precision is whole
    # seconds. Compare the original audit at that clock's resolution without
    # changing its retained timestamp. PostgreSQL preserves microseconds.
    comparable_finished_at = call.finished_at.replace(microsecond=0) if conn.dialect.name == "sqlite" else call.finished_at
    if (
        call.call_id != attempt_id
        or call.messages_hash != stable_hash(wire)
        or call.tools_spec_hash != (stable_hash(tools) if tools else None)
        or call.model_requested != descriptor.model
        or call.temperature != descriptor.temperature
        or call.seed != descriptor.seed
        or call.cost_response_accountable is not True
        or call.started_at != restore_utc(physical_started_at)
        or not comparable_finished_at <= restore_utc(response_finished_at) <= restore_utc(physical_settled_at)
    ):
        raise AuditIntegrityError("PRIMARY incomplete audit differs from its original physical fence")
    if (
        type(receipt) is not PaidIncompleteReceipt
        or receipt.producer != "primary"
        or receipt.identity.attempt_id != attempt_id
        or call.status is not ComposerLLMCallStatus.MALFORMED_RESPONSE
        or call.error_class != "V4ProviderResultIncomplete"
        or call.error_message != "paid_result_incomplete"
    ):
        raise AuditIntegrityError("PRIMARY incomplete changed its original malformed audit")
    cost = receipt.cost
    if call.provider_cost is None:
        if cost.status != "unknown" or cost.amount is not None or cost.source != "not_available":
            raise AuditIntegrityError("PRIMARY incomplete audit changed its observed unknown cost")
    elif (
        cost.status != "known"
        or cost.amount != encode_paid_overflow_cost_amount(call.provider_cost)
        or cost.source != call.provider_cost_source
    ):
        raise AuditIntegrityError("PRIMARY incomplete audit changed its original provider cost")
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
        raise AuditIntegrityError("PRIMARY incomplete audit changed its original usage observation")
