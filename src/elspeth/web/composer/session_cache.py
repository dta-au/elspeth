"""Bounded session ownership and immutable memo storage for Composer work.

Weights account for retained values and metadata, rather than estimating RSS.
The registry's one short lock protects metadata only; user computation never
runs under it. Request borrows and actual-worker pins have separate lifetimes.
"""

from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Callable, Hashable
from concurrent.futures import Future
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from typing import TYPE_CHECKING

from elspeth.web.async_workers import run_joined_sync_in_worker, run_sync_in_worker
from elspeth.web.composer._response_json import FrozenResponseJSON, encode_response_json, parse_frozen_response_json
from elspeth.web.composer.discovery_cache import CachedDiscoveryPayload
from elspeth.web.composer.state import ValidationSummary

if TYPE_CHECKING:
    from elspeth.contracts import SinkProtocol, SourceProtocol, TransformProtocol
    from elspeth.plugins.infrastructure.manager import PluginManager


@dataclass(frozen=True, slots=True)
class SessionCacheKey:
    principal_scope: str
    session_id: str


@dataclass(frozen=True, slots=True)
class SessionCacheLimits:
    max_sessions: int = 128
    idle_seconds: float = 900.0
    validation_entries: int = 16
    discovery_entries: int = 128
    projection_entries: int = 32
    pending_entries: int = 16
    queued_jobs: int = 128
    schema_entries: int = 128
    retirement_entries: int = 128
    session_weight_bytes: int = 2 * 1024 * 1024
    total_weight_bytes: int = 64 * 1024 * 1024
    entry_weight_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        counts = (
            self.max_sessions,
            self.validation_entries,
            self.discovery_entries,
            self.projection_entries,
            self.pending_entries,
            self.queued_jobs,
            self.schema_entries,
            self.retirement_entries,
            self.session_weight_bytes,
            self.total_weight_bytes,
            self.entry_weight_bytes,
        )
        if any(value <= 0 for value in counts) or self.idle_seconds <= 0:
            raise ValueError("Session cache limits must be positive")


@dataclass(frozen=True, slots=True)
class RegistryRevision:
    """Actual registrations, including negative lookup dependencies."""

    manager: PluginManager
    sources: tuple[type[SourceProtocol], ...]
    transforms: tuple[type[TransformProtocol], ...]
    sinks: tuple[type[SinkProtocol], ...]


@dataclass(frozen=True, slots=True)
class ValidationKey:
    content_hash: str
    registry: RegistryRevision
    mode: str = "raw-v1"
    snapshot_hash: str = ""
    profile_generation: str | int = ""

    def weight_bytes(self) -> int:
        return (
            256
            + len(self.content_hash) * 4
            + len(self.snapshot_hash) * 4
            + len(str(self.profile_generation)) * 4
            + 16 * (len(self.registry.sources) + len(self.registry.transforms) + len(self.registry.sinks))
        )


@dataclass(frozen=True, slots=True)
class DiscoveryKey:
    tool_name: str
    arguments_key: str
    dependency_revision: str
    policy_snapshot_hash: str = ""

    def weight_bytes(self) -> int:
        return 192 + 4 * (len(self.tool_name) + len(self.arguments_key) + len(self.dependency_revision) + len(self.policy_snapshot_hash))


@dataclass(frozen=True, slots=True)
class ProjectionKey:
    content_hash: str
    policy_snapshot_hash: str
    dependency_revision: str

    def weight_bytes(self) -> int:
        return 192 + 4 * (len(self.content_hash) + len(self.policy_snapshot_hash) + len(self.dependency_revision))


class MemoSource(StrEnum):
    HIT = "hit"
    MISS = "miss"
    JOIN = "join"
    BYPASS = "bypass"


@dataclass(frozen=True, slots=True)
class MemoResult[V]:
    value: V
    source: MemoSource


@dataclass(frozen=True, slots=True)
class CacheStatistics:
    active_sessions: int
    retained_sessions: int
    weight_bytes: int
    retained_entries: int
    pending_entries: int
    queued_jobs: int
    borrows: int
    pins: int
    retirements: int
    hits: int
    misses: int
    joins: int
    bypasses: int


