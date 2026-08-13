"""Production-consumer contracts for the state-engine read models.

Repository truth tables live in the core Landscape test cohort.  This module
owns the other half of the proof: the production decision that consumes each
selector.  Thin consumers are executed with recording boundary fakes.  The
four orchestration seams whose public entry points necessarily run unrelated
plugins, heartbeats, or durable repair are pinned with explicit AST gates so
that source wiring is not misrepresented as end-to-end repository evidence.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import elspeth.core.checkpoint.recovery as recovery_module
import elspeth.engine.orchestrator.resume as resume_module
from elspeth.engine.barrier_coordination import BarrierIntakeCoordinator, BarrierIntakePassOutcome
from elspeth.engine.orchestrator.resume import ResumeCoordinator
from elspeth.engine.processor import RowProcessor
from elspeth.engine.scheduler_drain import ProcessorMode, SchedulerDrainCoordinator

REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_ID = "run-read-model-consumers"
NOW = datetime(2026, 8, 12, 2, 0, tzinfo=UTC)


class _ProcessorSchedulerSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.unresolved = 0
        self.unquiesced = 0
        self.active = 0
        self.peer_owners: tuple[str, ...] = ()
        self.row_ids: frozenset[str] = frozenset()
        self.blocked_rows: list[object] = []

    def count_unresolved_work(self, *, run_id: str) -> int:
        self.calls.append(("count_unresolved_work", {"run_id": run_id}))
        return self.unresolved

    def count_unquiesced_work(self, *, run_id: str) -> int:
        self.calls.append(("count_unquiesced_work", {"run_id": run_id}))
        return self.unquiesced

    def count_active_work(self, *, run_id: str) -> int:
        self.calls.append(("count_active_work", {"run_id": run_id}))
        return self.active

    def peer_active_leases(self, *, run_id: str, caller_owner: str, now: datetime) -> tuple[str, ...]:
        self.calls.append(
            (
                "peer_active_leases",
                {"run_id": run_id, "caller_owner": caller_owner, "now": now},
            )
        )
        return self.peer_owners

    def active_row_ids(self, *, run_id: str) -> frozenset[str]:
        self.calls.append(("active_row_ids", {"run_id": run_id}))
        return self.row_ids

    def list_blocked_barrier_items(self, *, run_id: str) -> list[object]:
        self.calls.append(("list_blocked_barrier_items", {"run_id": run_id}))
        return self.blocked_rows


class _FixedClock:
    def now_utc(self) -> datetime:
        return NOW


def _processor(spy: _ProcessorSchedulerSpy) -> RowProcessor:
    processor: Any = object.__new__(RowProcessor)
    processor._scheduler = spy
    processor._run_id = RUN_ID
    processor._scheduler_lease_owner = "leader-owner"
    processor._clock = _FixedClock()
    return cast(RowProcessor, processor)


@pytest.mark.parametrize(
    ("method", "selector_field", "selector_name"),
    [
        pytest.param(RowProcessor.has_unresolved_scheduler_work, "unresolved", "count_unresolved_work", id="RM-01"),
        pytest.param(RowProcessor.has_scheduled_work, "active", "count_active_work", id="RM-03"),
    ],
)
@pytest.mark.parametrize(("selector_result", "expected"), [(0, False), (3, True)])
def test_boolean_processor_consumers_use_the_exact_count(
    method: Any,
    selector_field: str,
    selector_name: str,
    selector_result: int,
    expected: bool,
) -> None:
    """RM-01/RM-03: zero is false and every positive count is true."""
    spy = _ProcessorSchedulerSpy()
    vars(spy)[selector_field] = selector_result

    assert method(_processor(spy)) is expected
    assert spy.calls == [(selector_name, {"run_id": RUN_ID})]


def test_rm02_processor_returns_the_exact_unquiesced_count() -> None:
    spy = _ProcessorSchedulerSpy()
    spy.unquiesced = 7

    assert _processor(spy).count_unquiesced_scheduler_work() == 7
    assert spy.calls == [("count_unquiesced_work", {"run_id": RUN_ID})]


def test_rm06_processor_preserves_peer_owner_order_and_selector_arguments() -> None:
    spy = _ProcessorSchedulerSpy()
    spy.peer_owners = ("worker-z", "worker-a")

    assert _processor(spy).peer_active_lease_owners() == ("worker-z", "worker-a")
    assert spy.calls == [
        (
            "peer_active_leases",
            {"run_id": RUN_ID, "caller_owner": "leader-owner", "now": NOW},
        )
    ]


def test_rm09_processor_returns_exact_active_source_row_id_set() -> None:
    spy = _ProcessorSchedulerSpy()
    spy.row_ids = frozenset(("row-z", "row-a"))

    assert _processor(spy).active_scheduled_row_ids() == frozenset(("row-z", "row-a"))
    assert spy.calls == [("active_row_ids", {"run_id": RUN_ID})]


@pytest.mark.parametrize(("rows", "expected"), [([], False), ([object()], True)])
def test_rm12_processor_barrier_decision_uses_exact_listing_emptiness(rows: list[object], expected: bool) -> None:
    spy = _ProcessorSchedulerSpy()
    spy.blocked_rows = rows

    assert _processor(spy).has_blocked_barrier_work() is expected
    assert spy.calls == [("list_blocked_barrier_items", {"run_id": RUN_ID})]


class _MaintenanceProcessor:
    def __init__(self, token: object, trace: list[object]) -> None:
        self._token = token
        self._trace = trace

    def _require_coordination_token(self) -> object:
        self._trace.append("require-token")
        return self._token


class _MaintenanceRunCoordination:
    def __init__(self, trace: list[object]) -> None:
        self._trace = trace

    def dead_non_leader_workers(
        self,
        *,
        run_id: str,
        leader_worker_id: str,
        now: datetime,
        grace_seconds: float,
    ) -> tuple[str, ...]:
        self._trace.append(("dead", run_id, leader_worker_id, now, grace_seconds))
        return ("dead-z", "dead-a")

    def evict_worker(
        self,
        *,
        token: object,
        target_worker_id: str,
        now: datetime,
        grace_seconds: float,
        window_seconds: float,
    ) -> bool:
        self._trace.append(("evict", token, target_worker_id, now, grace_seconds, window_seconds))
        return True


class _MaintenanceScheduler:
    def __init__(self, trace: list[object]) -> None:
        self._trace = trace

    def recover_expired_leases(self, *, now: datetime, coordination_token: object) -> int:
        self._trace.append(("recover", now, coordination_token))
        return 4


def test_rm08_maintenance_evicts_exact_dead_worker_listing_in_order() -> None:
    trace: list[object] = []
    token = SimpleNamespace(worker_id="leader-worker")
    drain: Any = object.__new__(SchedulerDrainCoordinator)
    drain._mode = ProcessorMode.LEADER
    drain._processor = _MaintenanceProcessor(token, trace)
    drain._run_coordination = _MaintenanceRunCoordination(trace)
    drain._scheduler = _MaintenanceScheduler(trace)
    drain._run_id = RUN_ID
    drain._scheduler_drains_since_maintenance = 9

    assert drain.run_maintenance(NOW) == 4
    assert trace[0] == "require-token"
    dead_call = cast(tuple[object, ...], trace[1])
    assert dead_call[0:4] == ("dead", RUN_ID, "leader-worker", NOW)
    evicted_worker_ids = []
    for entry in trace:
        if isinstance(entry, tuple) and entry[0] == "evict":
            evicted_worker_ids.append(entry[2])
    assert evicted_worker_ids == ["dead-z", "dead-a"]
    assert trace[-1] == ("recover", NOW, token)
    assert drain._scheduler_drains_since_maintenance == 0


def test_rm10_recovery_excludes_exact_blocked_barrier_token_id_set(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    class _BarrierRepository:
        def __init__(self, engine: object, *, events: object) -> None:
            calls.append(("init", str(engine)))

        def blocked_barrier_token_ids(self, *, run_id: str) -> frozenset[str]:
            calls.append(("blocked_barrier_token_ids", run_id))
            return frozenset(("token-z", "token-a"))

    monkeypatch.setattr(recovery_module, "BarrierJournalRepository", _BarrierRepository)
    manager: Any = object.__new__(recovery_module.RecoveryManager)
    manager._db = SimpleNamespace(engine="engine-sentinel")

    assert manager._get_buffered_journal_token_ids(RUN_ID) == {"token-z", "token-a"}
    assert calls == [("init", "engine-sentinel"), ("blocked_barrier_token_ids", RUN_ID)]


@pytest.mark.parametrize(("blocked_count", "expected"), [(0, False), (5, True)])
def test_rm11_resume_batch_repair_uses_exact_blocked_count(
    monkeypatch: pytest.MonkeyPatch,
    blocked_count: int,
    expected: bool,
) -> None:
    calls: list[tuple[str, str]] = []

    class _Recovery:
        def count_blocked_barrier_items(self, run_id: str) -> int:
            calls.append(("count", run_id))
            return blocked_count

    def _handle(execution: object, run_id: str) -> dict[str, str]:
        calls.append(("repair", run_id))
        assert execution == "execution-sentinel"
        return {"old": "retry"}

    monkeypatch.setattr(resume_module, "handle_incomplete_batches", _handle)
    coordinator: Any = object.__new__(ResumeCoordinator)
    snapshot = SimpleNamespace(
        factory=SimpleNamespace(execution="execution-sentinel"),
        recovery=_Recovery(),
        run_id=RUN_ID,
    )

    assert coordinator._repair_resume_batches(snapshot) == ({"old": "retry"}, expected)
    assert calls == [("repair", RUN_ID), ("count", RUN_ID)]


class _PendingBarrierScheduler:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows
        self.calls: list[str] = []

    def list_pending_blocked_barrier_items(self, *, run_id: str) -> list[object]:
        self.calls.append(run_id)
        return self.rows


def test_rm13_barrier_intake_adopts_the_exact_pending_listing_in_order() -> None:
    rows: list[object] = [SimpleNamespace(barrier_key="agg"), SimpleNamespace(barrier_key="agg")]
    scheduler = _PendingBarrierScheduler(rows)
    adopted: list[object] = []
    coordinator: Any = object.__new__(BarrierIntakeCoordinator)
    coordinator._scheduler = scheduler
    coordinator._run_id = RUN_ID
    coordinator._aggregation_settings = {"agg": object()}
    coordinator._coalesce_executor = None
    coordinator._row_union_executor = None
    coordinator._coalesce_node_ids = {}
    coordinator._row_union_node_ids = {}

    def _adopt(row: object, ctx: object) -> None:
        assert ctx == "context-sentinel"
        adopted.append(row)
        return None

    coordinator._adopt_aggregation_row = _adopt
    coordinator._replay_branch_losses = lambda: []

    assert coordinator.run_intake_pass("context-sentinel") == BarrierIntakePassOutcome(dispositions=())
    assert scheduler.calls == [RUN_ID]
    assert adopted == rows


@dataclass(frozen=True)
class _ArchitectureContract:
    rm_id: str
    path: str
    class_name: str | None
    function_name: str
    required_expressions: tuple[str, ...]


def _function_node(contract: _ArchitectureContract) -> ast.FunctionDef:
    path = REPO_ROOT / contract.path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    scope: ast.AST = tree
    if contract.class_name is not None:
        scope = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == contract.class_name)
    return next(node for node in ast.iter_child_nodes(scope) if isinstance(node, ast.FunctionDef) and node.name == contract.function_name)


@pytest.mark.parametrize(
    "contract",
    [
        pytest.param(
            _ArchitectureContract(
                "RM-04/RM-05",
                "src/elspeth/engine/scheduler_drain.py",
                "SchedulerDrainCoordinator",
                "drain_claims",
                (
                    "pending_ids = list(pending_items.keys())",
                    "self._scheduler.count_ready_in_set(run_id=self._run_id, work_item_ids=pending_ids)",
                    "self._scheduler.count_failed_in_set(run_id=self._run_id, work_item_ids=pending_ids)",
                    "self._scheduler.has_peer_owned_work(run_id=self._run_id, caller_owner=self._scheduler_lease_owner, work_item_ids=pending_ids)",
                    "ready_count == 0 and failed_count == 0",
                    "pending_items.clear()",
                ),
            ),
            id="RM-04-RM-05-continuation-hard-seam",
        ),
        pytest.param(
            _ArchitectureContract(
                "RM-07",
                "src/elspeth/engine/orchestrator/follower.py",
                "FollowerProcessor",
                "_drain_loop",
                (
                    "seat = self._run_coordination.live_leader(run_id=run_id, now=now)",
                    "seat is None or not seat.seat_live",
                    "raise _SeatDeadError(worker_id, run_id)",
                ),
            ),
            id="RM-07-follower-seat-hard-seam",
        ),
        pytest.param(
            _ArchitectureContract(
                "RM-12",
                "src/elspeth/engine/barrier_coordination.py",
                "BarrierRecoveryCoordinator",
                "restore_from_journal",
                (
                    "items = self._scheduler.list_blocked_barrier_items(run_id=self._run_id)",
                    "for item in items:",
                ),
            ),
            id="RM-12-restore-hard-seam",
        ),
        pytest.param(
            _ArchitectureContract(
                "RM-14",
                "src/elspeth/engine/orchestrator/resume.py",
                "ResumeCoordinator",
                "reconstruct_resume_state",
                (
                    "workset = snapshot.recovery.get_resume_workset(snapshot.run_id)",
                    "row_ids=workset.row_ids",
                    "incomplete_by_row=workset.incomplete_by_row",
                ),
            ),
            id="RM-14-resume-workset-hard-seam",
        ),
        pytest.param(
            _ArchitectureContract(
                "RM-14",
                "src/elspeth/engine/orchestrator/run_status.py",
                None,
                "derive_terminal_status_from_audit",
                (
                    "outcomes = factory.query.get_all_token_outcomes_for_run(run_id)",
                    "factory.run_status_projection.count_distinct_source_rows_with_terminal_outcome(run_id)",
                    "factory.run_status_projection.count_failed_coalesce_barrier_rows(run_id)",
                    "outcome_record.path is TerminalPath.ABANDONED",
                    "raise AuditIntegrityError",
                ),
            ),
            id="RM-14-terminal-projection-hard-seam",
        ),
        pytest.param(
            _ArchitectureContract(
                "RM-14",
                "src/elspeth/web/execution/accounting.py",
                None,
                "load_run_accounting_map_from_db",
                (
                    "func.count(case((token_outcomes_table.c.completed == 1, 1))).label('completed_count')",
                    "func.count(case((token_outcomes_table.c.path == TerminalPath.ABANDONED.value, 1))).label('abandoned_count')",
                    "outcomes_by_emitted_token.c.completed_count > 0",
                    "outcomes_by_emitted_token.c.abandoned_count > 0",
                    "pending=pending_tokens",
                    "abandoned=abandoned_tokens[run_id]",
                    "closure=closure",
                ),
            ),
            id="RM-14-accounting-census-hard-seam",
        ),
    ],
)
def test_hard_orchestration_seams_bind_owner_selector_to_exact_decision(
    contract: _ArchitectureContract,
) -> None:
    """Explicit architecture gates for seams too broad for isolated composition.

    These gates establish only production wiring, never repository truth-table
    behavior.  The paired core tests own the selector results themselves.
    """
    function = _function_node(contract)
    source = ast.unparse(function)

    for expression in contract.required_expressions:
        assert expression in source, f"{contract.rm_id} lost production consumer expression: {expression}"
