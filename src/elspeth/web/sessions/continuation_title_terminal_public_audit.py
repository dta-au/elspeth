"""Cold proof of the original bounded public call for a V4 TITLE terminal."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Connection, LargeBinary, Text, case, cast, func, select
from sqlalchemy.engine import Row

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.provider_audit_source import ProviderAuditSource
from elspeth.web.composer.visible_tool_event import visible_response_gap_for_source
from elspeth.web.sessions.continuation_paid_incomplete import PaidIncompleteReceipt
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowReceipt, encode_paid_overflow_cost_amount
from elspeth.web.sessions.continuation_retained_capacity import ProviderTerminalHoldSource
from elspeth.web.sessions.continuation_title_provider import TitleInputDescriptor
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.time_normalization import restore_utc

_CALL = TypeAdapter(ComposerLLMCall)


def _closed_title_public_call(row: Row[Any], source: ProviderAuditSource, attempt_id: str) -> ComposerLLMCall:
    if type(row) is not Row:
        raise AuditIntegrityError("TITLE terminal public call requires its actual SQL row")
    calls = row.tool_calls
    if (
        row.role != "audit"
        or row.writer_principal != "compose_loop"
        or row.content != "Provider call result recorded."
        or row.raw_content is not None
        or row.tool_call_id is not None
        or row.parent_assistant_id is not None
        or row.composition_state_id is not None
        or type(calls) is not list
        or len(calls) != 2
        or type(calls[0]) is not dict
        or calls[0].get("_kind") != "llm_call_audit"
        or type(calls[0].get("call")) is not dict
        or calls[0]["call"].get("call_id") != attempt_id
    ):
        raise AuditIntegrityError("TITLE terminal public audit lost exact original call shape")
    try:
        call = _CALL.validate_json(canonical_json(calls[0]["call"]), strict=True, extra="forbid")
    except (ValidationError, ValueError, TypeError) as exc:
        raise AuditIntegrityError("TITLE terminal public audit violates owned call projection") from exc
    if (
        type(call) is not ComposerLLMCall
        or call.cost_response_accountable is not None
        or calls[0] != llm_call_audit_envelope(call)
        or calls[1] != visible_response_gap_for_source(call, source, calls[0])
    ):
        raise AuditIntegrityError("TITLE terminal public audit changed original response companion")
    return call


def require_title_terminal_public_audit_on_connection(
    conn: Connection,
    *,
    source: ProviderTerminalHoldSource,
    descriptor: TitleInputDescriptor,
    attempt_id: str,
    requested_model: str,
    physical_started_at: datetime,
    physical_settled_at: datetime,
    response_finished_at: datetime,
    receipt: PaidOverflowReceipt | PaidIncompleteReceipt,
) -> None:
    """Require one original public row; the terminal writer owns quota separately."""
    if conn.dialect.name not in ("sqlite", "postgresql"):
        raise AuditIntegrityError("TITLE terminal public audit has unsupported dialect")
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
        raise AuditIntegrityError("TITLE terminal lost its one bounded original public call")
    row = rows[0]
    if row.tool_calls is None or len(canonical_json(row.tool_calls).encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("TITLE terminal public call exceeds unchanged document cap")
    call = _closed_title_public_call(
        row,
        ProviderAuditSource(source.session_id, source.session_operation_id, source.session_operation_epoch),
        attempt_id,
    )
    wire, error = parse_json_strict(descriptor.wire_json)
    if error is not None or type(wire) is not dict or set(wire) != {"schema", "messages", "tools_present", "tools", "options"}:
        raise AuditIntegrityError("TITLE terminal public call lost original input shape")
    options = wire["options"]
    if type(options) is not dict or type(wire["messages"]) is not list or wire["tools_present"] is not False or wire["tools"] is not None:
        raise AuditIntegrityError("TITLE terminal public call lost tool-less request")
    cost = receipt.cost
    if call.provider_cost is None:
        cost_matches = cost.status == "unknown" and cost.amount is None and cost.source == "not_available"
    else:
        cost_matches = (
            cost.status == "known"
            and cost.amount == encode_paid_overflow_cost_amount(call.provider_cost)
            and cost.source == call.provider_cost_source
        )
    usage = receipt.usage
    usage_matches = usage is not None and (
        usage.status != "observed"
        or (
            usage.model == (call.model_returned or call.model_requested)
            and usage.prompt_tokens == call.prompt_tokens
            and usage.completion_tokens == call.completion_tokens
            and usage.cached_prompt_tokens == call.cached_prompt_tokens
            and usage.reasoning_tokens == call.reasoning_tokens
        )
    )
    # SQLite CURRENT_TIMESTAMP has second precision; the warm transaction
    # orders observation after the original audit. Cold proof can compare only
    # the containing second there. PostgreSQL clock_timestamp is precise.
    audit_before_response = (
        call.finished_at < restore_utc(response_finished_at) + timedelta(seconds=1)
        if conn.dialect.name == "sqlite"
        else call.finished_at <= restore_utc(response_finished_at)
    )
    if (
        call.call_id != attempt_id
        or call.model_requested != requested_model
        or call.model_requested != options.get("model")
        or call.temperature != options.get("temperature")
        or call.seed != options.get("seed")
        or call.messages_hash != stable_hash(wire["messages"])
        or call.tools_spec_hash is not None
        or call.declared_tool_names
        or call.started_at > restore_utc(physical_started_at)
        or restore_utc(physical_started_at) > call.finished_at
        or not audit_before_response
        or restore_utc(response_finished_at) > restore_utc(physical_settled_at)
        or not cost_matches
        or not usage_matches
    ):
        raise AuditIntegrityError("TITLE terminal public audit changed original request, cost, usage or physical fence")
    if type(receipt) is PaidOverflowReceipt:
        if call.status is not ComposerLLMCallStatus.SUCCESS or call.error_class is not None or call.error_message is not None:
            raise AuditIntegrityError("TITLE overflow public call changed its original successful status")
    elif type(receipt) is PaidIncompleteReceipt:
        if (
            call.status is not ComposerLLMCallStatus.MALFORMED_RESPONSE
            or call.error_class != "V4ProviderResultIncomplete"
            or call.error_message != "returned_paid_result_incomplete"
        ):
            raise AuditIntegrityError("TITLE paid-incomplete public call changed its original malformed status")
    else:
        raise AuditIntegrityError("TITLE terminal public audit crossed receipt types")