def discovery_weight_bytes(payload: CachedDiscoveryPayload) -> int:
    response = payload.admitted_response
    return (
        128
        + 4 * sum(map(len, payload.affected_nodes))
        + (len(json.dumps(response.to_wire(), ensure_ascii=True)) * 2 if response is not None else 0)
    )


def projection_weight_bytes(value: FrozenResponseJSON) -> int:
    return 128 + len(json.dumps(encode_response_json(value), ensure_ascii=True)) * 2


def _admitted_discovery(payload: CachedDiscoveryPayload) -> bool:
    return payload.success is True and payload.data is None and payload.admitted_response is not None


@dataclass(frozen=True, slots=True)
class _Entry[V]:
    value: V
    weight: int


@dataclass(frozen=True, slots=True)
class _Queued[V]:
    outcome: Future[MemoResult[V]]
    weight: int


class _Evictable(ABC):
    @abstractmethod
    def evict_one(self) -> bool: ...

    @abstractmethod
    def clear(self) -> None: ...

    @property
    @abstractmethod
    def entry_count(self) -> int: ...

    @property
    @abstractmethod
    def pending_count(self) -> int: ...


def _consume_outcome[T](future: asyncio.Future[T]) -> None:
    if not future.cancelled():
        future.exception()


async def _shield_outcome[T](outcome: Future[T]) -> T:
    wrapped = asyncio.wrap_future(outcome)
    wrapped.add_done_callback(_consume_outcome)
    return await asyncio.shield(wrapped)


