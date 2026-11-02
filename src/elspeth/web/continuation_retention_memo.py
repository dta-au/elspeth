"""Pure values owned by one bounded continuation read or retention invocation.

The ambient variable avoids threading a memo through every required-SQL helper.
Captured stage work enters a fresh Context containing only this variable: caller
prompt scopes, session-lock tokens and other ContextVars must stay outside it.
SQL/admission results, clearance objects and live application owners have no
entry family here. The action clock opens one carrier for each retention run.
Each supported DATA read opens a fresh carrier inside its synchronous SQL
callable, closing it on return or error and restoring any surrounding scope.
"""

from __future__ import annotations

import functools
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import Context, ContextVar
from dataclasses import dataclass
from hashlib import sha256 as _TRUSTED_SHA256
from threading import Lock
from types import BuiltinFunctionType, FunctionType
from typing import TYPE_CHECKING, Literal, Protocol, cast

from elspeth.contracts.bounded_serialization import utf8_size
from elspeth.contracts.credential_material import (
    CREDENTIAL_DETECTOR_VERSION,
    CredentialMaterialFinding,
    CredentialTraversalPolicy,
    find_credential_material,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_limits import (
    CONTINUATION_LEGACY_ADVISOR_TEXT_MEMO_MAX_RETAINED_BYTES,
    CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
)

if TYPE_CHECKING:
    from elspeth.web.composer.continuation import ContinuationFrameStack, PrivateChunkRef
    from elspeth.web.composer.continuation_clock import ContinuationActionClock


@dataclass(frozen=True, slots=True)
class SnapshotDecodeKey:
    encoded: str


@dataclass(frozen=True, slots=True)
class PrivateChunkVerificationKey:
    reference: PrivateChunkRef
    canonical: str


@dataclass(frozen=True, slots=True)
class CanonicalTextEncodingKey:
    canonical: str
    max_utf8_bytes: int
    reference: PrivateChunkRef | None = None


@dataclass(frozen=True, slots=True)
class CanonicalTextProof:
    encoded: bytes
    digest: str
    retained_bytes: int


class _DigestResult(Protocol):
    def hexdigest(self) -> str: ...


@dataclass(frozen=True, slots=True)
class RecorderBlockHashKey:
    expected_digest: str
    canonical: str


@dataclass(frozen=True, slots=True)
class RecorderRequestTextKey:
    canonical: str
    cache_bound_bytes: int


@dataclass(frozen=True, slots=True)
class RecorderRequestTextProof:
    ref_document_json: str
    blocks: tuple[tuple[str, str], ...]


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
    """Closed value families; close and miss publication share one lock.

    Sequential awaited Futures establish visibility between ordinary hops.
    The lock additionally protects overlapping publication/closure; computation
    runs outside it, and a late result cannot repopulate a closed carrier.
    Each key includes the actual producer identity as well as complete inputs.
    """

    _legacy_advisor_owner: ContinuationActionClock | None
    _text_parent: RetentionStageMemo | None

    def __init__(
        self,
        *,
        legacy_advisor_owner: ContinuationActionClock | None = None,
        text_parent: RetentionStageMemo | None = None,
    ) -> None:
        if legacy_advisor_owner is not None:
            from elspeth.web.composer.continuation_clock import ContinuationActionClock

            if type(legacy_advisor_owner) is not ContinuationActionClock:
                raise TypeError("Legacy Advisor memo requires its exact action clock")
        if text_parent is not None and (
            type(text_parent) is not RetentionStageMemo
            or text_parent._legacy_advisor_owner is None
            or not text_parent.active
            or legacy_advisor_owner is not None
        ):
            raise AuditIntegrityError("Nested Advisor memo requires one active text-only preparation owner")
        self._legacy_advisor_owner = legacy_advisor_owner
        self._text_parent = text_parent
        self._shared_text_budget_bytes = (
            CONTINUATION_LEGACY_ADVISOR_TEXT_MEMO_MAX_RETAINED_BYTES
            if legacy_advisor_owner is not None
            else CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
        )
        self._lock = Lock()
        self._closed = False
        self._snapshots: dict[tuple[Callable[[str], ContinuationFrameStack], SnapshotDecodeKey], ContinuationFrameStack] = {}
        self._chunks: dict[tuple[Callable[[str], None], PrivateChunkVerificationKey], None] = {}
        self._canonical_text: tuple[Callable[[CanonicalTextEncodingKey], bytes], CanonicalTextEncodingKey, bytes] | None = None
        self._canonical_proofs: OrderedDict[
            tuple[Callable[[CanonicalTextEncodingKey], bytes], Callable[[bytes], _DigestResult], CanonicalTextEncodingKey],
            CanonicalTextProof,
        ] = OrderedDict()
        self._canonical_proof_bytes = 0
        self._recorder_block_hashes: OrderedDict[tuple[Callable[[RecorderBlockHashKey], object], RecorderBlockHashKey], int] = OrderedDict()
        self._recorder_block_hash_bytes = 0
        self._recorder_request: (
            tuple[Callable[[RecorderRequestTextKey], RecorderRequestTextProof | None], RecorderRequestTextKey, RecorderRequestTextProof]
            | None
        ) = None
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
            return (
                len(self._snapshots)
                + len(self._chunks)
                + len(self._credentials)
                + len(self._states)
                + int(self._canonical_text is not None)
                + len(self._canonical_proofs)
                + len(self._recorder_block_hashes)
                + int(self._recorder_request is not None)
            )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._snapshots.clear()
            self._chunks.clear()
            self._canonical_text = None
            self._canonical_proofs.clear()
            self._canonical_proof_bytes = 0
            self._recorder_block_hashes.clear()
            self._recorder_block_hash_bytes = 0
            self._recorder_request = None
            self._credentials.clear()
            self._states.clear()
            self._text_parent = None

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
        if self._legacy_advisor_owner is not None:
            return _snapshot_value(key, producer)
        return self._lookup(self._snapshots, (producer, key), lambda: _snapshot_value(key, producer))

    def private_chunk(self, key: PrivateChunkVerificationKey, producer: Callable[[str], None]) -> None:
        if type(key) is not PrivateChunkVerificationKey:
            raise TypeError("Chunk memo requires its exact key")
        _require_function_producer(producer)
        if self._legacy_advisor_owner is not None:
            return _chunk_value(key, producer)
        self._lookup(self._chunks, (producer, key), lambda: _chunk_value(key, producer))

    def canonical_text_encoding(self, key: CanonicalTextEncodingKey, producer: Callable[[CanonicalTextEncodingKey], bytes]) -> bytes:
        """Retain only one immutable exact-text proof, never its parsed graph."""
        with self._lock:
            parent = self._text_parent if not self._closed else None
        if parent is not None:
            return parent.canonical_text_encoding(key, producer)
        if (
            type(key) is not CanonicalTextEncodingKey
            or type(key.canonical) is not str
            or type(key.max_utf8_bytes) is not int
            or key.max_utf8_bytes < 1
        ):
            raise TypeError("Canonical text memo requires its exact bounded key")
        _require_function_producer(producer)
        with self._lock:
            entry = self._canonical_text if not self._closed else None
            if entry is not None and entry[0] is producer and entry[1] == key:
                return entry[2]
        value = producer(key)
        if type(value) is not bytes or len(value) > key.max_utf8_bytes or value != key.canonical.encode("utf-8"):
            raise TypeError("Canonical text memo requires exact immutable bytes")
        with self._lock:
            if not self._closed and len(value) <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
                entry = self._canonical_text
                if entry is not None and entry[0] is producer and entry[1] == key:
                    return entry[2]
                self._canonical_proofs.clear()
                self._canonical_proof_bytes = 0
                self._recorder_block_hashes.clear()
                self._recorder_block_hash_bytes = 0
                self._canonical_text = (producer, key, value)
        return value

    def canonical_text_proof(
        self,
        key: CanonicalTextEncodingKey,
        producer: Callable[[CanonicalTextEncodingKey], bytes],
        digest_producer: Callable[[bytes], _DigestResult],
    ) -> CanonicalTextProof:
        """Reuse only exact immutable canonical bytes and SHA, within one 1 MiB stage budget."""
        with self._lock:
            parent = self._text_parent if not self._closed else None
        if parent is not None:
            return parent.canonical_text_proof(key, producer, digest_producer)
        if (
            type(key) is not CanonicalTextEncodingKey
            or type(key.canonical) is not str
            or type(key.max_utf8_bytes) is not int
            or key.max_utf8_bytes < 1
        ):
            raise TypeError("Canonical text proof requires its exact bounded key")
        if key.reference is not None:
            from elspeth.web.composer.continuation import PrivateChunkRef

            if type(key.reference) is not PrivateChunkRef:
                raise TypeError("Canonical text proof requires its exact nominal reference")
        _require_function_producer(producer)
        if type(digest_producer) not in (FunctionType, BuiltinFunctionType):
            raise TypeError("Canonical text proof requires its exact digest producer")
        cache_key = (producer, digest_producer, key)
        trusted_digest = digest_producer is _TRUSTED_SHA256
        with self._lock:
            hit = self._canonical_proofs.get(cache_key) if trusted_digest and not self._closed else None
            if hit is not None:
                self._canonical_proofs.move_to_end(cache_key)
                return hit
        encoded = producer(key)
        if type(encoded) is not bytes or encoded != key.canonical.encode("utf-8"):
            raise TypeError("Canonical text proof requires exact immutable bytes")
        digest = digest_producer(encoded).hexdigest()
        if type(digest) is not str or len(digest) != 64:
            raise TypeError("Canonical text proof requires its exact digest")
        reference = key.reference
        reference_bytes = (
            len(reference.codec.encode("utf-8")) + len(reference.digest.encode("utf-8")) + len(str(reference.byte_length))
            if reference is not None
            else 0
        )
        size = len(encoded) * 2 + len(digest) + reference_bytes + len(str(key.max_utf8_bytes))
        proof = CanonicalTextProof(encoded, digest, size)
        # Count the retained text key, encoded bytes and digest, including
        # distinct producer/bound entries; evict oldest proofs on pressure.
        with self._lock:
            if (
                not trusted_digest
                or self._closed
                or len(encoded) > key.max_utf8_bytes
                or size > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
            ):
                return proof
            hit = self._canonical_proofs.get(cache_key)
            if hit is not None:
                self._canonical_proofs.move_to_end(cache_key)
                return hit
            self._canonical_text = None
            while self._canonical_proof_bytes + size > self._shared_text_budget_bytes:
                _, evicted = self._canonical_proofs.popitem(last=False)
                self._canonical_proof_bytes -= evicted.retained_bytes
            while self._canonical_proof_bytes + self._recorder_block_hash_bytes + size > self._shared_text_budget_bytes:
                _, evicted_size = self._recorder_block_hashes.popitem(last=False)
                self._recorder_block_hash_bytes -= evicted_size
            self._canonical_proofs[cache_key] = proof
            self._canonical_proof_bytes += size
        return proof

    def recorder_block_hash(self, key: RecorderBlockHashKey, producer: Callable[[RecorderBlockHashKey], object]) -> None:
        """Retain one exact immutable block address proof; callers still parse/store fresh."""
        with self._lock:
            parent = self._text_parent if not self._closed else None
        if parent is not None:
            return parent.recorder_block_hash(key, producer)
        if type(key) is not RecorderBlockHashKey or type(key.expected_digest) is not str or type(key.canonical) is not str:
            raise TypeError("Recorder block hash memo requires its exact text key")
        _require_function_producer(producer)
        cache_key = (producer, key)
        with self._lock:
            if not self._closed and cache_key in self._recorder_block_hashes:
                self._recorder_block_hashes.move_to_end(cache_key)
                return
        if producer(key) is not None:
            raise TypeError("Recorder block hash producer must return no mutable value")
        size = len(key.expected_digest.encode("utf-8")) + len(key.canonical.encode("utf-8"))
        with self._lock:
            if self._closed or size > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
                return
            if cache_key in self._recorder_block_hashes:
                self._recorder_block_hashes.move_to_end(cache_key)
                return
            self._canonical_text = None
            while self._canonical_proof_bytes + self._recorder_block_hash_bytes + size > self._shared_text_budget_bytes:
                if self._recorder_block_hashes:
                    _, evicted_size = self._recorder_block_hashes.popitem(last=False)
                    self._recorder_block_hash_bytes -= evicted_size
                else:
                    _, evicted_proof = self._canonical_proofs.popitem(last=False)
                    self._canonical_proof_bytes -= evicted_proof.retained_bytes
            self._recorder_block_hashes[cache_key] = size
            self._recorder_block_hash_bytes += size

    def recorder_request_text(
        self, key: RecorderRequestTextKey, producer: Callable[[RecorderRequestTextKey], RecorderRequestTextProof | None]
    ) -> RecorderRequestTextProof | None:
        """Retain one immutable prepared-request projection under its complete byte budget."""
        with self._lock:
            parent = self._text_parent if not self._closed else None
        if parent is not None:
            return parent.recorder_request_text(key, producer)
        if (
            type(key) is not RecorderRequestTextKey
            or type(key.canonical) is not str
            or type(key.cache_bound_bytes) is not int
            or key.cache_bound_bytes < 1
        ):
            raise TypeError("Recorder request memo requires its exact bounded key")
        _require_function_producer(producer)
        with self._lock:
            entry = self._recorder_request if not self._closed else None
            if entry is not None and entry[0] is producer and entry[1] == key:
                return entry[2]
        value = producer(key)
        if value is None:
            return None
        if (
            type(value) is not RecorderRequestTextProof
            or type(value.ref_document_json) is not str
            or type(value.blocks) is not tuple
            or any(
                type(block) is not tuple or len(block) != 2 or type(block[0]) is not str or type(block[1]) is not str
                for block in value.blocks
            )
        ):
            raise TypeError("Recorder request memo requires exact immutable text proof")
        remaining = min(key.cache_bound_bytes, CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES)
        for item in (key.canonical, value.ref_document_json, *(part for block in value.blocks for part in block)):
            size = utf8_size(item, limit=remaining)
            remaining -= size
            if remaining < 0:
                return value
        with self._lock:
            if not self._closed:
                entry = self._recorder_request
                if entry is not None and entry[0] is producer and entry[1] == key:
                    return entry[2]
                self._recorder_request = (producer, key, value)
        return value

    def credential(
        self, key: CredentialScanKey, producer: Callable[[CredentialScanKey], CredentialMaterialFinding | None]
    ) -> CredentialMaterialFinding | None:
        if type(key) is not CredentialScanKey:
            raise TypeError("Credential memo requires its exact key")
        _require_function_producer(producer)
        if self._legacy_advisor_owner is not None:
            return _finding_value(producer(key))
        return self._lookup(self._credentials, (producer, key), lambda: _finding_value(producer(key)))

    def state_credentials(
        self, key: StateCredentialStepKey, producer: Callable[[StateCredentialStepKey], CredentialMaterialFinding | None]
    ) -> CredentialMaterialFinding | None:
        if type(key) is not StateCredentialStepKey:
            raise TypeError("State memo requires its exact key")
        _require_function_producer(producer)
        if self._legacy_advisor_owner is not None:
            return _finding_value(producer(key))
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
def legacy_advisor_preparation_memo_scope(clock: ContinuationActionClock) -> Iterator[RetentionStageMemo]:
    """Share only immutable text proofs across one legacy Advisor preparation."""
    if current_retention_stage_memo() is not None:
        raise AuditIntegrityError("Legacy Advisor preparation cannot borrow an unrelated memo")
    memo = RetentionStageMemo(legacy_advisor_owner=clock)
    token = _RETENTION_STAGE_MEMO.set(memo)
    try:
        yield memo
    finally:
        memo.close()
        _RETENTION_STAGE_MEMO.reset(token)


def current_legacy_advisor_preparation_memo(clock: ContinuationActionClock) -> RetentionStageMemo | None:
    memo = current_retention_stage_memo()
    if memo is None or memo._legacy_advisor_owner is None:
        return None
    if memo._legacy_advisor_owner is not clock:
        raise AuditIntegrityError("Legacy Advisor preparation changed its original action clock")
    return memo


def finish_legacy_advisor_preparation_memo(clock: ContinuationActionClock) -> None:
    """Seal the optional legacy carrier at the existing pre-SDK clock cut."""
    memo = current_legacy_advisor_preparation_memo(clock)
    if memo is not None:
        memo.close()


@contextmanager
def borrow_legacy_advisor_inflight_memo(memo: RetentionStageMemo | None, clock: ContinuationActionClock | None) -> Iterator[None]:
    """Keep one fresh full SQL proof while borrowing only exact parent text."""
    if memo is None:
        with retention_stage_memo_scope():
            yield
        return
    if (
        type(memo) is not RetentionStageMemo
        or clock is None
        or memo._legacy_advisor_owner is not clock
        or current_legacy_advisor_preparation_memo(clock) is not memo
    ):
        raise AuditIntegrityError("Semantic in-flight proof cannot borrow a foreign preparation memo")
    child = RetentionStageMemo(text_parent=memo)
    token = _RETENTION_STAGE_MEMO.set(child)
    try:
        yield
    finally:
        child.close()
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
