"""Pure values owned by exactly one continuation retention-stage invocation.

The ambient variable avoids threading a memo through every required-SQL helper.
Captured stage work enters a fresh Context containing only this variable: caller
prompt scopes, session-lock tokens and other ContextVars must stay outside it.
SQL/admission results, clearance objects and live application owners have no
entry family here. The action clock opens one carrier for each retention run.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import Context, ContextVar
from dataclasses import dataclass
from threading import Lock
from types import FunctionType
from typing import TYPE_CHECKING, Literal, cast

from elspeth.contracts.credential_material import (
    CREDENTIAL_DETECTOR_VERSION,
    CredentialMaterialFinding,
    CredentialTraversalPolicy,
    find_credential_material,
)

if TYPE_CHECKING:
    from elspeth.web.composer.continuation import ContinuationFrameStack, PrivateChunkRef


@dataclass(frozen=True, slots=True)
class SnapshotDecodeKey:
    encoded: str


@dataclass(frozen=True, slots=True)
class PrivateChunkVerificationKey:
    reference: PrivateChunkRef
    canonical: str


type CredentialInputKind = Literal["null", "bool", "int", "float", "string", "mapping", "mapping_proxy", "list", "tuple", "end"]


@dataclass(frozen=True, slots=True)
class CredentialInputToken:
    """One ordered, typed token from a bounded immutable input snapshot.

    Container delimiters preserve shape and mapping order. Scalar spellings
    preserve their exact type/value; a string containing JSON remains a string.
    The real scan adapter must decline caching unsupported or over-bound input.
    """

    kind: CredentialInputKind
    value: str


@dataclass(frozen=True, slots=True)
class CredentialPolicyKey:
    env_ref_names: frozenset[str]
    additional_credential_fields: frozenset[str]
    max_depth: int
    max_nodes: int
    max_width: int
    max_string_bytes: int
    detector_version: str

    @classmethod
    def capture(cls, policy: CredentialTraversalPolicy) -> CredentialPolicyKey:
        return cls(
            frozenset(policy.env_ref_names),
            frozenset(policy.additional_credential_fields),
            policy.max_depth,
            policy.max_nodes,
            policy.max_width,
            policy.max_string_bytes,
            CREDENTIAL_DETECTOR_VERSION,
        )


type CredentialScannerRole = Literal["generic", "field_mapper_projection_prevalidation", "state_projection", "state_component"]
type CredentialDetector = Callable[[object, CredentialTraversalPolicy], CredentialMaterialFinding | None]


@dataclass(frozen=True, slots=True)
class CredentialScanKey:
    scanner_role: CredentialScannerRole
    surface: str
    content: tuple[CredentialInputToken, ...]
    policy: CredentialPolicyKey
    detector: CredentialDetector = find_credential_material


@dataclass(frozen=True, slots=True)
class StateCredentialStepKey:
    """DATA for one original constructor-owned options inspection step.

    This is not an aggregate state clearance. Policy resolution and all other
    steps remain at their original sequential points in the state guard.
    """

    scan: CredentialScanKey


_MISSING = object()


def _snapshot_value(key: SnapshotDecodeKey, producer: Callable[[str], ContinuationFrameStack]) -> ContinuationFrameStack:
    from elspeth.web.composer.continuation import ContinuationFrameStack

    value = producer(key.encoded)
    if type(value) is not ContinuationFrameStack:
        raise TypeError("Snapshot memo cannot retain a foreign result")
    return value


def _chunk_value(key: PrivateChunkVerificationKey, producer: Callable[[str], None]) -> None:
    producer(key.canonical)


def _finding_value(value: CredentialMaterialFinding | None) -> CredentialMaterialFinding | None:
    if value is not None and type(value) is not CredentialMaterialFinding:
        raise TypeError("Credential memo cannot retain a foreign result")
    return value


def _require_function_producer(producer: Callable[..., object]) -> None:
    if type(producer) is not FunctionType:
        raise TypeError("Memo requires a pure function producer")


class RetentionStageMemo:
    """Four closed value families; close and miss publication share one lock.

    Sequential awaited Futures establish visibility between ordinary hops.
    The lock additionally protects overlapping publication/closure; computation
    runs outside it, and a late result cannot repopulate a closed carrier.
    Each key includes the actual producer identity as well as complete inputs.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._closed = False
        self._snapshots: dict[tuple[Callable[[str], ContinuationFrameStack], SnapshotDecodeKey], ContinuationFrameStack] = {}
        self._chunks: dict[tuple[Callable[[str], None], PrivateChunkVerificationKey], None] = {}
        self._credentials: dict[
            tuple[Callable[[CredentialScanKey], CredentialMaterialFinding | None], CredentialScanKey], CredentialMaterialFinding | None
        ] = {}
        self._states: dict[
            tuple[Callable[[StateCredentialStepKey], CredentialMaterialFinding | None], StateCredentialStepKey],
            CredentialMaterialFinding | None,
        ] = {}

    @property
    def active(self) -> bool:
        with self._lock:
            return not self._closed

    @property
    def cached_entry_count(self) -> int:
        with self._lock:
            return len(self._snapshots) + len(self._chunks) + len(self._credentials) + len(self._states)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._snapshots.clear()
            self._chunks.clear()
            self._credentials.clear()
            self._states.clear()

    def _lookup[K, V](self, entries: dict[K, V], key: K, compute: Callable[[], V]) -> V:
        with self._lock:
            closed = self._closed
            hit = entries.get(key, _MISSING) if not closed else _MISSING
        if hit is not _MISSING:
            return cast(V, hit)
        value = compute()
        with self._lock:
            if self._closed:
                return value
            return entries.setdefault(key, value)

    def snapshot(self, key: SnapshotDecodeKey, producer: Callable[[str], ContinuationFrameStack]) -> ContinuationFrameStack:
        if type(key) is not SnapshotDecodeKey:
            raise TypeError("Snapshot memo requires its exact key")
        _require_function_producer(producer)
        return self._lookup(self._snapshots, (producer, key), lambda: _snapshot_value(key, producer))

    def private_chunk(self, key: PrivateChunkVerificationKey, producer: Callable[[str], None]) -> None:
        if type(key) is not PrivateChunkVerificationKey:
            raise TypeError("Chunk memo requires its exact key")
        _require_function_producer(producer)
        self._lookup(self._chunks, (producer, key), lambda: _chunk_value(key, producer))

    def credential(
        self, key: CredentialScanKey, producer: Callable[[CredentialScanKey], CredentialMaterialFinding | None]
    ) -> CredentialMaterialFinding | None:
        if type(key) is not CredentialScanKey:
            raise TypeError("Credential memo requires its exact key")
        _require_function_producer(producer)
        return self._lookup(self._credentials, (producer, key), lambda: _finding_value(producer(key)))

    def state_credentials(
        self, key: StateCredentialStepKey, producer: Callable[[StateCredentialStepKey], CredentialMaterialFinding | None]
    ) -> CredentialMaterialFinding | None:
        if type(key) is not StateCredentialStepKey:
            raise TypeError("State memo requires its exact key")
        _require_function_producer(producer)
        return self._lookup(self._states, (producer, key), lambda: _finding_value(producer(key)))