class WeightedMemo[K: Hashable, V](_Evictable):
    """Typed values with per-key running-work coalescing and bounded queueing.

    A synchronous caller only joins producers already executing in a thread.
    It ignores queued async wrappers and claims a missing key inline, avoiding
    starvation when all shared-pool threads encounter the same queued key.
    """

    def __init__(
        self,
        owner: ComposerSessionCache,
        max_entries: int,
        *,
        admit: Callable[[V], bool] | None = None,
        prepare: Callable[[V], V] | None = None,
    ) -> None:
        self._owner = owner
        self._max_entries = max_entries
        self._admit = admit
        self._prepare = prepare
        self._entries: OrderedDict[K, _Entry[V]] = OrderedDict()
        self._pending: dict[K, Future[V]] = {}
        self._queued: dict[K, _Queued[V]] = {}

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def evict_one(self) -> bool:
        if not self._entries:
            return False
        _, entry = self._entries.popitem(last=False)
        self._owner._adjust_weight(-entry.weight)
        return True

    def clear(self) -> None:
        while self.evict_one():
            pass

    def get_or_compute(
        self,
        key: K,
        compute: Callable[[], V],
        *,
        key_weight: int,
        value_weight: Callable[[V], int],
        retain: Callable[[V], bool] | None = None,
    ) -> MemoResult[V]:
        owner = self._owner
        registry = owner._registry
        pending: Future[V] | None = None
        producer = False
        reservation = key_weight + 192
        with registry._lock:
            if owner._usable():
                entry = self._entries.get(key)
                if entry is not None:
                    self._entries.move_to_end(key)
                    owner._touch()
                    registry._hits += 1
                    return MemoResult(entry.value, MemoSource.HIT)
                pending = self._pending.get(key)
                if pending is not None:
                    registry._joins += 1
                    owner._pins += 1
                elif len(self._pending) < registry.limits.pending_entries and registry._reserve(owner, reservation):
                    pending = Future()
                    self._pending[key] = pending
                    owner._pins += 1
                    registry._misses += 1
                    producer = True
            if pending is None:
                registry._bypasses += 1
        if pending is None:
            return MemoResult(compute(), MemoSource.BYPASS)
        try:
            if not producer:
                return MemoResult(pending.result(), MemoSource.JOIN)
            try:
                value = compute()
                if self._prepare is not None:
                    value = self._prepare(value)
                # Value inspection and dependency rechecks may be expensive.
                # Keep them outside the metadata lock too.
                should_retain = (self._admit is None or self._admit(value)) and (retain is None or retain(value))
                weight = key_weight + 64 + value_weight(value) if should_retain else 0
                with registry._lock:
                    self._pending.pop(key)
                    owner._adjust_weight(-reservation)
                    if should_retain and owner._usable() and weight <= registry.limits.entry_weight_bytes:
                        while len(self._entries) >= self._max_entries:
                            self.evict_one()
                        if registry._reserve(owner, weight):
                            self._entries[key] = _Entry(value, weight)
                pending.set_result(value)
                return MemoResult(value, MemoSource.MISS)
            except BaseException as exc:
                with registry._lock:
                    if self._pending.pop(key, None) is not None:
                        owner._adjust_weight(-reservation)
                pending.set_exception(exc)
                raise
        finally:
            with registry._lock:
                owner._release_pin()

    async def get_or_compute_async(
        self,
        key: K,
        compute: Callable[[], V],
        *,
        key_weight: int,
        value_weight: Callable[[V], int],
        retain: Callable[[V], bool] | None = None,
    ) -> MemoResult[V]:
        owner = self._owner
        registry = owner._registry
        running: Future[V] | None = None
        queued: _Queued[V] | None = None
        joined_queue = False
        reservation = key_weight + 256
        with registry._lock:
            if owner._usable():
                entry = self._entries.get(key)
                if entry is not None:
                    self._entries.move_to_end(key)
                    owner._touch()
                    registry._hits += 1
                    return MemoResult(entry.value, MemoSource.HIT)
                running = self._pending.get(key)
                if running is not None:
                    registry._joins += 1
                else:
                    queued = self._queued.get(key)
                    if queued is not None:
                        registry._joins += 1
                        joined_queue = True
                    elif (
                        len(registry._jobs) < registry.limits.queued_jobs
                        and len(self._queued) < registry.limits.pending_entries
                        and registry._reserve(owner, reservation)
                    ):
                        queued = _Queued(Future(), reservation)
                        self._queued[key] = queued
                        owner._pins += 1
                        task = asyncio.create_task(self._run_job(key, queued, compute, key_weight, value_weight, retain))
                        registry._jobs.add(task)
                        task.add_done_callback(lambda done: self._job_done(key, done))
            if running is None and queued is None:
                registry._bypasses += 1
        if running is not None:
            return MemoResult(await _shield_outcome(running), MemoSource.JOIN)
        if queued is not None:
            result = await _shield_outcome(queued.outcome)
            return MemoResult(result.value, MemoSource.JOIN) if joined_queue else result
        # Pressure fallback submits only the caller's computation, with no
        # memo invocation or later cache mutation attached to the worker.
        return MemoResult(await run_sync_in_worker(compute), MemoSource.BYPASS)

    async def _run_job(
        self,
        key: K,
        queued: _Queued[V],
        compute: Callable[[], V],
        key_weight: int,
        value_weight: Callable[[V], int],
        retain: Callable[[V], bool] | None,
    ) -> None:
        try:
            result = await run_joined_sync_in_worker(
                self.get_or_compute,
                key,
                compute,
                key_weight=key_weight,
                value_weight=value_weight,
                retain=retain,
            )
            queued.outcome.set_result(result)
        except BaseException as exc:
            queued.outcome.set_exception(exc)
        finally:
            registry = self._owner._registry
            with registry._lock:
                self._finish_queued(key, queued)

    def _finish_queued(self, key: K, queued: _Queued[V]) -> None:
        if self._queued.get(key) is queued:
            self._queued.pop(key)
            self._owner._adjust_weight(-queued.weight)
            self._owner._release_pin()

    def _job_done(self, key: K, task: asyncio.Task[None]) -> None:
        registry = self._owner._registry
        with registry._lock:
            registry._jobs.discard(task)
            if task.cancelled():
                # Cancellation before the coroutine's first instruction never
                # enters its finally block. No submission exists in this case.
                queued = self._queued.get(key)
                if queued is not None:
                    self._finish_queued(key, queued)
                    queued.outcome.set_exception(asyncio.CancelledError())


