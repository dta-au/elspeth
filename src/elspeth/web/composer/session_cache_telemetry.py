"""Bounded operational cache metrics, independent of audit and cache authority.

Only fixed dimensions are retained. Instrument callbacks run outside request
locks; weak registry references never extend cache lifetime. Accounting weight
includes metadata and reservations and is not a measurement of process RSS.
"""

from __future__ import annotations

from collections.abc import Iterable
from threading import Lock
from typing import TYPE_CHECKING, Literal
from weakref import WeakSet

from opentelemetry.metrics import CallbackOptions, Meter, Observation, get_meter

if TYPE_CHECKING:
    from elspeth.web.composer.session_cache import ComposerSessionCacheRegistry

MemoNamespace = Literal["validation", "discovery", "projection"]
LookupNamespace = Literal["validation", "discovery", "projection", "owner"]
LookupResult = Literal["hit", "miss", "join", "bypass"]
_NAMESPACES: tuple[LookupNamespace, ...] = ("validation", "discovery", "projection", "owner")
_RESULTS: tuple[LookupResult, ...] = ("hit", "miss", "join", "bypass")
_lock = Lock()
_lookups = {(namespace, result): 0 for namespace in _NAMESPACES for result in _RESULTS}
_evictions: dict[MemoNamespace, int] = {"validation": 0, "discovery": 0, "projection": 0}
_registries: WeakSet[ComposerSessionCacheRegistry] = WeakSet()


def register(registry: ComposerSessionCacheRegistry) -> None:
    with _lock:
        _registries.add(registry)


def record_lookup(namespace: LookupNamespace, result: LookupResult) -> None:
    with _lock:
        _lookups[namespace, result] += 1


def record_eviction(namespace: MemoNamespace) -> None:
    with _lock:
        _evictions[namespace] += 1


def _observe_lookups(_options: CallbackOptions) -> Iterable[Observation]:
    with _lock:
        values = tuple(_lookups.items())
    return tuple(Observation(count, {"kind": namespace, "result": result}) for (namespace, result), count in values)


def _observe_evictions(_options: CallbackOptions) -> Iterable[Observation]:
    with _lock:
        values = tuple(_evictions.items())
    return tuple(Observation(count, {"kind": namespace}) for namespace, count in values)


def _observe_weight(_options: CallbackOptions) -> Iterable[Observation]:
    with _lock:
        registries = tuple(_registries)
    # Never acquire a registry lock while holding the telemetry lock.
    return (Observation(sum(registry.snapshot().weight_bytes for registry in registries)),)


def wire_metrics(meter: Meter) -> None:
    meter.create_observable_counter(
        "composer.cache.lookups", callbacks=[_observe_lookups], unit="{lookup}", description="Cache lookup outcomes by fixed namespace."
    )
    meter.create_observable_counter(
        "composer.cache.evictions",
        callbacks=[_observe_evictions],
        unit="{entry}",
        description="Retained entries removed, including retirement.",
    )
    meter.create_observable_gauge(
        "composer.cache.weight",
        callbacks=[_observe_weight],
        unit="By",
        description="Current cache accounting weight across live registries.",
    )


wire_metrics(get_meter(__name__))
