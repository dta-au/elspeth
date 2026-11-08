"""Real metric collection covers bounded labels and non-owning cache observation."""

from __future__ import annotations

import gc
import threading
import weakref
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from elspeth.web.composer import session_cache_telemetry as telemetry
from elspeth.web.composer.session_cache import ComposerSessionCacheRegistry, ProjectionKey, SessionCacheKey, SessionCacheLimits


@pytest.fixture
def reader(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemoryMetricReader]:
    monkeypatch.setattr(telemetry, "_lookups", dict.fromkeys(telemetry._lookups, 0))
    monkeypatch.setattr(telemetry, "_evictions", dict.fromkeys(telemetry._evictions, 0))
    monkeypatch.setattr(telemetry, "_registries", weakref.WeakSet())
    metric_reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[metric_reader])
    telemetry.wire_metrics(provider.get_meter("cache-proof"))
    yield metric_reader
    provider.shutdown()


def _points(reader: InMemoryMetricReader) -> dict[str, dict[tuple[tuple[str, object], ...], int | float]]:
    data = reader.get_metrics_data()
    assert data is not None
    return {
        metric.name: {tuple(sorted(point.attributes.items())): point.value for point in metric.data.data_points}
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }


def test_metrics_match_real_hits_pressure_bypass_and_retirement(reader: InMemoryMetricReader) -> None:
    registry = ComposerSessionCacheRegistry(limits=replace(SessionCacheLimits(), projection_entries=1))
    with registry.borrow(SessionCacheKey("secret-principal", "secret-session")) as lease:
        cache = lease.cache
        assert cache is not None
        for name in ("a", "a", "b"):
            cache.projections.get_or_compute(
                ProjectionKey(name, "secret-policy", "revision"), lambda: "value", key_weight=64, value_weight=len
            )
        registry.borrow(None).close()
        points = _points(reader)
        lookups = points["composer.cache.lookups"]
        assert lookups[(("kind", "projection"), ("result", "hit"))] == 1
        assert lookups[(("kind", "projection"), ("result", "miss"))] == 2
        assert lookups[(("kind", "owner"), ("result", "bypass"))] == 1
        assert len(lookups) == 16
        assert points["composer.cache.evictions"][(("kind", "projection"),)] == registry.snapshot().evictions == 1
        assert points["composer.cache.weight"][()] == registry.snapshot().weight_bytes > 0
        assert "secret" not in repr(points)
    registry.begin_shutdown()
    assert _points(reader)["composer.cache.weight"][()] == 0
    assert _points(reader)["composer.cache.evictions"][(("kind", "projection"),)] == 2


def test_observation_does_not_keep_private_registries_alive(reader: InMemoryMetricReader) -> None:
    registry = ComposerSessionCacheRegistry()
    registry.borrow(SessionCacheKey("principal", "private")).close()
    reference = weakref.ref(registry)
    assert _points(reader)["composer.cache.weight"][()] > 0
    del registry
    gc.collect()
    assert reference() is None
    assert _points(reader)["composer.cache.weight"][()] == 0


def test_join_and_disabled_metrics_use_fixed_dimensions(reader: InMemoryMetricReader) -> None:
    registry = ComposerSessionCacheRegistry()
    entered = threading.Event()
    release = threading.Event()

    def compute() -> str:
        entered.set()
        assert release.wait(5)
        return "value"

    with registry.borrow(SessionCacheKey("principal", "session")) as lease, ThreadPoolExecutor(max_workers=2) as pool:
        cache = lease.cache
        assert cache is not None
        key = ProjectionKey("content", "policy", "revision")
        producer = pool.submit(cache.projections.get_or_compute, key, compute, key_weight=64, value_weight=len)
        assert entered.wait(5)
        # The telemetry hook provides a deterministic join barrier without sleeps.
        original = telemetry.record_lookup
        joined = threading.Event()

        def record(namespace: telemetry.LookupNamespace, result: telemetry.LookupResult) -> None:
            original(namespace, result)
            if result == "join":
                joined.set()

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(telemetry, "record_lookup", record)
            waiter = pool.submit(cache.projections.get_or_compute, key, compute, key_weight=64, value_weight=len)
            try:
                assert joined.wait(5)
            finally:
                release.set()
            assert producer.result().value == waiter.result().value == "value"
    disabled = ComposerSessionCacheRegistry(enabled=False)
    with disabled.borrow(SessionCacheKey("principal", "disabled")) as lease:
        assert lease.cache is not None
        lease.cache.projections.get_or_compute(key, lambda: "value", key_weight=64, value_weight=len)
    lookups = _points(reader)["composer.cache.lookups"]
    assert lookups[(("kind", "projection"), ("result", "join"))] == 1
    assert lookups[(("kind", "projection"), ("result", "bypass"))] == 1
    assert len(lookups) == 16
