"""Dataverse statistics cross the real orchestrator and JSON telemetry exporter."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from elspeth.contracts.config.runtime import RuntimeTelemetryConfig
from elspeth.contracts.enums import BackpressureMode, RunStatus, TelemetryGranularity
from elspeth.contracts.errors import GracefulShutdownError
from elspeth.core.landscape import LandscapeDB
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.plugins.infrastructure.clients.dataverse import DataverseClientError, DataversePageResponse
from elspeth.plugins.sources.dataverse import DataverseSource
from elspeth.telemetry import TelemetryManager
from elspeth.telemetry.exporters.console import ConsoleExporter
from tests.fixtures.base_classes import as_sink, as_source
from tests.fixtures.pipeline import build_production_graph
from tests.fixtures.plugins import CollectSink


def _page(rows: list[dict[str, object]]) -> DataversePageResponse:
    return DataversePageResponse(
        status_code=200,
        rows=rows,
        latency_ms=2.0,
        headers={},
        request_headers={},
        request_url="https://test.crm.dynamics.com/api/data/v9.2/contacts",
        next_link=None,
        paging_cookie=None,
        more_records=False,
    )


@pytest.mark.parametrize("outcome", ["exhausted", "failed", "partial"])
def test_dataverse_statistics_through_orchestrator_and_console(
    landscape_db: LandscapeDB,
    payload_store: FilesystemPayloadStore,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
) -> None:
    shutdown_event = threading.Event()
    source = DataverseSource(
        {
            "environment_url": "https://test.crm.dynamics.com",
            "auth": {"method": "managed_identity"},
            "entity": "contact",
            "schema": {"mode": "observed"},
            "on_validation_failure": "discard",
        }
    )
    source.on_success = "output"
    sink = CollectSink()
    config = PipelineConfig(sources={"primary": as_source(source)}, transforms=[], sinks={"output": as_sink(sink)})
    graph = build_production_graph(config)
    exporter = ConsoleExporter()
    exporter.configure({"format": "json"})
    manager = TelemetryManager(
        RuntimeTelemetryConfig(
            enabled=True,
            granularity=TelemetryGranularity.LIFECYCLE,
            backpressure_mode=BackpressureMode.BLOCK,
            fail_on_total_exporter_failure=True,
            max_consecutive_failures=1,
            exporter_configs=(),
        ),
        exporters=[exporter],
    )

    def pages() -> Iterator[DataversePageResponse]:
        # The second row violates the locked inferred type; discard still counts.
        yield _page([{"count": 1}, {"count": "bad"}, {"count": 2}])
        if outcome == "failed":
            raise DataverseClientError(
                "second page failed",
                retryable=False,
                status_code=503,
                request_url="https://test.crm.dynamics.com/api/data/v9.2/contacts",
            )
        yield _page([{"count": 3}])

    def interrupt_after_processed_row() -> None:
        # Exercise the engine's public cooperative-shutdown boundary after
        # downstream processing, with the source generator still suspended.
        if source._rows_yielded:
            shutdown_event.set()

    try:
        with (
            patch("azure.identity.ManagedIdentityCredential", autospec=True),
            patch("elspeth.plugins.sources.dataverse.DataverseClient", autospec=True) as client_class,
        ):
            client_class.return_value.get_page.return_value = _page([{"LogicalName": "contact", "EntitySetName": "contacts"}])
            client_class.return_value.paginate_odata.return_value = pages()
            orchestrator = Orchestrator(landscape_db, telemetry_manager=manager)
            if outcome == "failed":
                with pytest.raises(DataverseClientError, match="second page failed"):
                    orchestrator.run(config, graph=graph, payload_store=payload_store)
            elif outcome == "partial":
                with pytest.raises(GracefulShutdownError, match="interrupted after 1 rows"):
                    orchestrator.run(
                        config,
                        graph=graph,
                        payload_store=payload_store,
                        shutdown_event=shutdown_event,
                        check_coordination_latch=interrupt_after_processed_row,
                    )
            else:
                result = orchestrator.run(config, graph=graph, payload_store=payload_store)
                assert result.status == RunStatus.COMPLETED
                assert result.rows_succeeded == 3
        manager.flush()
    finally:
        manager.close()

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    statistics = [event for event in events if event["event_type"] == "DataverseLoadStatistics"]
    assert len(statistics) == 1
    event = statistics[0]
    assert event["plugin_name"] == "dataverse"
    assert event["node_id"] == source.node_id
    assert event["run_id"]
    assert event["pages_fetched"] == (2 if outcome == "exhausted" else 1)
    assert event["rows_yielded"] == {"exhausted": 3, "failed": 2, "partial": 1}[outcome]
    assert event["rows_rejected"] == (0 if outcome == "partial" else 1)
    assert event["load_state"] == outcome
