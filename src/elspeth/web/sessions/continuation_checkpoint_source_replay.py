"""Immutable checkpoint origin/preparation/dispatch replay, independent of terminal state."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal

from sqlalchemy import Connection, Row, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.continuation import PrivateChunkRef
from elspeth.web.composer.continuation_checkpoint_end_input import decode_checkpoint_end_input
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_snapshot
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.sessions.continuation import StoredContinuationSnapshot, _effect_snapshot
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_retained_capacity import ProviderTerminalHoldSource, read_bounded_retained_chunk
from elspeth.web.sessions.continuation_selection import load_private_chunks
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_events_table as events


@dataclass(frozen=True, slots=True)
class CheckpointProviderSourceData:
    origin: StoredContinuationSnapshot
    prepared: StoredContinuationSnapshot
    admitted: StoredContinuationSnapshot
    identity: PaidOverflowIdentity
    phase: Literal["early", "end"]
    kind: str
    outer_effect_key: str
    pass_index: int
    retry_ordinal: int
    end_input_ref: PrivateChunkRef | None
    env_ref_names: frozenset[str]
    requested_model: str


def _bounded_bytes(conn: Connection, source: ProviderTerminalHoldSource, key: str, codec: str) -> bytes:
    value = read_bounded_retained_chunk(
        conn, session_id=source.session_id, operation_id=source.operation_id, key=key, allowance=source.allowance_bytes
    )
    if value is None or value.codec != codec:
        raise AuditIntegrityError("checkpoint paid overflow lost a bounded immutable original chunk")
    return value.payload


def _checkpoint_at(conn: Connection, source: ProviderTerminalHoldSource, checkpoint_id: str) -> StoredContinuationSnapshot:
    row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == source.session_id,
            checkpoints.c.operation_id == source.operation_id,
            checkpoints.c.checkpoint_id == checkpoint_id,
        )
    ).one_or_none()
    if row is None:
        raise AuditIntegrityError("checkpoint paid overflow lost its original checkpoint")
    payload = _bounded_bytes(conn, source, row.snapshot_chunk_key, "snapshot.v2").decode("utf-8", errors="strict")
    return StoredContinuationSnapshot(payload, row.cursor, row.checkpoint_id)


def _event_at(conn: Connection, source: ProviderTerminalHoldSource, cursor: int) -> tuple[str, str]:
    row = conn.execute(
        select(events).where(
            events.c.session_id == source.session_id,
            events.c.operation_id == source.operation_id,
            events.c.sequence == cursor,
        )
    ).one_or_none()
    if row is None or row.payload_chunk_key != f"event:{cursor}":
        raise AuditIntegrityError("checkpoint paid overflow lost its exact journal event")
    return row.tag, _bounded_bytes(conn, source, row.payload_chunk_key, "event.v2").decode("utf-8", errors="strict")


def read_checkpoint_provider_source_on_connection(
    conn: Connection, *, source: ProviderTerminalHoldSource, effect: Row[Any], names: frozenset[str] | None
) -> CheckpointProviderSourceData:
    """Replay only immutable source data; callers prove live or terminal ownership."""
    if type(source) is not ProviderTerminalHoldSource:
        raise TypeError("checkpoint source replay requires its nominal hold source")
    if names is not None and (type(names) is not frozenset or any(type(name) is not str for name in names)):
        raise TypeError("checkpoint source replay requires original guard names")
    sid, operation_id, effect_key = source.session_id, source.operation_id, source.effect_key
    if (
        effect.session_id != sid
        or effect.operation_id != operation_id
        or effect.effect_key != effect_key
        or effect.owner != "provider"
        or not effect_key.startswith("provider:1:")
        or effect.input_sha256 != source.source_input_sha256
        or type(effect.provider_attempt_id) is not str
        or type(effect.requested_model) is not str
        or (effect.creator_sequence, effect.creator_generation) != (source.creator_sequence, source.creator_generation)
        or (effect.physical_sequence, effect.physical_generation) != (source.creator_sequence, source.creator_generation)
    ):
        raise AuditIntegrityError("checkpoint source replay changed its original physical child")
    end_input_ref: PrivateChunkRef | None = None
    prepared = _checkpoint_at(conn, source, source.source_checkpoint_id)
    if (
        prepared.cursor != source.source_cursor
        or sha256(prepared.payload_json.encode("utf-8")).hexdigest() != source.source_snapshot_sha256
    ):
        raise AuditIntegrityError("checkpoint paid overflow changed its prepared source snapshot")
    preparation_tag, preparation_text = _event_at(conn, source, prepared.cursor)
    preparation, error = parse_json_strict(preparation_text)
    if (
        preparation_tag != "checkpoint"
        or error is not None
        or type(preparation) is not dict
        or set(preparation)
        != {
            "owner",
            "version",
            "stage",
            "phase",
            "kind",
            "pass_index",
            "outer_effect_key",
            "source_checkpoint_id",
            "source_cursor",
            "creator_sequence",
            "creator_generation",
            "end_input_chunk_key",
            "end_input_sha256",
            "end_env_ref_names",
            "captured_root_sha256",
        }
        or preparation["owner"] != "composer_checkpoint_source"
        or preparation["version"] != 1
        or preparation["stage"] != "source_prepared"
        or preparation["phase"] not in ("early", "end")
        or preparation["kind"] not in ("iteration_early", "iteration_end", "end_review")
        or (preparation["kind"] == "iteration_early") != (preparation["phase"] == "early")
        or type(preparation["pass_index"]) is not int
        or preparation["pass_index"] < 0
        or preparation["creator_sequence"] != source.creator_sequence
        or preparation["creator_generation"] != source.creator_generation
        or preparation["captured_root_sha256"] != sha256(prepared.payload_json.encode("utf-8")).hexdigest()
        or (preparation["phase"] == "end") != (preparation["end_input_chunk_key"] is not None)
        or (preparation["phase"] == "end") != (preparation["end_input_sha256"] is not None)
        or type(preparation["end_env_ref_names"]) is not list
        or any(type(name) is not str or not name for name in preparation["end_env_ref_names"])
        or preparation["end_env_ref_names"] != sorted(set(preparation["end_env_ref_names"]))
        or (preparation["phase"] == "early" and preparation["end_env_ref_names"] != [])
        or preparation_text != canonical_json(preparation)
    ):
        raise AuditIntegrityError("checkpoint paid overflow lost its original phase preparation event")
    origin = _checkpoint_at(conn, source, preparation["source_checkpoint_id"])
    prepared_root = decode_continuation_snapshot(prepared.payload_json)
    original_root = decode_continuation_snapshot(origin.payload_json)
    if (
        origin.cursor != preparation["source_cursor"]
        or prepared.cursor != origin.cursor + 1
        or prepared_root.model_copy(update={"frames": original_root.frames, "cursor": original_root.cursor}) != original_root
        or preparation["outer_effect_key"] not in {item.effect_id for item in original_root.effect_frontier}
        or not any(
            item.effect_id == preparation["outer_effect_key"] and item.disposition == "dispatched" for item in original_root.effect_frontier
        )
    ):
        raise AuditIntegrityError("checkpoint paid overflow lost its original unpaid outer phase")
    end_retry_ordinal: int | None = None
    if preparation["phase"] == "end":
        if names != frozenset(preparation["end_env_ref_names"]):
            raise AuditIntegrityError("checkpoint paid overflow changed current END guard inventory")
        end_key = preparation["end_input_chunk_key"]
        end_digest = preparation["end_input_sha256"]
        if type(end_key) is not str or type(end_digest) is not str or end_key != f"value:composer.checkpoint-end-input.v1:{end_digest}":
            raise AuditIntegrityError("checkpoint paid overflow changed its original END input reference")
        end_bytes = _bounded_bytes(conn, source, end_key, "composer.checkpoint-end-input.v1")
        if len(end_bytes) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES or sha256(end_bytes).hexdigest() != end_digest:
            raise AuditIntegrityError("checkpoint paid overflow changed its bounded END input bytes")
        end_data = decode_checkpoint_end_input(
            PrivateChunk(
                PrivateChunkRef(codec="composer.checkpoint-end-input.v1", digest=end_digest, byte_length=len(end_bytes)),
                end_bytes.decode("utf-8", errors="strict"),
            ),
            env_ref_names=frozenset(preparation["end_env_ref_names"]),
        )
        if (
            end_data.session_id != sid
            or end_data.operation_id != operation_id
            or end_data.original_checkpoint_id != origin.checkpoint_id
            or end_data.original_cursor != origin.cursor
            or end_data.outer_effect_key != preparation["outer_effect_key"]
            or end_data.pass_index != preparation["pass_index"]
            or end_data.captured_runtime_sha256
            != stable_hash(prepared_root.model_copy(update={"cursor": origin.cursor}).model_dump(mode="json"))
            or end_data.runtime_chunks
            != tuple(chunk.reference for chunk in load_private_chunks(conn, session_id=sid, operation_id=operation_id, stack=prepared_root))
        ):
            raise AuditIntegrityError("checkpoint paid overflow lost its complete original END gate input")
        end_retry_ordinal = end_data.retry_ordinal
        end_input_ref = PrivateChunkRef(codec="composer.checkpoint-end-input.v1", digest=end_digest, byte_length=len(end_bytes))
    admitted_row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.cursor == prepared.cursor + 1,
        )
    ).one_or_none()
    if admitted_row is None or admitted_row.kind != "transition":
        raise AuditIntegrityError("checkpoint paid overflow lost its original paid dispatch")
    admitted = _checkpoint_at(conn, source, admitted_row.checkpoint_id)
    dispatch_tag, dispatch_text = _event_at(conn, source, admitted.cursor)
    dispatch, error = parse_json_strict(dispatch_text)
    if (
        dispatch_tag != "checkpoint"
        or error is not None
        or type(dispatch) is not dict
        or set(dispatch)
        != {
            "owner",
            "version",
            "stage",
            "phase",
            "kind",
            "pass_index",
            "retry_ordinal",
            "effect_key",
            "outer_effect_key",
            "attempt_id",
            "request_sha256",
            "requested_model",
            "source_checkpoint_id",
            "source_cursor",
            "end_input_chunk_key",
            "end_input_sha256",
            "end_env_ref_names",
        }
        or dispatch["owner"] != "composer_checkpoint_paid"
        or dispatch["version"] != 1
        or dispatch["stage"] != "dispatched"
        or dispatch["phase"] != preparation["phase"]
        or dispatch["kind"] != preparation["kind"]
        or dispatch["pass_index"] != preparation["pass_index"]
        or type(dispatch["retry_ordinal"]) is not int
        or dispatch["retry_ordinal"] < 0
        or dispatch["effect_key"] != effect_key
        or dispatch["outer_effect_key"] != preparation["outer_effect_key"]
        or dispatch["attempt_id"] != effect.provider_attempt_id
        or dispatch["request_sha256"] != source.source_input_sha256
        or dispatch["requested_model"] != effect.requested_model
        or dispatch["source_checkpoint_id"] != prepared.checkpoint_id
        or dispatch["source_cursor"] != prepared.cursor
        or dispatch["end_input_chunk_key"] != preparation["end_input_chunk_key"]
        or dispatch["end_input_sha256"] != preparation["end_input_sha256"]
        or dispatch["end_env_ref_names"] != preparation["end_env_ref_names"]
        or (preparation["phase"] == "end" and dispatch["retry_ordinal"] != end_retry_ordinal)
        or dispatch_text != canonical_json(dispatch)
    ):
        raise AuditIntegrityError("checkpoint paid overflow changed its original dispatch event")
    expected_dispatched = decode_continuation_snapshot(
        _effect_snapshot(
            prepared.payload_json,
            effect_key=effect_key,
            disposition="dispatched",
            receipt_id=None,
            add_attempt_id=effect.provider_attempt_id,
        )
    ).model_copy(update={"cursor": admitted.cursor})
    if admitted.cursor != prepared.cursor + 1 or decode_continuation_snapshot(admitted.payload_json) != expected_dispatched:
        raise AuditIntegrityError("checkpoint paid overflow changed its complete original admitted root")
    identity = PaidOverflowIdentity(
        session_id=sid,
        operation_id=operation_id,
        effect_key=effect_key,
        attempt_id=effect.provider_attempt_id,
        session_operation_id=source.session_operation_id,
        session_operation_epoch=source.session_operation_epoch,
        ownership_generation=source.creator_generation,
        lease_sha256=source.lease_sha256,
        policy_sha256=source.policy_sha256,
        source_checkpoint_id=source.source_checkpoint_id,
        source_cursor=source.source_cursor,
        source_snapshot_sha256=source.source_snapshot_sha256,
    )
    return CheckpointProviderSourceData(
        origin,
        prepared,
        admitted,
        identity,
        "end" if preparation["phase"] == "end" else "early",
        preparation["kind"],
        preparation["outer_effect_key"],
        preparation["pass_index"],
        dispatch["retry_ordinal"],
        end_input_ref,
        frozenset(preparation["end_env_ref_names"]),
        effect.requested_model,
    )
