"""Contract tests for optional exporter-native delivery metrics."""

from __future__ import annotations

from typing import cast

import pytest

from elspeth.telemetry.manager import _exporter_delivery_metrics, _metric_delta
from elspeth.telemetry.protocols import DeliveryMetricsExporterProtocol, ExporterProtocol
from tests.fixtures.telemetry import TelemetryTestExporter


def _valid_metrics() -> dict[str, object]:
    return {
        "attempted": 8,
        "delivered": 5,
        "failed": 1,
        "dropped": 2,
        "pending": 0,
        "consecutive_failures": 0,
        "last_success_unix_nano": 123,
        "lifecycle_failures": 0,
    }


class _DeliveryMetricsExporter(TelemetryTestExporter):
    def __init__(self, metrics: object) -> None:
        super().__init__()
        self._delivery_metrics = metrics

    @property
    def delivery_metrics(self) -> object:
        return self._delivery_metrics


class _DynamicDeliveryMetricsExporter(TelemetryTestExporter):
    """Exporter that resolves ``delivery_metrics`` through ``__getattr__``.

    A wrapper/decorator exporter forwarding to a wrapped implementation is a
    plausible third-party shape, and third-party exporters reach
    ``_exporter_delivery_metrics`` through the ``elspeth_get_exporters``
    pluggy hook with no type gate in front of them. Such an object fails a
    ``runtime_checkable`` Protocol ``isinstance()`` — since Python 3.12 those
    resolve members via ``inspect.getattr_static``, which bypasses
    ``__getattr__`` — so delivery accounting used to vanish silently
    (ADR-032). Only ``delivery_metrics`` is forwarded; every other name still
    raises ``AttributeError`` so the real methods on the base class are used.
    """

    def __init__(self, metrics: object) -> None:
        super().__init__()
        object.__setattr__(self, "_dynamic_metrics", metrics)

    def __getattr__(self, name: str) -> object:
        if name == "delivery_metrics":
            return object.__getattribute__(self, "_dynamic_metrics")
        raise AttributeError(name)


def _read_metrics(metrics: object) -> dict[str, int | None] | None:
    exporter = cast(ExporterProtocol, _DeliveryMetricsExporter(metrics))
    return _exporter_delivery_metrics(exporter)


def _read_dynamic_metrics(metrics: object) -> dict[str, int | None] | None:
    exporter = cast(ExporterProtocol, _DynamicDeliveryMetricsExporter(metrics))
    return _exporter_delivery_metrics(exporter)


def test_delivery_metrics_resolved_through_getattr_is_accepted() -> None:
    metrics = _valid_metrics()

    assert _read_dynamic_metrics(metrics) == metrics


def test_delivery_metrics_resolved_through_getattr_still_validates_values() -> None:
    metrics = _valid_metrics()
    metrics["delivered"] = -1

    with pytest.raises(ValueError, match=r"delivered.*non-negative"):
        _read_dynamic_metrics(metrics)


def test_delivery_metrics_protocol_is_not_an_isinstance_gate() -> None:
    """ADR-032 rule 3: the capability Protocol must not be usable as a guard."""
    exporter = _DynamicDeliveryMetricsExporter(_valid_metrics())

    with pytest.raises(TypeError):
        isinstance(exporter, DeliveryMetricsExporterProtocol)  # type: ignore[misc]


def test_exporter_without_delivery_metrics_capability_returns_none() -> None:
    exporter = cast(ExporterProtocol, TelemetryTestExporter())

    assert _exporter_delivery_metrics(exporter) is None


def test_delivery_metrics_reads_every_required_key_without_coercion() -> None:
    metrics = _valid_metrics()

    assert _read_metrics(metrics) == metrics


def test_delivery_metrics_missing_required_key_raises() -> None:
    metrics = _valid_metrics()
    del metrics["failed"]

    with pytest.raises(KeyError, match="failed"):
        _read_metrics(metrics)


def test_delivery_metrics_rejects_bool_counter() -> None:
    metrics = _valid_metrics()
    metrics["delivered"] = True

    with pytest.raises(TypeError, match="delivered"):
        _read_metrics(metrics)


def test_delivery_metrics_rejects_negative_counter() -> None:
    metrics = _valid_metrics()
    metrics["delivered"] = -1

    with pytest.raises(ValueError, match=r"delivered.*non-negative"):
        _read_metrics(metrics)


def test_delivery_metrics_rejects_malformed_container() -> None:
    with pytest.raises(TypeError, match="delivery_metrics"):
        _read_metrics(["not", "a", "mapping"])


def test_metric_delta_uses_required_keys() -> None:
    before = cast(dict[str, int | None], _valid_metrics())
    after_raw = _valid_metrics()
    after_raw["delivered"] = 7
    after = cast(dict[str, int | None], after_raw)

    assert _metric_delta(before, after, "delivered") == 2


def test_metric_delta_rejects_counter_regression() -> None:
    before = cast(dict[str, int | None], _valid_metrics())
    after_raw = _valid_metrics()
    after_raw["delivered"] = 4
    after = cast(dict[str, int | None], after_raw)

    with pytest.raises(ValueError, match=r"delivered.*regressed.*5.*4"):
        _metric_delta(before, after, "delivered")


def test_metric_delta_missing_required_key_raises() -> None:
    before = _valid_metrics()
    del before["failed"]

    with pytest.raises(KeyError, match="failed"):
        _metric_delta(
            cast(dict[str, int | None], before),
            cast(dict[str, int | None], _valid_metrics()),
            "failed",
        )


def test_metric_delta_rejects_bool_counter() -> None:
    before = _valid_metrics()
    before["dropped"] = False

    with pytest.raises(TypeError, match="dropped"):
        _metric_delta(
            cast(dict[str, int | None], before),
            cast(dict[str, int | None], _valid_metrics()),
            "dropped",
        )
