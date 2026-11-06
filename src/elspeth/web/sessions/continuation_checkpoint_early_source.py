"""Bounded immutable EARLY request commitment; DATA never grants SDK authority."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Literal
from weakref import ReferenceType

from pydantic import Field, ValidationError, model_validator
from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation import ContinuationValue, PrivateChunkRef
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_snapshot, encode_private_chunk
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_retained_capacity import ProviderTerminalHoldSource, read_bounded_retained_chunk

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_checkpoint_early_physical import CheckpointEarlyPhysicalSource
    from elspeth.web.sessions.continuation_checkpoint_source_replay import CheckpointProviderSourceData


class CheckpointEarlyGuardCommitment(ContinuationValue):
    provenance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: Literal["advisor"] = "advisor"
    mode: Literal["required", "debug-when-unconfigured"]
    adapter: str | None
    policy_fingerprint: str | None
    validity_fingerprint: str
    timeout_seconds: int | float | None


class CheckpointEarlyInput(ContinuationValue):
    schema_version: Literal["composer.checkpoint-early-input.v1"] = "composer.checkpoint-early-input.v1"
    session_id: str
    operation_id: str
    original_checkpoint_id: str
    original_cursor: int = Field(ge=0)
    outer_effect_key: str
    retry_ordinal: Literal[0, 1]
    captured_runtime_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_chunks: tuple[PrivateChunkRef, ...]
    state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    transcript_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    messages_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prepared_guard_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    guard: CheckpointEarlyGuardCommitment
    requested_model: str = Field(min_length=1)
    env_ref_names: tuple[str, ...]

    @model_validator(mode="after")
    def closed_inventory(self) -> CheckpointEarlyInput:
        if self.env_ref_names != tuple(sorted(set(self.env_ref_names))) or any(not name for name in self.env_ref_names):
            raise ValueError("EARLY request requires its complete sorted original environment names")
        if self.prepared_guard_sha256 != stable_hash(self.guard.model_dump(mode="json")):
            raise ValueError("EARLY request changed its complete prepared guard commitment")
        return self


@dataclass(frozen=True, slots=True)
class CapturedCheckpointEarlyInput:
    snapshot: CheckpointEarlyInput
    chunk: PrivateChunk
    physical_source: ReferenceType[CheckpointEarlyPhysicalSource]


def encode_checkpoint_early_input(snapshot: CheckpointEarlyInput) -> PrivateChunk:
    if type(snapshot) is not CheckpointEarlyInput:
        raise AuditIntegrityError("EARLY input requires its nominal owned snapshot")
    text = canonical_json(snapshot.model_dump(mode="json"))
    if len(text.encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("EARLY input exceeds the unchanged complete private document bound")
    return encode_private_chunk(codec="composer.checkpoint-early-input.v1", canonical=text, env_ref_names=frozenset(snapshot.env_ref_names))


def decode_checkpoint_early_input(chunk: PrivateChunk) -> CheckpointEarlyInput:
    raw = chunk.canonical.encode("utf-8")
    if (
        chunk.reference.codec != "composer.checkpoint-early-input.v1"
        or len(raw) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
        or len(raw) != chunk.reference.byte_length
        or sha256(raw).hexdigest() != chunk.reference.digest
    ):
        raise AuditIntegrityError("EARLY input changed its bounded immutable reference")
    try:
        snapshot = CheckpointEarlyInput.model_validate_json(chunk.canonical, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("EARLY input lost its closed original request shape") from error
    if canonical_json(snapshot.model_dump(mode="json")) != chunk.canonical:
        raise AuditIntegrityError("EARLY input changed its canonical original bytes")
    return snapshot


def early_source_event_fields(reference: PrivateChunkRef | None) -> dict[str, int | str]:
    if reference is None:
        return {"version": 1}
    if reference.codec != "composer.checkpoint-early-input.v1":
        raise AuditIntegrityError("EARLY source changed its installed request codec")
    return {"version": 2, "early_input_chunk_key": f"value:{reference.codec}:{reference.digest}", "early_input_sha256": reference.digest}


def read_checkpoint_early_input_for_hold_on_connection(
    conn: Connection,
    *,
    source: ProviderTerminalHoldSource,
) -> CheckpointEarlyInput | None:
    """Recover a recipe input through complete bounded source replay.

    A version-one source retains its historical recipe. A version-two EARLY
    source must reprove the actual prepared graph, request, and encoded guard
    inventory; reading this DATA creates no live physical authority.
    """
    from elspeth.contracts.json_parser import parse_json_strict
    from elspeth.web.sessions.continuation_checkpoint_paid_terminal import _bounded_bytes, _event_at
    from elspeth.web.sessions.continuation_checkpoint_source_replay import read_checkpoint_provider_source_on_connection
    from elspeth.web.sessions.models import composer_continuation_effects_table as effects

    tag, text = _event_at(conn, source, source.source_cursor)
    value, error = parse_json_strict(text)
    if tag != "checkpoint" or error is not None or type(value) is not dict:
        raise AuditIntegrityError("checkpoint hold lost its original prepared-source event")
    if value.get("version") == 1:
        return None
    if value.get("version") != 2 or value.get("phase") != "early":
        raise AuditIntegrityError("checkpoint hold lost its closed prepared-source version")
    key, digest = value["early_input_chunk_key"], value["early_input_sha256"]
    if type(key) is not str or type(digest) is not str or key != f"value:composer.checkpoint-early-input.v1:{digest}":
        raise AuditIntegrityError("checkpoint hold replaced its original EARLY input reference")
    payload = _bounded_bytes(conn, source, key, "composer.checkpoint-early-input.v1")
    early = decode_checkpoint_early_input(
        PrivateChunk(
            PrivateChunkRef(codec="composer.checkpoint-early-input.v1", digest=digest, byte_length=len(payload)),
            payload.decode("utf-8", errors="strict"),
        )
    )
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == source.session_id,
            effects.c.operation_id == source.operation_id,
            effects.c.effect_key == source.effect_key,
        )
    ).one()
    replay = read_checkpoint_provider_source_on_connection(conn, source=source, effect=effect, names=frozenset(early.env_ref_names))
    if replay.phase != "early" or replay.early_input != early:
        raise AuditIntegrityError("checkpoint hold replaced its complete original EARLY request")
    return early


def require_early_input_on_connection(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    reference: PrivateChunkRef,
    allowance: int,
    origin: StoredContinuationSnapshot,
    prepared: StoredContinuationSnapshot,
    outer_effect_key: str,
) -> CheckpointEarlyInput:
    value = read_bounded_retained_chunk(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        key=f"value:{reference.codec}:{reference.digest}",
        allowance=allowance,
    )
    if value is None or value.codec != reference.codec:
        raise AuditIntegrityError("EARLY source lost its bounded original request commitment")
    snapshot = decode_checkpoint_early_input(PrivateChunk(reference, value.payload.decode("utf-8", errors="strict")))
    root = decode_continuation_snapshot(prepared.payload_json)
    from elspeth.web.composer.continuation import ComposeFrame
    from elspeth.web.sessions.continuation_selection import load_private_chunks

    compose = root.frames[0]
    if (
        type(compose) is not ComposeFrame
        or snapshot.session_id != session_id
        or snapshot.operation_id != operation_id
        or snapshot.original_checkpoint_id != origin.checkpoint_id
        or snapshot.original_cursor != origin.cursor
        or snapshot.outer_effect_key != outer_effect_key
        or snapshot.captured_runtime_sha256 != stable_hash(root.model_copy(update={"cursor": origin.cursor}).model_dump(mode="json"))
        or snapshot.runtime_chunks
        != tuple(chunk.reference for chunk in load_private_chunks(conn, session_id=session_id, operation_id=operation_id, stack=root))
        or snapshot.state_sha256 != compose.state.digest
        or snapshot.transcript_sha256 != compose.transcript.digest
    ):
        raise AuditIntegrityError("EARLY request commitment changed its original complete source graph")
    return snapshot


def resolve_early_original_stage_on_connection(
    conn: Connection,
    *,
    source: CheckpointProviderSourceData,
    names: frozenset[str],
) -> StoredContinuationSnapshot:
    """Resolve E or exactly R0/C0/H0/E; never search arbitrary old stages."""
    from elspeth.web.sessions.continuation_checkpoint_source_replay import CheckpointProviderSourceData
    from elspeth.web.sessions.continuation_iteration_return import _prove_record, _stage_record

    if (
        type(source) is not CheckpointProviderSourceData
        or source.phase != "early"
        or source.kind != "iteration_early"
        or source.early_input is None
        or source.early_input_ref is None
        or source.env_ref_names != names
        or source.early_input.retry_ordinal != source.retry_ordinal
    ):
        raise AuditIntegrityError("EARLY original stage requires its exact versioned request source")
    origin = source.origin
    if source.retry_ordinal == 1:
        from elspeth.web.sessions.continuation_checkpoint_early_retry import read_early_format_retry_on_connection

        retry = read_early_format_retry_on_connection(
            conn,
            session_id=source.identity.session_id,
            operation_id=source.identity.operation_id,
            selected=origin,
        )
        if retry is None or retry.marker.outer_effect_key != source.outer_effect_key:
            raise AuditIntegrityError("EARLY second request lost its original complete format retry")
        origin = retry.child.original_stage
    elif source.retry_ordinal != 0:
        raise AuditIntegrityError("EARLY request exceeded its one original complete-response retry")
    record = _stage_record(conn, origin, source.identity.session_id, source.identity.operation_id)
    if record is None or record[0].stage != "early_dispatched" or not record[0].early_required:
        raise AuditIntegrityError("EARLY source lost its exact original dispatched iteration stage")
    _prove_record(conn, origin, record[0], record[1])
    from elspeth.web.composer.continuation_iteration_transitions import applied_batch_cut

    original_compose, original_batch = applied_batch_cut(decode_continuation_snapshot(origin.payload_json).frames)
    current_compose, current_batch = applied_batch_cut(decode_continuation_snapshot(source.origin.payload_json).frames)
    if source.retry_ordinal == 1:
        # The full retry-child proof above verifies the refreshed accumulator:
        # its shared graph includes the first paid call's appended recorder.
        # Compare the remaining batch invariants with the pre-call stage.
        current_batch = current_batch.model_copy(update={"accumulator": original_batch.accumulator})
    if (
        source.outer_effect_key != f"advance-early:{original_batch.response.digest}"
        or current_batch != original_batch
        or current_compose.state != original_compose.state
    ):
        raise AuditIntegrityError("EARLY request changed its original applied graph or unpaid outer owner")
    return origin