class ComposerSessionCache:
    def __init__(self, registry: ComposerSessionCacheRegistry, key: SessionCacheKey, weight: int) -> None:
        self._registry = registry
        self.key = key
        self._weight = weight
        self._borrows = 0
        self._pins = 0
        self._retired = False
        self._last_used = registry._clock()
        limits = registry.limits
        self.validations: WeightedMemo[ValidationKey, ValidationSummary] = WeightedMemo(self, limits.validation_entries)
        self.discovery: WeightedMemo[DiscoveryKey, CachedDiscoveryPayload] = WeightedMemo(
            self, limits.discovery_entries, admit=_admitted_discovery
        )
        self.projections: WeightedMemo[ProjectionKey, FrozenResponseJSON] = WeightedMemo(
            self, limits.projection_entries, prepare=parse_frozen_response_json
        )
        self._memos: tuple[_Evictable, ...] = (self.validations, self.discovery, self.projections)
        self._schema_revision = ""
        self._schemas: set[tuple[str, str]] = set()
        self._schema_weight = 0

    def _usable(self) -> bool:
        return self._registry.enabled and not self._retired and not self._registry._closing

    def _touch(self) -> None:
        self._last_used = self._registry._clock()
        if not self._retired:
            self._registry._active.move_to_end(self.key)

    def _adjust_weight(self, delta: int) -> None:
        self._weight += delta
        self._registry._weight += delta

    def _evict_one(self) -> bool:
        return any(memo.evict_one() for memo in self._memos)

    def _clear_values(self) -> None:
        for memo in self._memos:
            memo.clear()
        self._schemas.clear()
        self._schema_revision = ""
        self._adjust_weight(-self._schema_weight)
        self._schema_weight = 0

    def _release_pin(self) -> None:
        self._pins -= 1
        self._touch()
        self._registry._dispose_if_drained(self)

    def schemas_loaded(self, snapshot_hash: str) -> frozenset[tuple[str, str]]:
        with self._registry._lock:
            if self._retired or self._schema_revision != snapshot_hash:
                return frozenset()
            return frozenset(self._schemas)

    def mark_schema_loaded(self, snapshot_hash: str, plugin_kind: str, plugin_name: str) -> bool:
        """Record actual delivery, independently of discovery memo occupancy."""
        with self._registry._lock:
            if self._retired or self._registry._closing:
                return False
            if self._schema_revision != snapshot_hash:
                self._schemas.clear()
                self._adjust_weight(-self._schema_weight)
                self._schema_weight = 0
                self._schema_revision = ""
            identity = (plugin_kind, plugin_name)
            if identity in self._schemas:
                return True
            weight = 96 + 4 * (len(plugin_kind) + len(plugin_name))
            if not self._schemas:
                weight += 64 + len(snapshot_hash) * 4
            if len(self._schemas) >= self._registry.limits.schema_entries or not self._registry._reserve(self, weight):
                return False
            self._schemas.add(identity)
            self._schema_revision = snapshot_hash
            self._schema_weight += weight
            self._touch()
            return True


class SessionCacheLease:
    def __init__(self, cache: ComposerSessionCache | None) -> None:
        self.cache = cache
        self._closed = False

    def __enter__(self) -> SessionCacheLease:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.cache is not None:
            registry = self.cache._registry
            with registry._lock:
                self.cache._borrows -= 1
                self.cache._touch()
                registry._dispose_if_drained(self.cache)


class SessionRetirement:
    def __init__(self, registry: ComposerSessionCacheRegistry, session_id: str, recorded: bool) -> None:
        self._registry = registry
        self._session_id = session_id
        self._recorded = recorded
        self._closed = False

    def __enter__(self) -> SessionRetirement:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._registry._lock:
            if self._recorded:
                remaining = self._registry._retirements[self._session_id] - 1
                if remaining:
                    self._registry._retirements[self._session_id] = remaining
                else:
                    del self._registry._retirements[self._session_id]
                    self._registry._weight -= 96 + 4 * len(self._session_id)
            else:
                self._registry._retirement_overflow -= 1


