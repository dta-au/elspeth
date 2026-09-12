"""Typed plugin statistics remain intact across actual exporter projections."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime

import pytest

from elspeth.contracts import ChromaWriteStatistics, DataverseLoadStatistics, RAGRetrievalStatistics
from elspeth.contracts.events import TelemetryEvent
from elspeth.telemetry.exporters.azure_monitor import AzureMonitorExporter
from elspeth.telemetry.exporters.otlp import OTLPExporter

_TIMESTAMP = datetime(2026, 9, 12, 0, 0, tzinfo=UTC)

_DATAVERSE = DataverseLoadStatistics(
    timestamp=_TIMESTAMP,
    run_id="run-statistics",
    node_id="source-1",
    plugin_name="dataverse",
    pages_fetched=2,
    rows_yielded=5,
    rows_rejected=3,
    load_state="partial",
)
_RAG = RAGRetrievalStatistics(
    timestamp=_TIMESTAMP,
    run_id="run-statistics",
    node_id="transform-1",
    plugin_name="rag_retrieval",
    provider="chroma",
    total_queries=3,
    total_chunks=5,
    quarantine_count=1,
    score_count=2,
    score_mean=0.75,
    score_std=0.125,
)
_CHROMA = ChromaWriteStatistics(
    timestamp=_TIMESTAMP,
    run_id="run-statistics",
    node_id="sink-1",
    plugin_name="chroma",
    total_written=7,
    total_bytes=439,
)


@pytest.mark.parametrize(
    "event,expected",
    [
        pytest.param(
            _DATAVERSE,
            {
                "node_id": "source-1",
                "plugin_name": "dataverse",
                "pages_fetched": 2,
                "rows_yielded": 5,
                "rows_rejected": 3,
                "load_state": "partial",
            },
            id="dataverse",
        ),
        pytest.param(
            _RAG,
            {
                "node_id": "transform-1",
                "plugin_name": "rag_retrieval",
                "total_queries": 3,
                "total_chunks": 5,
                "quarantine_count": 1,
                "score_count": 2,
                "score_mean": 0.75,
                "score_std": 0.125,
            },
            id="rag",
        ),
        pytest.param(_CHROMA, {"node_id": "sink-1", "plugin_name": "chroma", "total_written": 7, "total_bytes": 439}, id="chroma"),
    ],
)
def test_statistics_reach_otlp_and_azure_spans(event: TelemetryEvent, expected: dict[str, object]) -> None:
    """Assert exact values after exporter wiring, including OTLP's allowlist."""
    common = {"run_id": "run-statistics", "timestamp": _TIMESTAMP.isoformat(), "event_type": type(event).__name__}
    otlp = OTLPExporter()._event_to_span(event)
    azure = AzureMonitorExporter()._event_to_span(event)

    assert otlp.name == type(event).__name__
    assert otlp.attributes == common | expected
    azure_expected = common | expected | {"cloud.provider": "azure", "elspeth.exporter": "azure_monitor"}
    if isinstance(event, RAGRetrievalStatistics):
        azure_expected["provider"] = "chroma"
    assert azure.attributes == azure_expected
    assert otlp.start_time == azure.start_time == int(_TIMESTAMP.timestamp() * 1_000_000_000)


def test_no_score_observations_remain_unknown_in_exporters() -> None:
    event = replace(_RAG, score_count=0, score_mean=None, score_std=None)
    assert event.to_dict()["score_mean"] is None
    assert event.to_dict()["score_std"] is None
    for span in (OTLPExporter()._event_to_span(event), AzureMonitorExporter()._event_to_span(event)):
        assert span.attributes is not None
        assert span.attributes["score_count"] == 0
        assert "score_mean" not in span.attributes
        assert "score_std" not in span.attributes


@pytest.mark.parametrize("event", [_DATAVERSE, _RAG, _CHROMA])
def test_statistics_are_frozen(event: TelemetryEvent) -> None:
    with pytest.raises(FrozenInstanceError):
        event.run_id = "mutated"


@pytest.mark.parametrize("bad_count", [-1, True, 1.5, "3"])
def test_statistics_reject_invalid_counters(bad_count: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        replace(_DATAVERSE, rows_rejected=bad_count)
    with pytest.raises((TypeError, ValueError)):
        replace(_RAG, total_queries=bad_count)
    with pytest.raises((TypeError, ValueError)):
        replace(_CHROMA, total_bytes=bad_count)


@pytest.mark.parametrize("event", [_DATAVERSE, _RAG, _CHROMA])
def test_statistics_require_run_node_and_aware_time(event: TelemetryEvent) -> None:
    with pytest.raises(ValueError, match="node_id"):
        replace(event, node_id="")
    with pytest.raises(ValueError, match="run_id"):
        replace(event, run_id="")
    with pytest.raises(ValueError, match="timestamp"):
        replace(event, timestamp=_TIMESTAMP.replace(tzinfo=None))


def test_dataverse_unstarted_cannot_claim_work() -> None:
    with pytest.raises(ValueError, match="not_started"):
        replace(_DATAVERSE, load_state="not_started")
    with pytest.raises(ValueError, match="load_state"):
        replace(_DATAVERSE, load_state="completed")


@pytest.mark.parametrize("bad_score", [float("nan"), float("inf"), True, "0.7"])
def test_rag_rejects_invalid_observed_scores(bad_score: object) -> None:
    with pytest.raises(ValueError, match="score_mean"):
        replace(_RAG, score_mean=bad_score)


def test_rag_score_summary_requires_observations() -> None:
    with pytest.raises(ValueError, match="score_count"):
        replace(_RAG, score_count=4)
    with pytest.raises(ValueError, match="score_mean"):
        replace(_RAG, score_count=0)
    with pytest.raises(ValueError, match="score_std"):
        replace(_RAG, score_count=1)
    with pytest.raises(ValueError, match="non-negative"):
        replace(_RAG, score_std=-0.1)
