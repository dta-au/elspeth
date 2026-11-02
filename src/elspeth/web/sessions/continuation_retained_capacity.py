"""Transaction-local admission of exact retained Continuation chunk payload bytes.

The caller owns the original session fence, lock and selected-root CAS. This
helper observes every key in that same transaction; it grants no authority to
start a new operation or to resume paid work after a capacity refusal.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from typing import Literal

from sqlalchemy import Connection, LargeBinary, case, cast, func, insert, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as provider_attempts


class ContinuationRetainedCapacityExceeded(Exception):
    """Fresh operation payload would exceed its caller-supplied allowance."""

    reason = "retained_history_bytes"

    def __init__(self) -> None:
        super().__init__("continuation retained payload allowance exceeded")


@dataclass(frozen=True, slots=True)
class ProspectiveContinuationChunk:
    key: str
    payload: bytes
    codec: str


@dataclass(frozen=True, slots=True)
class ContinuationReadContract:
    version: str
    policy_hash: str | None
    allowance: int | None


@dataclass(frozen=True, slots=True)
class ProviderTerminalHoldSource:
    """Original V4 source and bounded fallback, never a dispatch grant."""

    session_id: str
    operation_id: str
    effect_key: str
    policy_sha256: str
    allowance_bytes: int
    source_checkpoint_id: str
    source_cursor: int
    source_snapshot_sha256: str
    source_input_sha256: str
    creator_sequence: int
    creator_generation: int
    claim_attempt: int
    session_operation_id: str
    session_operation_epoch: int
    lease_sha256: str
    reserved_bytes: int
    fallback_contract_sha256: str


@dataclass(frozen=True, slots=True)
class ProviderTerminalHoldState:
    state: Literal["active", "consumed"]
    provider_attempt_id: str | None


def load_continuation_read_contract(conn: Connection, *, session_id: str, operation_id: str) -> ContinuationReadContract:
    row = conn.execute(
        select(
            jobs.c.continuation_policy_version,
            jobs.c.continuation_policy_sha256,
            jobs.c.continuation_retained_payload_limit_bytes,
        ).where(jobs.c.session_id == session_id, jobs.c.operation_id == operation_id)
    ).one_or_none()
    if row is None:
        raise AuditIntegrityError("continuation read contract lost its parent")
    version = row.continuation_policy_version
    digest = row.continuation_policy_sha256
    allowance = row.continuation_retained_payload_limit_bytes
    if version in ("composer.continuation.policy.v1", "composer.continuation.policy.v2"):
        if digest is not None or allowance is not None:
            raise AuditIntegrityError("legacy continuation has a V3 read contract")
    elif version == "composer.continuation.policy.v3":
        if type(digest) is not str or len(digest) != 64 or type(allowance) is not int or allowance <= 0:
            raise AuditIntegrityError("V3 continuation lost its original read contract")
    elif version == "composer.continuation.policy.v4":
        if type(digest) is not str or len(digest) != 64 or type(allowance) is not int or allowance <= 0:
            raise AuditIntegrityError("V4 continuation lost its original read contract")
    else:
        raise AuditIntegrityError("continuation read contract is unclassified")
    return ContinuationReadContract(version, digest, allowance)


def read_bounded_retained_chunk(
    conn: Connection, *, session_id: str, operation_id: str, key: str, allowance: int
) -> ProspectiveContinuationChunk | None:
    """Project actual bytes only if the original admitted per-chunk ceiling holds."""
    if type(allowance) is not int or allowance <= 0:
        raise AuditIntegrityError("bounded continuation read requires its original allowance")
    if conn.dialect.name == "postgresql":
        actual_length = func.octet_length(chunks.c.payload)
    elif conn.dialect.name == "sqlite":
        actual_length = func.length(cast(chunks.c.payload, LargeBinary))
    else:
        raise AuditIntegrityError("bounded continuation read has an unsupported database")
    row = conn.execute(
        select(
            chunks.c.codec,
            chunks.c.sha256,
            chunks.c.byte_length,
            actual_length.label("actual_byte_length"),
            case((actual_length <= allowance, chunks.c.payload), else_=None).label("payload"),
        ).where(chunks.c.session_id == session_id, chunks.c.operation_id == operation_id, chunks.c.chunk_key == key)
    ).one_or_none()
    if row is None:
        return None
    if (
        type(row.actual_byte_length) is not int
        or not 0 <= row.actual_byte_length <= allowance
        or type(row.payload) is not bytes
        or len(row.payload) != row.actual_byte_length
        or row.byte_length != row.actual_byte_length
        or type(row.codec) is not str
        or not row.codec
        or type(row.sha256) is not str
        or sha256(row.payload).hexdigest() != row.sha256
    ):
        raise AuditIntegrityError("continuation chunk exceeded its original bound or lost its SQL binding")
    return ProspectiveContinuationChunk(key, row.payload, row.codec)


def _live_provider_hold_bytes(conn: Connection, *, session_id: str, operation_id: str, contract: ContinuationReadContract) -> int:
    total = 0
    rows = conn.execute(
        select(holds.c.state, holds.c.policy_sha256, holds.c.allowance_bytes, holds.c.reserved_bytes).where(
            holds.c.session_id == session_id, holds.c.operation_id == operation_id
        )
    )
    for row in rows:
        if contract.version != "composer.continuation.policy.v4":
            raise AuditIntegrityError("legacy continuation acquired a paid-provider terminal hold")
        if (
            row.policy_sha256 != contract.policy_hash
            or row.allowance_bytes != contract.allowance
            or type(row.reserved_bytes) is not int
            or not 0 < row.reserved_bytes <= row.allowance_bytes
            or row.state not in ("active", "consumed")
        ):
            raise AuditIntegrityError("provider terminal hold differs from original admission")
        if row.state == "active":
            total += row.reserved_bytes
    return total


def _require_sha256(value: str, *, field: str) -> None:
    if type(value) is not str or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"provider terminal hold {field} must be a lowercase SHA-256")


def _require_hold_source(source: ProviderTerminalHoldSource) -> None:
    if type(source) is not ProviderTerminalHoldSource:
        raise TypeError("provider terminal hold requires its original owned source")
    for value, field, maximum in (
        (source.session_id, "session", 128),
        (source.operation_id, "operation", 36),
        (source.effect_key, "effect", 128),
        (source.source_checkpoint_id, "checkpoint", 36),
        (source.session_operation_id, "session operation", 128),
    ):
        if type(value) is not str or not 0 < len(value) <= maximum:
            raise ValueError(f"provider terminal hold {field} identity is invalid")
    if len(source.operation_id) != 36 or len(source.source_checkpoint_id) != 36:
        raise ValueError("provider terminal hold UUID identity is invalid")
    for value, field in (
        (source.policy_sha256, "policy"),
        (source.source_snapshot_sha256, "source snapshot"),
        (source.source_input_sha256, "source input"),
        (source.lease_sha256, "lease"),
        (source.fallback_contract_sha256, "fallback contract"),
    ):
        _require_sha256(value, field=field)
    if (
        type(source.allowance_bytes) is not int
        or type(source.reserved_bytes) is not int
        or not 0 < source.reserved_bytes <= source.allowance_bytes
        or type(source.source_cursor) is not int
        or source.source_cursor < 0
        or type(source.creator_sequence) is not int
        or source.creator_sequence <= 0
        or type(source.creator_generation) is not int
        or source.creator_generation <= 0
        or type(source.claim_attempt) is not int
        or source.claim_attempt <= 0
        or type(source.session_operation_epoch) is not int
        or source.session_operation_epoch <= 0
    ):
        raise ValueError("provider terminal hold source has an invalid bound or original fence")


def _require_historical_hold_source(conn: Connection, source: ProviderTerminalHoldSource) -> ContinuationReadContract:
    _require_hold_source(source)
    contract = load_continuation_read_contract(conn, session_id=source.session_id, operation_id=source.operation_id)
    if (
        contract.version != "composer.continuation.policy.v4"
        or contract.policy_hash != source.policy_sha256
        or contract.allowance != source.allowance_bytes
    ):
        raise AuditIntegrityError("provider terminal hold differs from original V4 policy")
    effect = conn.execute(
        select(effects.c.owner, effects.c.input_sha256, effects.c.creator_sequence, effects.c.creator_generation).where(
            effects.c.session_id == source.session_id,
            effects.c.operation_id == source.operation_id,
            effects.c.effect_key == source.effect_key,
        )
    ).one_or_none()
    if (
        effect is None
        or effect.owner != "provider"
        or effect.input_sha256 != source.source_input_sha256
        or effect.creator_sequence != source.creator_sequence
        or effect.creator_generation != source.creator_generation
    ):
        raise AuditIntegrityError("provider terminal hold differs from its original provider effect")
    creator = conn.execute(
        select(
            attempts.c.claim_attempt,
            attempts.c.session_operation_id,
            attempts.c.session_operation_epoch,
            attempts.c.session_operation_lease_token,
        ).where(
            attempts.c.session_id == source.session_id,
            attempts.c.operation_id == source.operation_id,
            attempts.c.sequence == source.creator_sequence,
            attempts.c.generation == source.creator_generation,
        )
    ).one_or_none()
    if (
        creator is None
        or creator.claim_attempt != source.claim_attempt
        or creator.session_operation_id != source.session_operation_id
        or creator.session_operation_epoch != source.session_operation_epoch
        or sha256(creator.session_operation_lease_token.encode("utf-8")).hexdigest() != source.lease_sha256
    ):
        raise AuditIntegrityError("provider terminal hold differs from its original physical fence")
    checkpoint = conn.execute(
        select(checkpoints.c.cursor, checkpoints.c.snapshot_chunk_key).where(
            checkpoints.c.session_id == source.session_id,
            checkpoints.c.operation_id == source.operation_id,
            checkpoints.c.checkpoint_id == source.source_checkpoint_id,
        )
    ).one_or_none()
    if checkpoint is None or checkpoint.cursor != source.source_cursor:
        raise AuditIntegrityError("provider terminal hold differs from its original checkpoint")
    snapshot = read_bounded_retained_chunk(
        conn,
        session_id=source.session_id,
        operation_id=source.operation_id,
        key=checkpoint.snapshot_chunk_key,
        allowance=source.allowance_bytes,
    )
    if snapshot is None or snapshot.codec != "snapshot.v2" or sha256(snapshot.payload).hexdigest() != source.source_snapshot_sha256:
        raise AuditIntegrityError("provider terminal hold differs from its original snapshot")
    return contract


def _require_hold_fields(
    conn: Connection, *, source: ProviderTerminalHoldSource, contract: ContinuationReadContract
) -> ProviderTerminalHoldState:
    _require_hold_source(source)
    if (
        contract.version != "composer.continuation.policy.v4"
        or source.policy_sha256 != contract.policy_hash
        or source.allowance_bytes != contract.allowance
    ):
        raise AuditIntegrityError("provider hold credit differs from original V4 admission")
    row = conn.execute(
        select(holds).where(
            holds.c.session_id == source.session_id,
            holds.c.operation_id == source.operation_id,
            holds.c.effect_key == source.effect_key,
        )
    ).one_or_none()
    if row is None:
        raise AuditIntegrityError("provider hold lost its original row")
    for field, expected in asdict(source).items():
        if row._mapping[field] != expected:
            raise AuditIntegrityError("provider hold differs from its original source")
    _require_historical_hold_source(conn, source)
    if row.state not in ("active", "consumed") or (row.provider_attempt_id is not None and type(row.provider_attempt_id) is not str):
        raise AuditIntegrityError("provider hold has an invalid settlement state")
    return ProviderTerminalHoldState(state=row.state, provider_attempt_id=row.provider_attempt_id)


def _require_active_provider_hold(conn: Connection, *, source: ProviderTerminalHoldSource, contract: ContinuationReadContract) -> int:
    row = _require_hold_fields(conn, source=source, contract=contract)
    if row.state != "active" or row.provider_attempt_id is None:
        raise AuditIntegrityError("provider hold credit requires its original active paid attempt")
    return source.reserved_bytes


def reserve_provider_terminal_hold(
    conn: Connection,
    *,
    source: ProviderTerminalHoldSource,
    prospective: tuple[ProspectiveContinuationChunk, ...],
    created_at: datetime,
) -> None:
    """Reserve a V4 provider fallback within the caller's original locked cut.

    This does not turn a prepared effect into a dispatched one. The issuer must
    perform that CAS in the same transaction before it may enter the SDK.
    """
    if not conn.in_transaction():
        raise AuditIntegrityError("provider terminal hold requires the original SQL transaction")
    if type(created_at) is not datetime or created_at.tzinfo is None:
        raise ValueError("provider terminal hold requires an aware creation time")
    contract = _require_historical_hold_source(conn, source)
    parent = conn.execute(
        select(
            jobs.c.status,
            jobs.c.checkpoint_id,
            jobs.c.journal_cursor,
            jobs.c.current_segment_sequence,
            jobs.c.ownership_generation,
            jobs.c.attempt,
            jobs.c.session_operation_id,
            jobs.c.session_operation_epoch,
            jobs.c.session_operation_lease_token,
        ).where(jobs.c.session_id == source.session_id, jobs.c.operation_id == source.operation_id)
    ).one()
    if (
        parent.status != "running"
        or parent.checkpoint_id != source.source_checkpoint_id
        or parent.journal_cursor != source.source_cursor
        or parent.current_segment_sequence != source.creator_sequence
        or parent.ownership_generation != source.creator_generation
        or parent.attempt != source.claim_attempt
        or parent.session_operation_id != source.session_operation_id
        or parent.session_operation_epoch != source.session_operation_epoch
        or type(parent.session_operation_lease_token) is not str
        or sha256(parent.session_operation_lease_token.encode("utf-8")).hexdigest() != source.lease_sha256
    ):
        raise AuditIntegrityError("provider terminal hold source is no longer the running original")
    effect = conn.execute(
        select(effects.c.disposition, effects.c.provider_attempt_id).where(
            effects.c.session_id == source.session_id,
            effects.c.operation_id == source.operation_id,
            effects.c.effect_key == source.effect_key,
        )
    ).one()
    if effect.disposition != "prepared" or effect.provider_attempt_id is not None:
        raise AuditIntegrityError("provider terminal hold requires the original undispatched effect")
    if (
        conn.execute(
            select(holds.c.effect_key).where(
                holds.c.session_id == source.session_id,
                holds.c.operation_id == source.operation_id,
                holds.c.effect_key == source.effect_key,
            )
        ).one_or_none()
        is not None
    ):
        raise AuditIntegrityError("provider terminal hold already exists for this effect")
    admit_retained_payload_chunks(
        conn,
        session_id=source.session_id,
        operation_id=source.operation_id,
        prospective=prospective,
        retained_payload_limit_bytes=source.allowance_bytes,
        _new_hold_bytes=source.reserved_bytes,
    )
    if contract.policy_hash != source.policy_sha256:
        raise AuditIntegrityError("provider terminal hold policy changed during reservation")
    conn.execute(insert(holds).values(**asdict(source), provider_attempt_id=None, state="active", created_at=created_at, consumed_at=None))


def bind_provider_terminal_hold_attempt(conn: Connection, *, source: ProviderTerminalHoldSource, provider_attempt_id: str) -> None:
    """Bind exactly one paid physical attempt to the original active hold."""
    if not conn.in_transaction():
        raise AuditIntegrityError("provider terminal hold binding requires the original SQL transaction")
    if type(provider_attempt_id) is not str or not provider_attempt_id:
        raise ValueError("provider terminal hold requires a physical attempt identity")
    contract = _require_historical_hold_source(conn, source)
    row = conn.execute(
        select(holds.c.provider_attempt_id, holds.c.state).where(
            holds.c.session_id == source.session_id,
            holds.c.operation_id == source.operation_id,
            holds.c.effect_key == source.effect_key,
        )
    ).one_or_none()
    if row is None or row.state != "active" or row.provider_attempt_id is not None:
        raise AuditIntegrityError("provider terminal hold is missing, spent, or already bound")
    _require_hold_fields(conn, source=source, contract=contract)
    physical = conn.execute(
        select(
            provider_attempts.c.session_id,
            provider_attempts.c.source,
            provider_attempts.c.operation_id,
            provider_attempts.c.operation_epoch,
            provider_attempts.c.lease_token,
        ).where(provider_attempts.c.attempt_id == provider_attempt_id)
    ).one_or_none()
    if (
        physical is None
        or physical.session_id != source.session_id
        or physical.source not in ("composer", "auto_title")
        or physical.operation_id != source.session_operation_id
        or physical.operation_epoch != source.session_operation_epoch
        or sha256(physical.lease_token.encode("utf-8")).hexdigest() != source.lease_sha256
    ):
        raise AuditIntegrityError("provider terminal hold cannot bind a foreign physical attempt")
    effect_attempt = conn.execute(
        select(effects.c.provider_attempt_id).where(
            effects.c.session_id == source.session_id,
            effects.c.operation_id == source.operation_id,
            effects.c.effect_key == source.effect_key,
        )
    ).scalar_one()
    if effect_attempt != provider_attempt_id:
        raise AuditIntegrityError("provider terminal hold attempt differs from original effect")
    changed = conn.execute(
        update(holds)
        .where(
            holds.c.session_id == source.session_id,
            holds.c.operation_id == source.operation_id,
            holds.c.effect_key == source.effect_key,
            holds.c.state == "active",
            holds.c.provider_attempt_id.is_(None),
        )
        .values(provider_attempt_id=provider_attempt_id)
    )
    if changed.rowcount != 1:
        raise AuditIntegrityError("provider terminal hold attempt binding lost its active row")


def consume_provider_terminal_hold_after_complete_result(
    conn: Connection, *, source: ProviderTerminalHoldSource, provider_attempt_id: str, consumed_at: datetime
) -> None:
    """Release a hold only after its original complete paid result is durable.

    The caller must commit this update with its result append. Terminal paid
    overflow needs a distinct effect/cold-reader contract before its hold can
    be consumed; this function deliberately cannot classify that outcome.
    """
    if not conn.in_transaction():
        raise AuditIntegrityError("provider terminal hold consumption requires the original SQL transaction")
    if type(consumed_at) is not datetime or consumed_at.tzinfo is None:
        raise ValueError("provider terminal hold consumption requires an aware time")
    contract = _require_historical_hold_source(conn, source)
    row = _require_hold_fields(conn, source=source, contract=contract)
    if row.state != "active" or row.provider_attempt_id != provider_attempt_id:
        raise AuditIntegrityError("provider terminal hold cannot consume a missing or foreign paid attempt")
    effect = conn.execute(
        select(
            effects.c.disposition,
            effects.c.receipt_key,
            effects.c.result_chunk_key,
            effects.c.provider_attempt_id,
        ).where(
            effects.c.session_id == source.session_id,
            effects.c.operation_id == source.operation_id,
            effects.c.effect_key == source.effect_key,
        )
    ).one()
    physical = conn.execute(
        select(provider_attempts.c.settled_at, provider_attempts.c.ledger_entry_id).where(
            provider_attempts.c.attempt_id == provider_attempt_id
        )
    ).one_or_none()
    if (
        effect.disposition != "committed"
        or effect.provider_attempt_id != provider_attempt_id
        or type(effect.receipt_key) is not str
        or not effect.receipt_key
        or type(effect.result_chunk_key) is not str
        or not effect.result_chunk_key
        or physical is None
        or physical.settled_at is None
        or physical.ledger_entry_id is None
    ):
        raise AuditIntegrityError("provider terminal hold cannot be released before complete paid settlement")
    changed = conn.execute(
        update(holds)
        .where(
            holds.c.session_id == source.session_id,
            holds.c.operation_id == source.operation_id,
            holds.c.effect_key == source.effect_key,
            holds.c.state == "active",
            holds.c.provider_attempt_id == provider_attempt_id,
        )
        .values(state="consumed", consumed_at=consumed_at)
    )
    if changed.rowcount != 1:
        raise AuditIntegrityError("provider terminal hold consumption lost its active row")


def admit_retained_payload_chunks(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    prospective: tuple[ProspectiveContinuationChunk, ...],
    retained_payload_limit_bytes: int,
    consuming_hold: ProviderTerminalHoldSource | None = None,
    _new_hold_bytes: int = 0,
) -> None:
    """Check one complete proposed batch before its first write.

    Existing keys are free only after their actual bytes, digest, recorded
    length and codec match. Distinct keys count separately. All historical and
    off-head keys participate. The allowance covers payload bytes, not SQL row,
    index, WAL or backup space.
    """
    if type(retained_payload_limit_bytes) is not int or retained_payload_limit_bytes <= 0:
        raise ValueError("retained payload allowance requires an exact positive integer")
    if not conn.in_transaction():
        raise AuditIntegrityError("retained capacity admission requires the original SQL transaction")
    contract = load_continuation_read_contract(conn, session_id=session_id, operation_id=operation_id)
    if (
        contract.version in ("composer.continuation.policy.v3", "composer.continuation.policy.v4")
        and retained_payload_limit_bytes != contract.allowance
    ):
        raise AuditIntegrityError("bounded retained payload allowance differs from original admission")
    if type(_new_hold_bytes) is not int or _new_hold_bytes < 0:
        raise ValueError("new provider hold bytes must be an exact nonnegative integer")
    if _new_hold_bytes and contract.version != "composer.continuation.policy.v4":
        raise AuditIntegrityError("only V4 may reserve a paid-provider terminal hold")
    read_bound = contract.allowance if contract.version in ("composer.continuation.policy.v3", "composer.continuation.policy.v4") else None
    if conn.dialect.name == "postgresql":
        actual_length = func.octet_length(chunks.c.payload)
    elif conn.dialect.name == "sqlite":
        actual_length = func.length(cast(chunks.c.payload, LargeBinary))
    else:
        raise AuditIntegrityError("retained capacity admission has an unsupported database")

    new_by_key: dict[str, ProspectiveContinuationChunk] = {}
    for item in prospective:
        if (
            type(item) is not ProspectiveContinuationChunk
            or type(item.key) is not str
            or not item.key
            or type(item.payload) is not bytes
            or type(item.codec) is not str
            or not item.codec
        ):
            raise ValueError("prospective chunk requires exact bytes and owned codec/key")
        prior = new_by_key.get(item.key)
        if prior is not None and prior != item:
            raise AuditIntegrityError("prospective immutable chunk collision")
        new_by_key[item.key] = item

    used = 0
    stored_keys: list[str] = []
    existing_keys: set[str] = set()
    for row in conn.execute(
        select(chunks.c.chunk_key, chunks.c.byte_length, actual_length.label("actual_byte_length")).where(
            chunks.c.session_id == session_id,
            chunks.c.operation_id == operation_id,
        )
    ):
        if type(row.actual_byte_length) is not int or row.actual_byte_length < 0 or row.byte_length != row.actual_byte_length:
            raise AuditIntegrityError("retained chunk actual byte length differs from its recorded length")
        used += row.actual_byte_length
        stored_keys.append(row.chunk_key)
        if row.chunk_key in new_by_key:
            existing_keys.add(row.chunk_key)

    for key in existing_keys:
        item = new_by_key[key]
        if read_bound is not None:
            old_bounded = read_bounded_retained_chunk(conn, session_id=session_id, operation_id=operation_id, key=key, allowance=read_bound)
            if old_bounded is None or old_bounded != item:
                raise AuditIntegrityError("immutable chunk collision")
        else:
            old = conn.execute(
                select(chunks).where(
                    chunks.c.session_id == session_id,
                    chunks.c.operation_id == operation_id,
                    chunks.c.chunk_key == key,
                )
            ).one()
            if (
                type(old.payload) is not bytes
                or old.payload != item.payload
                or old.sha256 != sha256(old.payload).hexdigest()
                or old.sha256 != sha256(item.payload).hexdigest()
                or old.byte_length != len(item.payload)
                or old.codec != item.codec
            ):
                raise AuditIntegrityError("immutable chunk collision")

    added = sum(len(item.payload) for key, item in new_by_key.items() if key not in existing_keys)
    live_holds = _live_provider_hold_bytes(conn, session_id=session_id, operation_id=operation_id, contract=contract)
    hold_credit = 0
    if consuming_hold is not None:
        hold_credit = _require_active_provider_hold(conn, source=consuming_hold, contract=contract)
        if consuming_hold.session_id != session_id or consuming_hold.operation_id != operation_id:
            raise AuditIntegrityError("provider hold credit belongs to a different operation")
    if used + live_holds - hold_credit + _new_hold_bytes + added > retained_payload_limit_bytes:
        # An ordinary fresh overflow must not conceal a corrupt old digest.
        # Fetch one row at a time; never materialize the entire history here.
        for key in stored_keys:
            if read_bound is not None:
                if (
                    read_bounded_retained_chunk(conn, session_id=session_id, operation_id=operation_id, key=key, allowance=read_bound)
                    is None
                ):
                    raise AuditIntegrityError("retained chunk disappeared during capacity admission")
            else:
                row = conn.execute(
                    select(chunks.c.payload, chunks.c.sha256, chunks.c.codec, chunks.c.byte_length).where(
                        chunks.c.session_id == session_id,
                        chunks.c.operation_id == operation_id,
                        chunks.c.chunk_key == key,
                    )
                ).one()
                if (
                    type(row.payload) is not bytes
                    or type(row.codec) is not str
                    or not row.codec
                    or len(row.payload) != row.byte_length
                    or sha256(row.payload).hexdigest() != row.sha256
                ):
                    raise AuditIntegrityError("retained chunk digest or binding mismatch")
        raise ContinuationRetainedCapacityExceeded()
