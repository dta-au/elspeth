"""Cold public-audit join for one semantic returned-incomplete paid attempt."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Connection, LargeBinary, Text, case, cast, func, select

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.provider_audit_source import ProviderAuditSource
from elspeth.web.sessions.continuation_cost import _closed_call
from elspeth.web.sessions.continuation_paid_incomplete import PaidIncompleteReceipt
from elspeth.web.sessions.continuation_paid_overflow import encode_paid_overflow_cost_amount
from elspeth.web.sessions.continuation_retained_capacity import ProviderTerminalHoldSource
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.time_normalization import restore_utc


def require_semantic_paid_incomplete_public_audit_on_connection(
    conn: Connection,
    *,
    source: ProviderTerminalHoldSource,
    attempt_id: str,
    requested_model: str,
    original_messages_hash: str,
    physical_started_at: datetime,
    physical_settled_at: datetime,
    response_finished_at: datetime,
    receipt: PaidIncompleteReceipt,
) -> None:
    """Join the public sidecar to the retained semantic request and terminal.

    SQLite's ``CURRENT_TIMESTAMP`` stores whole seconds; PostgreSQL retains
    its database clock precision. Only the SQLite comparison projects call
    timestamps into those same exact recorded second buckets.
    """
    try:
        canonical_attempt = str(UUID(attempt_id))
    except (TypeError, ValueError) as exc:
        raise AuditIntegrityError("semantic paid audit lost its canonical physical identity") from exc
    if canonical_attempt != attempt_id or conn.dialect.name not in ("sqlite", "postgresql"):
        raise AuditIntegrityError("semantic paid audit has unsupported original source")
    text = cast(messages.c.tool_calls, Text)
    byte_length = func.octet_length(text) if conn.dialect.name == "postgresql" else func.length(cast(messages.c.tool_calls, LargeBinary))
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
            text.like(f"%{attempt_id}%"),
        )
        .limit(2)
    ).all()
    if len(rows) != 1 or rows[0].actual_byte_length is None or rows[0].actual_byte_length > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("semantic paid audit lost its one bounded original public row")
    row = rows[0]
    if row.session_id != source.session_id or row.tool_calls is None:
        raise AuditIntegrityError("semantic paid audit changed its bounded original JSON")
    if len(canonical_json(row.tool_calls).encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("semantic paid audit exceeds its unchanged private-document cap")
    call = _closed_call(
        row,
        ProviderAuditSource(source.session_id, source.session_operation_id, source.session_operation_epoch),
        attempt_id,
    )
    if call.call_id != attempt_id or call.messages_hash != original_messages_hash or call.model_requested != requested_model:
        raise AuditIntegrityError("semantic paid audit changed its original physical request")
    if (
        call.cost_response_accountable is not True
        or call.status is not ComposerLLMCallStatus.MALFORMED_RESPONSE
        or call.error_class != "V4ProviderResultIncomplete"
        or call.error_message != "paid_result_incomplete"
    ):
        raise AuditIntegrityError("semantic paid audit changed its original returned-incomplete outcome")
    physical_start = restore_utc(physical_started_at)
    response_finish = restore_utc(response_finished_at)
    physical_settled = restore_utc(physical_settled_at)
    if conn.dialect.name == "sqlite":
        if any(value.microsecond != 0 for value in (physical_start, response_finish, physical_settled)):
            raise AuditIntegrityError("semantic paid audit changed its SQLite database clock precision")
        call_start = call.started_at.replace(microsecond=0)
        call_finish = call.finished_at.replace(microsecond=0)
    else:
        call_start, call_finish = call.started_at, call.finished_at
    if not call_start <= physical_start <= call_finish or not call_finish <= response_finish <= physical_settled:
        raise AuditIntegrityError("semantic paid audit crossed its recorded physical time bucket")
    cost = receipt.cost
    if call.provider_cost is None:
        if cost.status != "unknown" or cost.amount is not None or cost.source != "not_available":
            raise AuditIntegrityError("semantic paid audit changed its observed unknown cost")
    elif (
        cost.status != "known"
        or cost.amount != encode_paid_overflow_cost_amount(call.provider_cost)
        or cost.source != call.provider_cost_source
    ):
        raise AuditIntegrityError("semantic paid audit changed its original provider cost")
    usage = receipt.usage
    if usage.status == "observed" and (
        usage.model != (call.model_returned or call.model_requested)
        or usage.prompt_tokens != call.prompt_tokens
        or usage.completion_tokens != call.completion_tokens
        or usage.cached_prompt_tokens != call.cached_prompt_tokens
        or usage.reasoning_tokens != call.reasoning_tokens
    ):
        raise AuditIntegrityError("semantic paid audit changed its original usage observation")