class ComposerSessionCacheRegistry:
    def __init__(
        self,
        *,
        enabled: bool = True,
        limits: SessionCacheLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.enabled = enabled
        self.limits = limits if limits is not None else SessionCacheLimits()
        self._clock = clock
        self._lock = RLock()
        self._active: OrderedDict[SessionCacheKey, ComposerSessionCache] = OrderedDict()
        self._owners: set[ComposerSessionCache] = set()
        self._jobs: set[asyncio.Task[None]] = set()
        self._retirements: dict[str, int] = {}
        self._retirement_overflow = 0
        self._weight = 0
        self._closing = False
        self._hits = self._misses = self._joins = self._bypasses = 0

    def _dispose_if_drained(self, owner: ComposerSessionCache) -> None:
        if owner._retired and owner._borrows == 0 and owner._pins == 0:
            owner._clear_values()
            self._owners.remove(owner)
            self._weight -= owner._weight
            owner._weight = 0

    def _retire(self, owner: ComposerSessionCache) -> None:
        owner._retired = True
        self._active.pop(owner.key, None)
        owner._clear_values()
        self._dispose_if_drained(owner)

    def _evict_idle(self, *, expired_only: bool, excluding: ComposerSessionCache | None = None) -> bool:
        now = self._clock()
        for owner in tuple(self._active.values()):
            if owner is excluding or owner._borrows or owner._pins:
                continue
            if expired_only and now - owner._last_used < self.limits.idle_seconds:
                continue
            self._retire(owner)
            return True
        return False

    def _reserve(self, owner: ComposerSessionCache, weight: int) -> bool:
        if weight < 0:
            raise ValueError("Cache weight must be non-negative")
        if weight > self.limits.entry_weight_bytes or weight + 512 > self.limits.session_weight_bytes:
            return False
        while owner._weight + weight > self.limits.session_weight_bytes or self._weight + weight > self.limits.total_weight_bytes:
            if owner._evict_one():
                continue
            if self._evict_idle(expired_only=False, excluding=owner):
                continue
            return False
        owner._adjust_weight(weight)
        return True

    def borrow(self, key: SessionCacheKey | None) -> SessionCacheLease:
        with self._lock:
            if key is None or self._closing or self._retirement_overflow or key.session_id in self._retirements:
                self._bypasses += 1
                return SessionCacheLease(None)
            while self._evict_idle(expired_only=True):
                pass
            owner = self._active.get(key)
            if owner is None:
                # Retirement reservations cover the archive attempt; a retired
                # owner's own reservation lasts until its actual work drains.
                # The bounded owner set prevents two owners for the same key.
                if any(retained.key == key for retained in self._owners):
                    self._bypasses += 1
                    return SessionCacheLease(None)
                weight = 512 + 4 * (len(key.principal_scope) + len(key.session_id))
                while len(self._owners) >= self.limits.max_sessions or self._weight + weight > self.limits.total_weight_bytes:
                    if not self._evict_idle(expired_only=False):
                        self._bypasses += 1
                        return SessionCacheLease(None)
                if weight > self.limits.session_weight_bytes or weight > self.limits.entry_weight_bytes:
                    self._bypasses += 1
                    return SessionCacheLease(None)
                owner = ComposerSessionCache(self, key, weight)
                self._active[key] = owner
                self._owners.add(owner)
                self._weight += weight
            owner._borrows += 1
            owner._touch()
            return SessionCacheLease(owner)

    def retire_session(self, session_id: str) -> SessionRetirement:
        with self._lock:
            weight = 96 + 4 * len(session_id)
            existing = session_id in self._retirements
            recorded = existing or (
                len(self._retirements) < self.limits.retirement_entries
                and weight <= self.limits.entry_weight_bytes
                and self._weight + weight <= self.limits.total_weight_bytes
            )
            if recorded:
                self._retirements[session_id] = self._retirements.get(session_id, 0) + 1
                if not existing:
                    self._weight += weight
            else:
                self._retirement_overflow += 1
            for owner in tuple(self._active.values()):
                if owner.key.session_id == session_id:
                    self._retire(owner)
            return SessionRetirement(self, session_id, recorded)

    def begin_shutdown(self) -> None:
        with self._lock:
            self._closing = True
            for owner in tuple(self._active.values()):
                self._retire(owner)

    async def finish_shutdown(self) -> None:
        self.begin_shutdown()
        with self._lock:
            jobs = tuple(self._jobs)
        if jobs:
            await asyncio.gather(*(asyncio.shield(job) for job in jobs))

    def snapshot(self) -> CacheStatistics:
        with self._lock:
            return CacheStatistics(
                len(self._active),
                len(self._owners),
                self._weight,
                sum(m.entry_count for o in self._owners for m in o._memos),
                sum(m.pending_count for o in self._owners for m in o._memos),
                len(self._jobs),
                sum(o._borrows for o in self._owners),
                sum(o._pins for o in self._owners),
                sum(self._retirements.values()) + self._retirement_overflow,
                self._hits,
                self._misses,
                self._joins,
                self._bypasses,
            )
