"""Follower telemetry span wiring through the production builder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from elspeth.contracts.coordination import LeaderInfo
from elspeth.contracts.enums import RunStatus
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.types import NodeID
from elspeth.engine.orchestrator.follower import build_follower_processor

_RUN_ID = "run-follower-span-wiring"
_WORKER_ID = f"worker:{_RUN_ID}:follower"
_STARTED_AT = datetime(2026, 1, 15, 3, 4, 5, tzinfo=UTC)


@dataclass(slots=True)
class _RunRecord:
    status: RunStatus = RunStatus.RUNNING
    started_at: datetime = _STARTED_AT


class _RunLifecycle:
    def __init__(self, run: _RunRecord) -> None:
        self._run = run
        self.calls: list[str] = []

    def get_run(self, run_id: str) -> _RunRecord:
        self.calls.append(run_id)
        return self._run


class _RunCoordination:
    def __init__(self) -> None:
        self.departed: list[str] = []

    def live_leader(self, *, run_id: str, now: datetime) -> LeaderInfo:
        return LeaderInfo(
            run_id=run_id,
            leader_worker_id=f"worker:{run_id}:leader",
            leader_epoch=1,
            leader_heartbeat_expires_at=now + timedelta(seconds=80),
            seat_live=True,
        )

    def depart_worker(self, *, worker_id: str, now: datetime) -> None:
        del now
        self.departed.append(worker_id)


class _Heartbeat:
    coordination_lost = False

    def start(self) -> None:
        pass

    def stop(self, *, final_beat: bool = True) -> None:
        del final_beat


class _Telemetry:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def handle_event(self, event: Any) -> None:
        self.events.append(event)


class _Graph:
    @staticmethod
    def get_sources() -> list[NodeID]:
        return [NodeID("source")]

    @staticmethod
    def get_transform_id_map() -> dict[str, NodeID]:
        return {}

    @staticmethod
    def get_sink_id_map() -> dict[str, NodeID]:
        return {}

    @staticmethod
    def get_aggregation_id_map() -> dict[str, NodeID]:
        return {}

    @staticmethod
    def get_route_resolution_map() -> dict[Any, Any]:
        return {}

    @staticmethod
    def get_config_gate_id_map() -> dict[Any, NodeID]:
        return {}

    @staticmethod
    def get_coalesce_id_map() -> dict[Any, NodeID]:
        return {}


class _DrainOneRow:
    def __init__(self, run: _RunRecord) -> None:
        self._run = run
        self.span_factory: Any = None

    def drain_follower_ready_work(
        self,
        ctx: PluginContext,
        *,
        before_claim: Any = None,
    ) -> list[Any]:
        if before_claim is not None:
            before_claim()
        with self.span_factory.row_span("row-1", "token-1", run_id=ctx.run_id):
            pass
        self._run.status = RunStatus.COMPLETED
        return [object()]


def test_builder_emits_follower_row_as_root_in_durable_run_trace() -> None:
    """Joined work uses the original run trace without inventing a leader span."""
    from elspeth.contracts.events import EngineSpanName

    run = _RunRecord()
    run_lifecycle = _RunLifecycle(run)
    run_coordination = _RunCoordination()
    factory = SimpleNamespace(
        run_lifecycle=run_lifecycle,
        run_coordination=run_coordination,
        data_flow=object(),
    )
    config = SimpleNamespace(sources={}, transforms=[], sinks={})
    telemetry = _Telemetry()
    processor = _DrainOneRow(run)

    def _build_row_processor(**kwargs: Any) -> tuple[_DrainOneRow, dict[Any, Any], None]:
        processor.span_factory = kwargs["span_factory"]
        return processor, {}, None

    with (
        patch("elspeth.engine.orchestrator.graph_wiring.build_source_id_map", return_value={}),
        patch("elspeth.engine.orchestrator.graph_wiring.assign_plugin_node_ids"),
        patch("elspeth.engine.orchestrator.graph_wiring.load_edge_map", return_value={}),
        patch(
            "elspeth.engine.orchestrator.processor_factory.build_row_processor",
            side_effect=_build_row_processor,
        ),
        patch("elspeth.engine.orchestrator.follower.RunHeartbeatThread", return_value=_Heartbeat()),
    ):
        follower = build_follower_processor(
            factory=factory,  # type: ignore[arg-type]
            run_id=_RUN_ID,
            worker_id=_WORKER_ID,
            graph=_Graph(),  # type: ignore[arg-type]
            config=config,  # type: ignore[arg-type]
            payload_store=object(),  # type: ignore[arg-type]
            telemetry=telemetry,  # type: ignore[arg-type]
        )
        follower.run(PluginContext(run_id=_RUN_ID, config={}, landscape=None))

    assert run_lifecycle.calls[0] == _RUN_ID
    assert run_coordination.departed == [_WORKER_ID]
    assert len(telemetry.events) == 1
    row_span = telemetry.events[0]
    assert row_span.name is EngineSpanName.ROW
    assert row_span.run_id == _RUN_ID
    assert row_span.trace_started_at == _STARTED_AT
    assert row_span.parent_span_id is None
