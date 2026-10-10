"""Tests for the B1-r3 MeterProvider precondition in elspeth.web.app.

Verifies that ``create_app()`` retains a real app-owned ``MeterProvider``
(not the OTel default ``NoOpMeterProvider``) and exposes a Prometheus
scrape endpoint at ``GET /metrics``.

Production and test installations
---------------------------------
The production installation reserves the process-global OTel provider once;
OTel does not support saving and restoring that slot. Ordinary tests use the
autouse owned test installation, which retains a real SDK provider and an
isolated registry per app without setting the global provider. The global
provider may therefore remain an OTel proxy in these tests. Assert on the
app-owned runtime and installation when checking app construction.

An isolated reader test uses ``provider.get_meter()`` directly. Production
module-level counters can be intercepted with a counter from a local provider;
see ``tests/unit/engine/test_executors.py``. Counter tests must assert emitted
data rather than infer it from provider type alone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from pydantic import SecretBytes
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

# ---------------------------------------------------------------------------
# Test 1 — creating the app wires a real MeterProvider
# ---------------------------------------------------------------------------


def test_meter_provider_is_not_noop(tmp_path: Path) -> None:
    """``create_app()`` owns a real ``MeterProvider``, not the OTel default.

    This is the B1-r3 load-bearing precondition: without a real provider every
    counter in the codebase silently discards its data.
    """
    from elspeth.web.app import create_app
    from elspeth.web.config import WebSettings

    settings = WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=SecretBytes(b"\x00" * 32),
    )
    app = create_app(settings)

    runtime = app.state.operator_telemetry
    provider = runtime.provider
    assert isinstance(provider, MeterProvider), f"Expected app-owned MeterProvider but got {type(provider).__name__!r}."
    assert runtime.cleanup_owner.installation.get_provider() is provider


# ---------------------------------------------------------------------------
# Test 2 — a counter bound to an in-memory reader records data
# ---------------------------------------------------------------------------


def test_counter_emits_to_in_memory_reader() -> None:
    """A counter created via ``provider.get_meter()`` and ``create_counter()``
    records an increment that appears in the metric reader's output.

    Uses an isolated ``InMemoryMetricReader`` bound to its own ``MeterProvider``
    via direct provider access (NOT via the global ``metrics.get_meter()``).
    This is hermetic with respect to the app-owned test installation and
    does not depend on the process-global OTel provider, which production
    can install only once.
    """
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    try:
        # Use provider.get_meter() directly — NOT the global metrics.get_meter().
        # The global API is separate from this isolated reader.
        meter = provider.get_meter("elspeth.test.b1r3")
        counter = meter.create_counter("test_counter_b1r3")
        counter.add(1, {"env": "test"})

        metrics_data = reader.get_metrics_data()
        assert metrics_data is not None

        found = False
        for rm in metrics_data.resource_metrics:
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    if metric.name == "test_counter_b1r3":
                        for point in metric.data.data_points:
                            if dict(point.attributes or {}).get("env") == "test":
                                number_point = cast(Any, point)
                                assert number_point.value >= 1
                                found = True

        assert found, (
            "Expected test_counter_b1r3 with env=test in metrics data but it was not found. Counter.add() appears to be discarding data."
        )
    finally:
        reader.shutdown()


# ---------------------------------------------------------------------------
# Test 3 — GET /metrics returns Prometheus exposition format
# ---------------------------------------------------------------------------


def test_metrics_endpoint_returns_prometheus_format(tmp_path: Path) -> None:
    """``GET /metrics`` on the FastAPI app returns HTTP 200 with
    Prometheus exposition format.

    Verifies:
    - Status code 200.
    - Content-Type contains ``text/plain`` (Prometheus default).
    - Body contains at least one non-comment, non-empty line (a real metric or
      the ``# HELP`` / ``# TYPE`` preamble).

    Uses the synchronously registered FastAPI route directly so the test runs
    without the application lifespan or an AnyIO portal. The ``/metrics`` route
    is independent of the lifespan context; it works before ``yield`` completes.
    """
    from elspeth.web.app import create_app
    from elspeth.web.config import WebSettings

    settings = WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=SecretBytes(b"\x00" * 32),
        operator_metrics_bearer_token="operator-metrics-token-for-tests-0001",
    )

    app = create_app(settings)

    # Pin the emit → exposition round-trip.  Without a known counter that
    # we register and increment, the previous body assertion ("at least
    # one non-comment line OR # HELP/# TYPE preamble") passed against a
    # stub body of just preamble.  A regression where every
    # production-registered counter silently failed to appear in
    # exposition would still produce ``# HELP`` / ``# TYPE`` lines and
    # pass.  By emitting a known counter and asserting its specific wire
    # name, we anchor the test to actual emit behaviour.
    pin_meter_name = "test.meter_provider.endpoint_pin"
    pin_counter_name = "elspeth_test_metrics_endpoint_pin_total"
    runtime = app.state.operator_telemetry
    pin_meter = runtime.provider.get_meter(pin_meter_name)
    pin_counter = pin_meter.create_counter(
        pin_counter_name.removesuffix("_total"),
        description="Pin counter for /metrics exposition round-trip test.",
    )
    pin_counter.add(1, {"phase8_pr_review": "s2"})

    # Call the registered synchronous route directly. TestClient would add an
    # AnyIO portal and, when used as a context manager, run lifespan startup;
    # neither is part of this route-level meter -> exposition contract.
    metrics_route = next(
        route
        for route in app.routes
        if isinstance(route, Route) and route.path == "/metrics" and route.methods is not None and "GET" in route.methods
    )
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/metrics",
            "raw_path": b"/metrics",
            "query_string": b"",
            "headers": [(b"authorization", b"Bearer operator-metrics-token-for-tests-0001")],
            "client": ("test", 123),
            "server": ("test", 80),
            "root_path": "",
            "app": app,
        }
    )
    response = metrics_route.endpoint(request)
    assert isinstance(response, Response)

    assert response.status_code == 200, (
        f"Expected 200 from GET /metrics but got {response.status_code}. "
        "Prometheus endpoint not mounted — check create_app() /metrics mount."
    )

    content_type = response.headers.get("content-type", "")
    assert "text/plain" in content_type, f"Expected text/plain content-type from /metrics but got {content_type!r}."

    body = bytes(response.body).decode("utf-8")
    # Hard-pin: the counter we just emitted must appear in exposition with
    # the value we added.  Prometheus normalises the metric name (dot →
    # underscore) but otherwise preserves it; the value line carries the
    # attribute we attached.
    assert pin_counter_name in body, (
        f"Pin counter {pin_counter_name!r} not present in /metrics body — "
        "registered counters are not reaching exposition.  Body head: "
        f"{body[:500]!r}"
    )
    assert 'phase8_pr_review="s2"' in body, (
        "Pin counter attribute not preserved through the meter → reader → "
        "exposition path.  Either the app-owned provider and retained "
        "reader disagree, or attribute "
        f"serialisation has regressed.  Body head: {body[:500]!r}"
    )