_RETENTION_STAGE_MEMO: ContextVar[RetentionStageMemo | None] = ContextVar("continuation_retention_stage_memo", default=None)


def current_retention_stage_memo() -> RetentionStageMemo | None:
    memo = _RETENTION_STAGE_MEMO.get()
    return memo if memo is not None and memo.active else None


@contextmanager
def retention_stage_memo_scope() -> Iterator[RetentionStageMemo]:
    memo = RetentionStageMemo()
    token = _RETENTION_STAGE_MEMO.set(memo)
    try:
        yield memo
    finally:
        memo.close()
        _RETENTION_STAGE_MEMO.reset(token)


@contextmanager
def without_retention_stage_memo() -> Iterator[None]:
    """Mask an outer stage while other work runs, without closing its carrier."""
    token = _RETENTION_STAGE_MEMO.set(None)
    try:
        yield
    finally:
        _RETENTION_STAGE_MEMO.reset(token)


def prepare_retention_sql_callable[**P, T](func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> Callable[[], T]:
    """Capture only the stage memo before submission; enter around the whole SQL.

    With a captured carrier a Context is created at every actual invocation, so
    even a reused adapter cannot enter one Context concurrently. Without one the
    ordinary partial is unchanged. Session lock set/guard and reset remain
    inside the original complete callable and same Context.
    """
    memo = current_retention_stage_memo()
    if memo is None:
        return functools.partial(func, *args, **kwargs)

    def invoke() -> T:
        def run() -> T:
            if not memo.active:
                return func(*args, **kwargs)
            token = _RETENTION_STAGE_MEMO.set(memo)
            try:
                return func(*args, **kwargs)
            finally:
                _RETENTION_STAGE_MEMO.reset(token)

        return Context().run(run)

    return invoke
