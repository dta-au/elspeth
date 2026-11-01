"""Transaction-local admission of exact retained Continuation chunk payload bytes.

The caller owns the original session fence, lock and selected-root CAS. This
helper observes every key in that same transaction; it grants no authority to
start a new operation or to resume paid work after a capacity refusal.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy import Connection, LargeBinary, case, cast, func, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks


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


def admit_retained_payload_chunks(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    prospective: tuple[ProspectiveContinuationChunk, ...],
    retained_payload_limit_bytes: int,
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
    if contract.version == "composer.continuation.policy.v3" and retained_payload_limit_bytes != contract.allowance:
        raise AuditIntegrityError("V3 retained payload allowance differs from original admission")
    read_bound = contract.allowance if contract.version == "composer.continuation.policy.v3" else None
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
    if used + added > retained_payload_limit_bytes:
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
