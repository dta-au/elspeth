"""Short-lived, bounded, per-process memo for advisory ``validate_state`` results.

``POST /validate`` and ``GET /audit-readiness`` both run the full semantic
graph validation for the same composition version within a second or two of
each other (often concurrently: the frontend fires both on a version change).
This memo collapses that pair into one inner validation while keeping both
callers on ``ExecutionServiceImpl.validate_state`` unchanged.

Scope and lifetime (governing rule: cache by lifetime, never across one):

* The key names every input the inner validation consumes that can change its
  result and that is representable as a value — see ``ValidationMemoKey``.
  ``user_id`` is a key component, so a result can never be served to another
  principal by construction.
* Live blob lookups are not representable in a key. The inner run records
  each blob record it consulted (``BlobReadLog``); a hit is served only after
  the caller re-reads those records under its own session-operation context
  and finds them unchanged. A run whose blob content read failed, or which
  saw one blob two different ways, is not stored.
* Entries live ``ttl_seconds`` after completion on a monotonic process clock.
  This is a performance memo for advisory verdicts, not an audit decision:
  execution and approval binding call the preflight directly and never read
  this memo, so a database clock would buy nothing here.
* Concurrent callers with the same key on the same event loop share one
  in-flight computation; a caller on another loop computes for itself, and a
  stored result is readable from any loop. A failed or cancelled computation
  is never memoized: it is evicted and each waiter computes for itself.
* Every stored and returned ``ValidationResult`` is a deep copy, so no two
  consumers share mutable model state.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.secrets import SecretInventoryItem
from elspeth.web.execution.schemas import ValidationResult

VALIDATION_MEMO_MAX_ENTRIES = 64
VALIDATION_MEMO_TTL_SECONDS = 10.0

BlobReads = tuple[tuple[UUID, BlobRecord | None], ...]


@dataclass(frozen=True, slots=True)
class ValidationMemoKey:
    """Every keyable input that can change an inner validation result.

    Inputs that are constant for one ``ExecutionServiceImpl`` instance
    (operator settings, YAML generator, secret-wiring policy, frozen profile
    registry, plugin catalog) are scoped by the memo living on that instance.
    Completion-gate facts are merged after the memo, so they are not part of
    the key.
    """

    user_id: str | None
    session_id: UUID | None
    state_content_hash: str
    state_version: int
    plugin_snapshot_hash: str
    secret_inventory: frozenset[SecretInventoryItem]


class BlobReadLog:
    """Blob records one inner validation consulted, written from its worker thread."""

    __slots__ = ("_cacheable", "_lock", "_reads")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reads: dict[UUID, BlobRecord | None] = {}
        self._cacheable = True

    def record(self, blob_id: UUID, record: BlobRecord | None) -> None:
        """Record the metadata row (``None`` = not found) a lookup observed."""
        with self._lock:
            if blob_id in self._reads and self._reads[blob_id] != record:
                # The row changed mid-run: the result is not a function of
                # one observation, so it must not be reused.
                self._cacheable = False
            self._reads[blob_id] = record

    def mark_uncacheable(self) -> None:
        """A dependency the key cannot represent (a failed content read) was hit."""
        with self._lock:
            self._cacheable = False

    def freeze(self) -> BlobReads | None:
        """Return the consulted rows, or ``None`` when the run must not be stored."""
        with self._lock:
            if not self._cacheable:
                return None
            return tuple(sorted(self._reads.items(), key=lambda item: item[0]))


@dataclass(frozen=True, slots=True)
class _Outcome:
    result: ValidationResult
    blob_reads: BlobReads


class _Entry:
    __slots__ = ("expires_at", "future", "outcome")

    def __init__(self, future: asyncio.Future[_Outcome | None]) -> None:
        # ``future`` lets same-loop callers join the in-flight computation;
        # ``outcome`` is plain data, readable from any loop once stored.
        self.future = future
        self.outcome: _Outcome | None = None
        # In flight: never expires by time; the owner always resolves it.
        self.expires_at = float("inf")


class ValidationResultMemo:
    """Bounded TTL memo with single-flight sharing for one service instance.

    The service is reachable from more than one event loop (and so more than
    one thread), so the entry table is guarded by a lock that is never held
    across an await, and an in-flight computation is joined only from the
    loop that owns it; a caller on another loop computes for itself.
    """

    def __init__(
        self,
        *,
        max_entries: int = VALIDATION_MEMO_MAX_ENTRIES,
        ttl_seconds: float = VALIDATION_MEMO_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 1:
            raise ValueError("ValidationResultMemo.max_entries must be >= 1")
        if ttl_seconds <= 0:
            raise ValueError("ValidationResultMemo.ttl_seconds must be > 0")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[ValidationMemoKey, _Entry] = OrderedDict()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    async def get_or_compute(
        self,
        key: ValidationMemoKey,
        *,
        compute: Callable[[BlobReadLog], Awaitable[ValidationResult]],
        blob_reads_current: Callable[[BlobReads], Awaitable[bool]],
    ) -> ValidationResult:
        """Return a private copy of the result for ``key``.

        ``compute`` runs the inner validation, recording consulted blob rows
        into the log it is handed. ``blob_reads_current`` re-reads those rows
        under the caller's own context; a hit is served only when it returns
        True.
        """
        loop = asyncio.get_running_loop()
        with self._lock:
            self._evict_expired_locked()
            entry = self._entries[key] if key in self._entries else None
            outcome = None if entry is None else entry.outcome
            joinable = entry is not None and outcome is None and entry.future.get_loop() is loop
        if entry is not None and joinable:
            # Shield: a cancelled waiter must not cancel the shared computation.
            outcome = await asyncio.shield(entry.future)
        if entry is not None and outcome is not None:
            if await blob_reads_current(outcome.blob_reads):
                return outcome.result.model_copy(deep=True)
            self._discard(key, entry)
        return await self._compute_as_owner(key, compute, loop)

    async def _compute_as_owner(
        self,
        key: ValidationMemoKey,
        compute: Callable[[BlobReadLog], Awaitable[ValidationResult]],
        loop: asyncio.AbstractEventLoop,
    ) -> ValidationResult:
        entry = _Entry(loop.create_future())
        with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
        log = BlobReadLog()
        try:
            result = await compute(log)
        except BaseException:
            # Failures and cancellations are never memoized; waiters see None
            # and compute for themselves.
            self._discard(key, entry)
            entry.future.set_result(None)
            raise
        blob_reads = log.freeze()
        if blob_reads is None:
            self._discard(key, entry)
            entry.future.set_result(None)
            return result
        stored = _Outcome(result=result.model_copy(deep=True), blob_reads=blob_reads)
        with self._lock:
            entry.expires_at = self._clock() + self._ttl_seconds
            entry.outcome = stored
        entry.future.set_result(stored)
        return result

    def _discard(self, key: ValidationMemoKey, entry: _Entry) -> None:
        with self._lock:
            if key in self._entries and self._entries[key] is entry:
                del self._entries[key]

    def _evict_expired_locked(self) -> None:
        now = self._clock()
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            del self._entries[key]
