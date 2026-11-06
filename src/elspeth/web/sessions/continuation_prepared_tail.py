"""Exact transaction-local capacity admission for a complete prepared V4 tail.

This owner admits bytes, not tool execution. The caller still proves the
original provider call and every actual SQL issue/publication source. A scope
must consume the entire admitted append chain in the same transaction.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import Connection
from sqlalchemy.engine import RootTransaction

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_policy import (
    ContinuationAdmissionPolicyV4,
    decode_continuation_policy,
)
from elspeth.web.sessions.continuation import (
    PreparedContinuationAppend,
    StoredContinuationSnapshot,
    continuation_unpaid_terminal_reserve_on_connection,
    load_snapshot,
)
from elspeth.web.sessions.continuation_retained_capacity import ProspectiveContinuationChunk, admit_retained_payload_chunks

_SCOPE_KEY = "elspeth.prepared_complete_v3_tail"


@dataclass(frozen=True, slots=True)
class PreparedTailPrefix:
    append: PreparedContinuationAppend
    chunks: tuple[ProspectiveContinuationChunk, ...]


def prepared_append_chunks(append: PreparedContinuationAppend) -> tuple[ProspectiveContinuationChunk, ...]:
    append.require_original()
    return (
        *(
            ProspectiveContinuationChunk(f"event:{append.expected_cursor + index}", event.payload_json.encode("utf-8"), "event.v2")
            for index, event in enumerate(append.events, 1)
        ),
        ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
    )


@dataclass(slots=True)
class _ConsumingTail:
    connection: Connection
    transaction: RootTransaction
    session_id: str
    operation_id: str
    prefixes: tuple[PreparedTailPrefix, ...]
    allowed: dict[str, ProspectiveContinuationChunk]
    next_append: int = 0


def _current(conn: Connection, session_id: str, operation_id: str) -> _ConsumingTail | None:
    owner = conn.info.get(_SCOPE_KEY)
    if owner is None:
        return None
    if (
        type(owner) is not _ConsumingTail
        or owner.connection is not conn
        or owner.transaction is not conn.get_transaction()
        or not owner.transaction.is_active
        or (owner.session_id, owner.operation_id) != (session_id, operation_id)
    ):
        raise AuditIntegrityError("prepared tail crossed its original transaction or operation")
    return owner


def require_prepared_tail_chunk(conn: Connection, *, session_id: str, operation_id: str, candidate: ProspectiveContinuationChunk) -> bool:
    owner = _current(conn, session_id, operation_id)
    if owner is None:
        return False
    if owner.allowed.get(candidate.key) != candidate:
        raise AuditIntegrityError("prepared tail writer introduced bytes outside its admitted complete plan")
    return True


def require_prepared_tail_append(
    conn: Connection, *, session_id: str, operation_id: str, append: PreparedContinuationAppend | None
) -> bool:
    owner = _current(conn, session_id, operation_id)
    if owner is None:
        return False
    if append is None or owner.next_append >= len(owner.prefixes) or owner.prefixes[owner.next_append].append != append:
        raise AuditIntegrityError("prepared tail writer changed its next admitted append")
    append.require_original()
    return True


def record_prepared_tail_append(conn: Connection, *, session_id: str, operation_id: str, append: PreparedContinuationAppend | None) -> None:
    owner = _current(conn, session_id, operation_id)
    if owner is not None:
        require_prepared_tail_append(conn, session_id=session_id, operation_id=operation_id, append=append)
        owner.next_append += 1


@contextmanager
def admit_prepared_complete_tail_on_connection(
    conn: Connection,
    *,
    original: StoredContinuationSnapshot,
    prefixes: tuple[PreparedTailPrefix, ...],
) -> Iterator[None]:
    if type(original) is not StoredContinuationSnapshot or type(prefixes) is not tuple or not prefixes or _SCOPE_KEY in conn.info:
        raise AuditIntegrityError("prepared complete tail requires its sole nonempty transaction plan")
    for prefix in prefixes:
        if (
            type(prefix) is not PreparedTailPrefix
            or type(prefix.append) is not PreparedContinuationAppend
            or type(prefix.chunks) is not tuple
            or any(type(chunk) is not ProspectiveContinuationChunk for chunk in prefix.chunks)
        ):
            raise AuditIntegrityError("prepared complete tail requires nominal immutable plan members")
    transaction = conn.get_transaction()
    if type(transaction) is not RootTransaction or not transaction.is_active:
        raise AuditIntegrityError("prepared complete tail requires its original active transaction")
    root = decode_continuation_snapshot(original.payload_json)
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    if type(policy) is not ContinuationAdmissionPolicyV4:
        raise AuditIntegrityError("prepared complete tail requires its original bounded policy")
    sid, operation = root.logical.session_id, root.logical.operation_id
    if load_snapshot(conn, session_id=sid, operation_id=operation) != original:
        raise AuditIntegrityError("prepared complete tail lost its original SQL head")
    preceding = original
    allowed: dict[str, ProspectiveContinuationChunk] = {}
    for prefix in prefixes:
        append = prefix.append
        append.require_original()
        if (
            (append.session_id, append.operation_id) != (sid, operation)
            or append.expected_checkpoint_id != preceding.checkpoint_id
            or append.expected_cursor != preceding.cursor
            or append.predecessor_json != preceding.payload_json
            or append.handoff
        ):
            raise AuditIntegrityError("prepared complete tail changed its original append chain")
        following = decode_continuation_snapshot(append.snapshot_json)
        if (
            following.logical != root.logical
            or following.mutation_ceiling != root.mutation_ceiling
            or following.policy_hash != root.policy_hash
            or following.admission_policy_json != root.admission_policy_json
            or following.provider_cost_policy != root.provider_cost_policy
            or following.cursor != preceding.cursor + len(append.events)
        ):
            raise AuditIntegrityError("prepared complete tail changed immutable source or cursor")
        for candidate in (*prefix.chunks, *prepared_append_chunks(append)):
            old = allowed.get(candidate.key)
            if old is not None and old != candidate:
                raise AuditIntegrityError("prepared complete tail contains an immutable-key collision")
            allowed[candidate.key] = candidate
        admit_retained_payload_chunks(
            conn,
            session_id=sid,
            operation_id=operation,
            prospective=tuple(allowed.values()),
            retained_payload_limit_bytes=policy.max_retained_payload_bytes,
        )
        preceding = StoredContinuationSnapshot(append.snapshot_json, following.cursor, append.checkpoint_id)
    final = decode_continuation_snapshot(preceding.payload_json)
    if (
        final.pending_owner_results
        or final.pending_settlement_ids
        or any(entry.disposition not in ("committed", "refused") for entry in final.effect_frontier)
    ):
        raise AuditIntegrityError("prepared complete tail leaves an unresolved effect after its transaction")
    reserve = continuation_unpaid_terminal_reserve_on_connection(
        conn, session_id=sid, operation_id=operation, post_root_json=preceding.payload_json, post_cursor=preceding.cursor
    )
    admit_retained_payload_chunks(
        conn,
        session_id=sid,
        operation_id=operation,
        prospective=(*allowed.values(), *reserve),
        retained_payload_limit_bytes=policy.max_retained_payload_bytes,
    )
    owner = _ConsumingTail(conn, transaction, sid, operation, prefixes, allowed)
    conn.info[_SCOPE_KEY] = owner
    try:
        yield
        if owner.next_append != len(prefixes) or load_snapshot(conn, session_id=sid, operation_id=operation) != preceding:
            raise AuditIntegrityError("prepared complete tail did not consume its entire admitted transaction")
    except BaseException:
        transaction.rollback()
        raise
    finally:
        if conn.info.pop(_SCOPE_KEY, None) is not owner:
            raise AuditIntegrityError("prepared complete tail lost its original transaction owner")
