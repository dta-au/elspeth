"""Independent historical proof of one committed V4 END repair successor."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Literal

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, is_lower_sha256_hex
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.advisor_audit import advisor_checkpoint_pass_audit_envelope
from elspeth.web.composer.advisor_checkpoint import _parse_advisor_checkpoint_guidance, checkpoint_pass_record
from elspeth.web.composer.continuation import ComposeFrame, EndFrame
from elspeth.web.composer.continuation_codec import decode_continuation_response, decode_continuation_snapshot
from elspeth.web.composer.continuation_evidence import state_from_evidence_json
from elspeth.web.composer.continuation_progress import (
    ActionExecutionReceipt,
    continuation_action_key,
    decode_continuation_frontier,
    decode_prescribed_continuation_action,
    encode_continuation_frontier,
    record_action_receipt,
)
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_checkpoint_end_complete_pass import PreparedEndCompletePass, require_end_complete_pass_on_connection
from elspeth.web.sessions.continuation_checkpoint_end_complete_withheld import (
    PreparedEndRepairWithheldReply,
    require_end_repair_withheld_reply_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_end_owed import (
    VerifiedCheckpointEndOwed,
    read_v4_checkpoint_end_settled_owed_history_on_connection,
)
from elspeth.web.sessions.continuation_retained_capacity import read_bounded_retained_chunk
from elspeth.web.sessions.continuation_semantic_advisor import _historical_snapshot
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.time_normalization import restore_utc


@dataclass(frozen=True, slots=True)
class VerifiedEndCompleteRepairHistory:
    owed: VerifiedCheckpointEndOwed
    successor: StoredContinuationSnapshot
    child_receipt: str
    outer_receipt: str


@dataclass(frozen=True, slots=True)
class EndCompleteRepairEvent:
    owner: Literal["composer_checkpoint_end_complete"]
    version: Literal[1]
    transition: Literal["end_repair_continued"]
    attempt_id: str
    effect_key: str
    outer_effect_key: str
    child_receipt: str
    outer_receipt: str
    response_chunk_key: str
    response_sha256: str
    original_call_id: str
    original_request_sha256: str
    private_audit_sha256: str
    pass_audit_id: str
    withheld_reply_id: str | None
    iteration_sha256: str
    action_frontier_sha256: str | None


_EVENT_FIELDS = frozenset(
    {
        "owner",
        "version",
        "transition",
        "attempt_id",
        "effect_key",
        "outer_effect_key",
        "child_receipt",
        "outer_receipt",
        "response_chunk_key",
        "response_sha256",
        "original_call_id",
        "original_request_sha256",
        "private_audit_sha256",
        "pass_audit_id",
        "withheld_reply_id",
        "iteration_sha256",
        "action_frontier_sha256",
    }
)


def _bounded_event(conn: Connection, owed: VerifiedCheckpointEndOwed) -> EndCompleteRepairEvent:
    sid, operation, cursor = owed.session_id, owed.operation_id, owed.selected.cursor + 1
    row = conn.execute(
        select(events.c.tag, events.c.payload_chunk_key).where(
            events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == cursor
        )
    ).one_or_none()
    bounded = (
        read_bounded_retained_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=f"event:{cursor}",
            allowance=owed.new_hold.allowance_bytes,
        )
        if row is not None
        else None
    )
    if (
        row is None
        or row.tag != "checkpoint"
        or row.payload_chunk_key != f"event:{cursor}"
        or bounded is None
        or bounded.codec != "event.v2"
    ):
        raise AuditIntegrityError("historical END repair lost its bounded immediate event")
    text = bounded.payload.decode("utf-8", errors="strict")
    value, error = parse_json_strict(text)
    if (
        error is not None
        or type(value) is not dict
        or set(value) != _EVENT_FIELDS
        or canonical_json(value) != text
        or value["owner"] != "composer_checkpoint_end_complete"
        or type(value["version"]) is not int
        or value["version"] != 1
        or value["transition"] != "end_repair_continued"
        or value["attempt_id"] != owed.attempt_id
        or value["effect_key"] != owed.effect_key
        or value["outer_effect_key"] != owed.outer_effect_key
        or value["response_chunk_key"] != f"value:{owed.response.reference.codec}:{owed.response.reference.digest}"
        or value["response_sha256"] != owed.response.reference.digest
        or value["original_call_id"] != owed.attempt_id
        or value["original_request_sha256"] != owed.request_sha256
        or any(
            type(value[field]) is not str or not value[field]
            for field in ("child_receipt", "outer_receipt", "private_audit_sha256", "pass_audit_id", "iteration_sha256")
        )
        or (value["withheld_reply_id"] is not None and (type(value["withheld_reply_id"]) is not str or not value["withheld_reply_id"]))
        or (value["action_frontier_sha256"] is not None and not is_lower_sha256_hex(value["action_frontier_sha256"]))
        or not is_lower_sha256_hex(value["private_audit_sha256"])
        or not is_lower_sha256_hex(value["iteration_sha256"])
    ):
        raise AuditIntegrityError("historical END repair changed its complete event fields")
    return EndCompleteRepairEvent(
        owner="composer_checkpoint_end_complete",
        version=1,
        transition="end_repair_continued",
        attempt_id=owed.attempt_id,
        effect_key=owed.effect_key,
        outer_effect_key=owed.outer_effect_key,
        child_receipt=value["child_receipt"],
        outer_receipt=value["outer_receipt"],
        response_chunk_key=value["response_chunk_key"],
        response_sha256=value["response_sha256"],
        original_call_id=value["original_call_id"],
        original_request_sha256=value["original_request_sha256"],
        private_audit_sha256=value["private_audit_sha256"],
        pass_audit_id=value["pass_audit_id"],
        withheld_reply_id=value["withheld_reply_id"],
        iteration_sha256=value["iteration_sha256"],
        action_frontier_sha256=value["action_frontier_sha256"],
    )


def _reprove_pass_and_withheld(
    conn: Connection, *, owed: VerifiedCheckpointEndOwed, event: EndCompleteRepairEvent, before: ComposeFrame
) -> None:
    sid, operation = owed.session_id, owed.operation_id
    state_chunk = read_bounded_retained_chunk(
        conn,
        session_id=sid,
        operation_id=operation,
        key=f"value:{before.state.codec}:{before.state.digest}",
        allowance=owed.new_hold.allowance_bytes,
    )
    if state_chunk is None or state_chunk.codec != before.state.codec or len(state_chunk.payload) != before.state.byte_length:
        raise AuditIntegrityError("historical END repair lost its original authored state")
    state = state_from_evidence_json(state_chunk.payload.decode("utf-8", errors="strict"))
    response = decode_continuation_response(owed.response)
    if response.tool_batch.calls or type(response.message.content) is not str:
        raise AuditIntegrityError("historical END repair changed its original no-tool model reply")
    verdict = _parse_advisor_checkpoint_guidance(response.message.content, phase="end")
    if verdict.ok:
        if owed.publication_binding is None:
            raise AuditIntegrityError("historical END repair lost its original publication identity")
        verdict = replace(verdict, publication_binding=owed.publication_binding)
    ordinal = owed.retry_ordinal
    if ordinal == 0:
        if not verdict.ok:
            raise AuditIntegrityError("historical END repair has an unretired first malformed pass")
        first_schema = verdict.response_schema_valid
    elif ordinal == 1 and owed.retry_history is not None:
        first_schema = owed.retry_history.marker.first_attempt_schema_valid
    else:
        raise AuditIntegrityError("historical END repair lost its bounded format retry")
    record = checkpoint_pass_record(
        verdict,
        phase="end",
        source="model",
        state=state,
        pass_index=owed.pass_index,
        provider_attempts=ordinal + 1,
        first_attempt_schema_valid=first_schema,
        first_attempt_accepted=ordinal == 0,
        format_reprompt_sent=ordinal == 1,
    )
    envelope = advisor_checkpoint_pass_audit_envelope(record)
    content = json.dumps(envelope, sort_keys=True)
    pass_id = event.pass_audit_id
    if type(pass_id) is not str:
        raise AuditIntegrityError("historical END repair lost its pass locator")
    row = conn.execute(select(messages).where(messages.c.session_id == sid, messages.c.id == pass_id)).one_or_none()
    if row is None:
        raise AuditIntegrityError("historical END repair lost its model pass row")
    require_end_complete_pass_on_connection(
        conn,
        session_id=sid,
        prepared=PreparedEndCompletePass(pass_id, record, envelope, content),
        sequence_no=row.sequence_no,
        created_at=restore_utc(row.created_at),
    )
    superseded = owed.end_input.superseded_reply_index if owed.end_input is not None else None
    candidate, error = parse_json_strict(owed.end_input.assistant_message_json) if owed.end_input is not None else (None, ValueError())
    if error is not None or type(candidate) is not dict or type(candidate.get("content")) is not str:
        raise AuditIntegrityError("historical END repair lost its original assistant reply")
    withheld_id = event.withheld_reply_id
    content = candidate["content"]
    if (withheld_id is not None) != (superseded is not None and bool(content.strip())):
        raise AuditIntegrityError("historical END repair changed its original withheld reply applicability")
    if type(withheld_id) is str:
        withheld = conn.execute(select(messages).where(messages.c.session_id == sid, messages.c.id == withheld_id)).one_or_none()
        if withheld is None:
            raise AuditIntegrityError("historical END repair lost its withheld reply row")
        require_end_repair_withheld_reply_on_connection(
            conn,
            session_id=sid,
            prepared=PreparedEndRepairWithheldReply(withheld_id, content),
            sequence_no=withheld.sequence_no,
            created_at=restore_utc(withheld.created_at),
        )


def _reprove_action(
    conn: Connection,
    *,
    owed: VerifiedCheckpointEndOwed,
    event: EndCompleteRepairEvent,
    before: ComposeFrame,
    after: ComposeFrame,
    successor: StoredContinuationSnapshot,
) -> None:
    prior = decode_continuation_snapshot(owed.selected.payload_json)
    following = decode_continuation_snapshot(successor.payload_json)
    ref = prior.semantic_frontier
    if prior.active_action_json is None:
        if (
            prior.active_action_source is not None
            or event.action_frontier_sha256 is not None
            or following.semantic_frontier != ref
            or following.active_action_json is not None
            or following.active_action_source is not None
        ):
            raise AuditIntegrityError("historical END repair changed its inactive action frontier")
        return
    if ref is None or prior.active_action_source is None or event.action_frontier_sha256 is None:
        raise AuditIntegrityError("historical END repair lost its original Review action")
    original_chunk = read_bounded_retained_chunk(
        conn,
        session_id=owed.session_id,
        operation_id=owed.operation_id,
        key=f"value:{ref.codec}:{ref.digest}",
        allowance=owed.new_hold.allowance_bytes,
    )
    if original_chunk is None or original_chunk.codec != ref.codec:
        raise AuditIntegrityError("historical END repair lost its bounded original action receipt")
    frontier = decode_continuation_frontier(original_chunk.payload.decode("utf-8", errors="strict"))
    action = decode_prescribed_continuation_action(prior.active_action_json)
    receipt = ActionExecutionReceipt(
        continuation_action_key(action, retained_evidence={item.receipt_id: item for item in frontier.evidence}),
        owed.outer_effect_key,
        "committed",
    )
    expected = record_action_receipt(frontier, receipt, actual_owner_receipt_id=owed.outer_effect_key)
    encoded = encode_continuation_frontier(expected)
    actual_ref = following.semantic_frontier
    actual_chunk = (
        read_bounded_retained_chunk(
            conn,
            session_id=owed.session_id,
            operation_id=owed.operation_id,
            key=f"value:{actual_ref.codec}:{actual_ref.digest}",
            allowance=owed.new_hold.allowance_bytes,
        )
        if actual_ref is not None
        else None
    )
    if (
        actual_ref is None
        or actual_ref.digest != event.action_frontier_sha256
        or actual_chunk is None
        or actual_chunk.codec != "composer.semantic-frontier.v1"
        or actual_chunk.payload != encoded.encode("utf-8")
        or following.active_action_json is not None
        or following.active_action_source is not None
        or before.initial_version != after.initial_version
    ):
        raise AuditIntegrityError("historical END repair changed its committed Review action receipt")


def read_original_end_complete_repair_history_on_connection(
    conn: Connection, *, owed: VerifiedCheckpointEndOwed, names: frozenset[str]
) -> VerifiedEndCompleteRepairHistory:
    """Re-read H and its immediate committed repair from durable rows only."""
    if (
        type(owed) is not VerifiedCheckpointEndOwed
        or type(names) is not frozenset
        or owed.end_input is None
        or owed.retry_ordinal not in (0, 1)
    ):
        raise AuditIntegrityError("historical END repair requires exact original H and names")
    historical = read_v4_checkpoint_end_settled_owed_history_on_connection(
        conn,
        session_id=owed.session_id,
        operation_id=owed.operation_id,
        attempt_id=owed.attempt_id,
        owed_checkpoint_id=owed.selected.checkpoint_id,
        names=names,
        child_disposition="committed",
        hold_state="consumed",
        expected_retry_ordinal=0 if owed.retry_ordinal == 0 else 1,
    )
    if historical != owed:
        raise AuditIntegrityError("historical END repair substituted its original complete H")
    event = _bounded_event(conn, owed)
    successor = _historical_snapshot(conn, owed.session_id, owed.operation_id, cursor=owed.selected.cursor + 1)
    bounded_snapshot = read_bounded_retained_chunk(
        conn,
        session_id=owed.session_id,
        operation_id=owed.operation_id,
        key=f"snapshot:{successor.checkpoint_id}",
        allowance=owed.new_hold.allowance_bytes,
    )
    if (
        successor.cursor != owed.selected.cursor + 1
        or bounded_snapshot is None
        or bounded_snapshot.codec != "snapshot.v2"
        or bounded_snapshot.payload != successor.payload_json.encode("utf-8")
    ):
        raise AuditIntegrityError("historical END repair lost its exact immediate successor")
    before_root = decode_continuation_snapshot(owed.selected.payload_json)
    after_root = decode_continuation_snapshot(successor.payload_json)
    if (
        len(before_root.frames) != 2
        or type(before_root.frames[0]) is not ComposeFrame
        or type(before_root.frames[1]) is not EndFrame
        or len(after_root.frames) != 1
        or type(after_root.frames[0]) is not ComposeFrame
        or before_root.pending_settlement_ids != (owed.attempt_id,)
        or after_root.pending_settlement_ids
        or after_root.pending_owner_results
        or before_root.logical != after_root.logical
        or before_root.policy_hash != after_root.policy_hash
    ):
        raise AuditIntegrityError("historical END repair changed its complete frame/owner transition")
    before, after = before_root.frames[0], after_root.frames[0]
    if (
        after.phase != "action_ready"
        or after.pending_response is not None
        or after.provider_effect_key is not None
        or after.provider_input_sha256 is not None
        or after.initial_state != before.initial_state
        or after.initial_version != before.initial_version
        or after.slice_sequence != before.slice_sequence
        or after.repair_turns != before.repair_turns
        or after.advisor_checkpoint_passes != before.advisor_checkpoint_passes + owed.end_input.passes_delta + 1
        or after.advisor_repair_context_introduced is not True
        or after.iteration.digest != event.iteration_sha256
        or after_root.active_action_json is not None
        or after_root.active_action_source is not None
    ):
        raise AuditIntegrityError("historical END repair changed its original repair successor")
    child = conn.execute(
        select(effects).where(
            effects.c.session_id == owed.session_id,
            effects.c.operation_id == owed.operation_id,
            effects.c.effect_key == owed.effect_key,
        )
    ).one_or_none()
    outer = conn.execute(
        select(effects).where(
            effects.c.session_id == owed.session_id,
            effects.c.operation_id == owed.operation_id,
            effects.c.effect_key == owed.outer_effect_key,
        )
    ).one_or_none()
    child_receipt, outer_receipt = event.child_receipt, event.outer_receipt
    if (
        child is None
        or child.owner != "provider"
        or child.disposition != "committed"
        or child.provider_attempt_id != owed.attempt_id
        or child.receipt_key != child_receipt
        or child.result_chunk_key != event.response_chunk_key
        or child.response_finished_at is None
        or outer is None
        or outer.owner != "composer_end"
        or outer.disposition != "committed"
        or outer.provider_attempt_id is not None
        or outer.receipt_key != outer_receipt
        or outer_receipt != f"repair:{after.iteration.digest}"
        or outer.result_chunk_key != f"value:{after.iteration.codec}:{after.iteration.digest}"
    ):
        raise AuditIntegrityError("historical END repair changed its paid child or outer effect")
    original_frontier = {item.effect_id: item for item in before_root.effect_frontier}
    repaired_frontier = {item.effect_id: item for item in after_root.effect_frontier}
    if (
        len(original_frontier) != len(before_root.effect_frontier)
        or len(repaired_frontier) != len(after_root.effect_frontier)
        or set(original_frontier) != set(repaired_frontier)
        or any(
            repaired_frontier[key].disposition != "committed"
            or repaired_frontier[key].receipt_id != receipt
            or original_frontier[key].disposition != "dispatched"
            or original_frontier[key].receipt_id is not None
            for key, receipt in ((owed.effect_key, child_receipt), (owed.outer_effect_key, outer_receipt))
        )
        or any(
            repaired_frontier[key] != item for key, item in original_frontier.items() if key not in (owed.effect_key, owed.outer_effect_key)
        )
    ):
        raise AuditIntegrityError("historical END repair changed its exact two-effect frontier")
    _reprove_pass_and_withheld(conn, owed=owed, event=event, before=before)
    _reprove_action(conn, owed=owed, event=event, before=before, after=after, successor=successor)
    if type(child_receipt) is not str or type(outer_receipt) is not str:
        raise AuditIntegrityError("historical END repair lost its original receipt identifiers")
    return VerifiedEndCompleteRepairHistory(owed, successor, child_receipt, outer_receipt)
