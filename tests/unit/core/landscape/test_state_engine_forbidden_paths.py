"""Complete-image refusal contracts for state-engine forbidden paths.

This module owns the Task 7 forbidden-path cohort only: F-04, F-06, F-07,
F-10, and F-12.  Every behavioral refusal compares the complete run-owned
durable image.  A valid leader-fenced no-op may extend the leader seat, while
a stale leader may add only its contracted best-effort ``fence_refusal``
event; neither delta is mislabeled as payload mutation.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert, update
from sqlalchemy.exc import IntegrityError

from elspeth.contracts import NodeType, RunStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.errors import (
    AuditIntegrityError,
    RunLeadershipLostError,
    RunWorkerEvictedError,
)
from elspeth.contracts.scheduler import BarrierEmission, TokenWorkStatus
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.core.landscape import database as database_module
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import (
    run_coordination_table,
    run_workers_table,
    token_work_items_table,
)
from tests.fixtures.landscape import leader_coordination_token, make_factory, make_landscape_db, register_test_node
from tests.helpers.state_engine import StateEngineImage, capture_state_engine_image

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
RUN_ID = "forbidden-path-run"
LEADER = f"worker:{RUN_ID}:leader"
WRONG_OWNER = f"worker:{RUN_ID}:wrong-owner"
SOURCE_NODE_ID = "source-1"
NODE_ID = "transform-1"
ROOT = Path(__file__).resolve().parents[4]
PAYLOAD = TokenSchedulerRepository.serialize_row_payload(PipelineRow({"value": 1}, SchemaContract(mode="OBSERVED", fields=(), locked=True)))


@dataclass(frozen=True)
class _Harness:
    db: LandscapeDB
    factory: RecorderFactory
    repo: TokenSchedulerRepository
    coordination_token: CoordinationToken


@pytest.fixture
def harness() -> Iterator[_Harness]:
    db = make_landscape_db()
    factory = make_factory(db)
    factory.run_lifecycle.begin_run(
        config={},
        canonical_version="v1",
        run_id=RUN_ID,
        leader_worker_id=LEADER,
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
    )
    factory.data_flow.register_node(
        run_id=RUN_ID,
        plugin_name="source",
        node_type=NodeType.SOURCE,
        plugin_version="1.0",
        config={},
        node_id=SOURCE_NODE_ID,
        schema_config=SchemaConfig.from_dict({"mode": "observed"}),
    )
    register_test_node(factory.data_flow, RUN_ID, NODE_ID)
    try:
        yield _Harness(
            db=db,
            factory=factory,
            repo=factory.scheduler,
            coordination_token=leader_coordination_token(factory, RUN_ID),
        )
    finally:
        db.close()


def _enqueue(harness: _Harness, name: str, sequence: int) -> tuple[str, str, str]:
    row, token = harness.factory.data_flow.create_row_with_token(
        run_id=RUN_ID,
        source_node_id=SOURCE_NODE_ID,
        row_index=sequence,
        data={"name": name},
        source_row_index=sequence,
        ingest_sequence=sequence,
        row_id=f"row-{name}",
        token_id=f"token-{name}",
    )
    item = harness.repo.enqueue_ready(
        run_id=RUN_ID,
        token_id=token.token_id,
        row_id=row.row_id,
        node_id=NODE_ID,
        step_index=1,
        ingest_sequence=sequence,
        available_at=NOW,
        row_payload_json=PAYLOAD,
    )
    return row.row_id, token.token_id, item.work_item_id


def _claim(harness: _Harness, name: str = "parent", *, owner: str = LEADER) -> tuple[str, str, str]:
    row_id, token_id, work_item_id = _enqueue(harness, name, 0)
    claimed = harness.repo.claim_ready(run_id=RUN_ID, lease_owner=owner, lease_seconds=60, now=NOW)
    assert claimed is not None and claimed.work_item_id == work_item_id
    return row_id, token_id, work_item_id


def _ready_child(harness: _Harness) -> BarrierEmission:
    row, token = harness.factory.data_flow.create_row_with_token(
        run_id=RUN_ID,
        source_node_id=SOURCE_NODE_ID,
        row_index=1,
        data={"name": "child"},
        source_row_index=1,
        ingest_sequence=1,
        row_id="row-child",
        token_id="token-child",
    )
    return BarrierEmission(
        token_id=token.token_id,
        row_id=row.row_id,
        node_id=NODE_ID,
        step_index=2,
        ingest_sequence=1,
        row_payload_json=PAYLOAD,
    )


def _mark_blocked(repo: TokenSchedulerRepository, work_item_id: str, _: BarrierEmission | None) -> object:
    return repo.mark_blocked(
        work_item_id=work_item_id,
        queue_key="queue-a",
        barrier_key=None,
        now=NOW + timedelta(seconds=1),
        expected_lease_owner=WRONG_OWNER,
    )


def _mark_terminal(repo: TokenSchedulerRepository, work_item_id: str, _: BarrierEmission | None) -> object:
    return repo.mark_terminal(
        work_item_id=work_item_id,
        now=NOW + timedelta(seconds=1),
        expected_lease_owner=WRONG_OWNER,
    )


def _mark_failed(repo: TokenSchedulerRepository, work_item_id: str, _: BarrierEmission | None) -> object:
    return repo.mark_failed(
        work_item_id=work_item_id,
        now=NOW + timedelta(seconds=1),
        expected_lease_owner=WRONG_OWNER,
    )


def _mark_pending_sink(repo: TokenSchedulerRepository, work_item_id: str, _: BarrierEmission | None) -> object:
    return repo.mark_pending_sink(
        work_item_id=work_item_id,
        row_payload_json=PAYLOAD,
        sink_name="sink-a",
        outcome=TerminalOutcome.SUCCESS.value,
        path=TerminalPath.DEFAULT_FLOW.value,
        error_hash=None,
        error_message=None,
        now=NOW + timedelta(seconds=1),
        expected_lease_owner=WRONG_OWNER,
    )


def _mark_terminal_with_ready_children(
    repo: TokenSchedulerRepository,
    work_item_id: str,
    child: BarrierEmission | None,
) -> object:
    assert child is not None
    return repo.mark_terminal_with_ready_children(
        work_item_id=work_item_id,
        emitted_ready=(child,),
        now=NOW + timedelta(seconds=1),
        expected_lease_owner=WRONG_OWNER,
    )


def _mark_failed_with_ready_children(
    repo: TokenSchedulerRepository,
    work_item_id: str,
    child: BarrierEmission | None,
) -> object:
    assert child is not None
    return repo.mark_failed_with_ready_children(
        work_item_id=work_item_id,
        emitted_ready=(child,),
        now=NOW + timedelta(seconds=1),
        expected_lease_owner=WRONG_OWNER,
    )


def _mark_pending_sink_with_ready_children(
    repo: TokenSchedulerRepository,
    work_item_id: str,
    child: BarrierEmission | None,
) -> object:
    assert child is not None
    return repo.mark_pending_sink_with_ready_children(
        work_item_id=work_item_id,
        emitted_ready=(child,),
        row_payload_json=PAYLOAD,
        sink_name="sink-a",
        outcome=TerminalOutcome.SUCCESS.value,
        path=TerminalPath.DEFAULT_FLOW.value,
        error_hash=None,
        error_message=None,
        now=NOW + timedelta(seconds=1),
        expected_lease_owner=WRONG_OWNER,
    )


_WrongOwnerDisposition = Callable[[TokenSchedulerRepository, str, BarrierEmission | None], object]


@pytest.mark.parametrize(
    ("disposition", "needs_child"),
    (
        pytest.param(_mark_blocked, False, id="F-04-mark-blocked"),
        pytest.param(_mark_terminal, False, id="F-04-mark-terminal"),
        pytest.param(_mark_failed, False, id="F-04-mark-failed"),
        pytest.param(_mark_pending_sink, False, id="F-04-mark-pending-sink"),
        pytest.param(_mark_terminal_with_ready_children, True, id="F-04-mark-terminal-with-ready-children"),
        pytest.param(_mark_failed_with_ready_children, True, id="F-04-mark-failed-with-ready-children"),
        pytest.param(_mark_pending_sink_with_ready_children, True, id="F-04-mark-pending-sink-with-ready-children"),
    ),
)
def test_f04_wrong_owner_cannot_disposition_a_lease(
    harness: _Harness,
    disposition: _WrongOwnerDisposition,
    needs_child: bool,
) -> None:
    _row_id, _token_id, work_item_id = _claim(harness)
    child = _ready_child(harness) if needs_child else None
    before = capture_state_engine_image(harness.db, run_id=RUN_ID)

    with pytest.raises(AuditIntegrityError, match=r"expected lease_owner.*wrong-owner"):
        disposition(harness.repo, work_item_id, child)

    assert capture_state_engine_image(harness.db, run_id=RUN_ID) == before


@pytest.mark.parametrize("batch", (False, True), ids=("singleton", "batch"))
def test_f04_wrong_owner_cannot_terminalize_pending_sink_debt_without_mutation(
    harness: _Harness,
    batch: bool,
) -> None:
    _row_id, token_id, work_item_id = _claim(harness)
    harness.repo.mark_pending_sink(
        work_item_id=work_item_id,
        row_payload_json=PAYLOAD,
        sink_name="sink-a",
        outcome=TerminalOutcome.SUCCESS.value,
        path=TerminalPath.DEFAULT_FLOW.value,
        error_hash=None,
        error_message=None,
        now=NOW + timedelta(seconds=1),
        expected_lease_owner=LEADER,
    )
    before = capture_state_engine_image(harness.db, run_id=RUN_ID)

    if batch:
        with pytest.raises(AuditIntegrityError, match="strict owner CAS"):
            harness.repo.mark_pending_sink_terminal_many(
                run_id=RUN_ID,
                token_ids=(token_id,),
                now=NOW + timedelta(seconds=2),
                expected_lease_owner=WRONG_OWNER,
                coordination_token=harness.coordination_token,
            )
    else:
        assert (
            harness.repo.mark_pending_sink_terminal(
                run_id=RUN_ID,
                token_id=token_id,
                now=NOW + timedelta(seconds=2),
                expected_lease_owner=WRONG_OWNER,
                coordination_token=harness.coordination_token,
            )
            == 0
        )

    assert capture_state_engine_image(harness.db, run_id=RUN_ID) == before


@pytest.mark.parametrize("status", ("departed", "evicted"))
@pytest.mark.parametrize("subtype", ("ready", "pending_sink"))
def test_f06_inactive_registered_worker_cannot_claim(
    harness: _Harness,
    status: str,
    subtype: str,
) -> None:
    _row_id, _token_id, work_item_id = _enqueue(harness, "claim", 0)
    if subtype == "pending_sink":
        claimed = harness.repo.claim_ready(run_id=RUN_ID, lease_owner=LEADER, lease_seconds=60, now=NOW)
        assert claimed is not None
        harness.repo.mark_pending_sink(
            work_item_id=work_item_id,
            row_payload_json=PAYLOAD,
            sink_name="sink-a",
            outcome=TerminalOutcome.SUCCESS.value,
            path=TerminalPath.DEFAULT_FLOW.value,
            error_hash=None,
            error_message=None,
            now=NOW,
            expected_lease_owner=LEADER,
        )
    inactive = f"worker:{RUN_ID}:{status}"
    with harness.db.engine.begin() as conn:
        conn.execute(
            insert(run_workers_table).values(
                worker_id=inactive,
                run_id=RUN_ID,
                role="follower",
                status=status,
                registered_at=NOW,
                heartbeat_expires_at=NOW + timedelta(hours=1),
                departed_at=NOW if status == "departed" else None,
                evicted_at=NOW if status == "evicted" else None,
            )
        )
    before = capture_state_engine_image(harness.db, run_id=RUN_ID)

    claim = harness.repo.claim_ready if subtype == "ready" else harness.repo.claim_pending_sink
    with pytest.raises(RunWorkerEvictedError) as raised:
        claim(run_id=RUN_ID, lease_owner=inactive, lease_seconds=60, now=NOW + timedelta(seconds=1))

    assert raised.value.worker_id == inactive
    assert raised.value.run_id == RUN_ID
    assert capture_state_engine_image(harness.db, run_id=RUN_ID) == before


@pytest.mark.parametrize("subtype", ("ready", "pending_sink"))
def test_f06_absent_worker_claim_is_a_zero_mutation_none(subtype: str, harness: _Harness) -> None:
    _row_id, _token_id, work_item_id = _enqueue(harness, "claim", 0)
    if subtype == "pending_sink":
        claimed = harness.repo.claim_ready(run_id=RUN_ID, lease_owner=LEADER, lease_seconds=60, now=NOW)
        assert claimed is not None
        harness.repo.mark_pending_sink(
            work_item_id=work_item_id,
            row_payload_json=PAYLOAD,
            sink_name="sink-a",
            outcome=TerminalOutcome.SUCCESS.value,
            path=TerminalPath.DEFAULT_FLOW.value,
            error_hash=None,
            error_message=None,
            now=NOW,
            expected_lease_owner=LEADER,
        )
    before = capture_state_engine_image(harness.db, run_id=RUN_ID)

    claim = harness.repo.claim_ready if subtype == "ready" else harness.repo.claim_pending_sink
    assert claim(run_id=RUN_ID, lease_owner="unregistered", lease_seconds=60, now=NOW + timedelta(seconds=1)) is None

    assert capture_state_engine_image(harness.db, run_id=RUN_ID) == before


def _register_peer(harness: _Harness, peer: str, *, status: str = "active") -> None:
    with harness.db.engine.begin() as conn:
        conn.execute(
            insert(run_workers_table).values(
                worker_id=peer,
                run_id=RUN_ID,
                role="follower",
                status=status,
                registered_at=NOW,
                heartbeat_expires_at=NOW + timedelta(minutes=10),
                departed_at=NOW if status == "departed" else None,
            )
        )


def _assert_only_leader_heartbeat_delta(before: StateEngineImage, after: StateEngineImage) -> None:
    delta = before.diff(after)
    assert delta.changed_tables == {"run_coordination"}
    assert delta.changed_columns == {
        "run_coordination": {"leader_heartbeat_expires_at", "updated_at"},
    }


@pytest.mark.parametrize(
    ("owner", "worker_status", "recover_at"),
    (
        pytest.param(LEADER, None, NOW + timedelta(seconds=61), id="F-07-own-expired-lease"),
        pytest.param("live-peer", "active", NOW + timedelta(seconds=31), id="F-07-live-peer-fresh-expiry"),
        pytest.param("dead-peer", "departed", NOW + timedelta(seconds=20), id="F-07-not-yet-expired"),
    ),
)
def test_f07_ineligible_lease_is_not_recovered(
    harness: _Harness,
    owner: str,
    worker_status: str | None,
    recover_at: datetime,
) -> None:
    if worker_status is not None:
        _register_peer(harness, owner)
    _row_id, _token_id, work_item_id = _claim(harness, owner=owner)
    if worker_status == "departed":
        with harness.db.engine.begin() as conn:
            conn.execute(
                update(run_workers_table)
                .where(run_workers_table.c.worker_id == owner)
                .where(run_workers_table.c.run_id == RUN_ID)
                .values(status="departed", departed_at=NOW)
            )
    before = capture_state_engine_image(harness.db, run_id=RUN_ID)

    recovered = harness.repo.recover_expired_leases(
        now=recover_at,
        coordination_token=harness.coordination_token,
    )

    assert recovered == 0
    after = capture_state_engine_image(harness.db, run_id=RUN_ID)
    _assert_only_leader_heartbeat_delta(before, after)
    work = next(row for row in after.tables["token_work_items"] if row["work_item_id"] == work_item_id)
    assert work["status"] == TokenWorkStatus.LEASED.value
    assert work["lease_owner"] == owner


def _depose_leader(harness: _Harness) -> StateEngineImage:
    with harness.db.engine.begin() as conn:
        conn.execute(
            update(run_coordination_table)
            .where(run_coordination_table.c.run_id == RUN_ID)
            .values(leader_epoch=run_coordination_table.c.leader_epoch + 1)
        )
    return capture_state_engine_image(harness.db, run_id=RUN_ID)


def _assert_only_fence_refusal(
    harness: _Harness,
    before: StateEngineImage,
    *,
    verb: str,
) -> None:
    after = capture_state_engine_image(harness.db, run_id=RUN_ID)
    delta = before.diff(after)
    assert delta.changed_tables == {"run_coordination_events"}
    added = tuple(row for row in after.tables["run_coordination_events"] if row not in before.tables["run_coordination_events"])
    assert len(added) == 1
    assert added[0]["event_type"] == "fence_refusal"
    assert json.loads(str(added[0]["context_json"])) == {"verb": verb}
    assert after.tables["runs"] == before.tables["runs"]


def test_f10_stale_leader_refusal_adds_only_fence_refusal_event(harness: _Harness) -> None:
    before = _depose_leader(harness)

    with pytest.raises(RunLeadershipLostError) as raised:
        harness.factory.run_lifecycle.update_run_status(
            RUN_ID,
            RunStatus.FAILED,
            token=harness.coordination_token,
        )

    assert raised.value.verb == "update_run_status"
    _assert_only_fence_refusal(harness, before, verb="update_run_status")


def test_f10_stale_leader_cannot_reconcile_source_completions(harness: _Harness) -> None:
    """The TS-02 repair surface is leader-fenced like every other repair verb."""
    before = _depose_leader(harness)

    with pytest.raises(RunLeadershipLostError) as raised:
        harness.factory.execution.reconcile_source_completions_from_scheduler(
            run_id=RUN_ID,
            coordination_token=harness.coordination_token,
            at=NOW + timedelta(seconds=1),
        )

    assert raised.value.verb == "reconcile_source_completions_from_scheduler"
    _assert_only_fence_refusal(harness, before, verb="reconcile_source_completions_from_scheduler")


def test_f10_fenced_verb_inventory_has_retained_stale_refusal_coverage() -> None:
    """No new leader-fenced mutation surface may escape the F-10 cohort.

    The shared fence is complete-image tested above and the retained per-verb
    suites exercise the setup needed to make every payload write eligible.
    This architecture gate binds that evidence to the complete live caller
    inventory so adding a fenced verb requires adding stale-token evidence.
    """
    expected = {
        "adopt_blocked_barrier_item",
        "adopt_group_losses",
        "complete_barrier",
        "complete_run",
        "create_checkpoint",
        "create_row_with_token",
        "delete_checkpoints",
        "evict_worker",
        "ingest_row_with_initial_claim",
        "mark_pending_sink_terminal",
        "mark_pending_sink_terminal_many",
        "reconcile_source_completions_from_scheduler",
        "recover_expired_leases",
        "stage_escalation_loss",
        "terminalize_pending_sinks_with_terminal_outcomes",
        "update_run_status",
    }
    actual: set[str] = set()
    for source_path in sorted((ROOT / "src/elspeth").rglob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = (
                node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else None
            )
            if function_name not in {"fenced_leader_transaction", "fenced_write", "_fenced_or_plain_write"}:
                continue
            for keyword in node.keywords:
                if keyword.arg == "verb" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                    actual.add(keyword.value.value)
    assert actual == expected

    retained_tests = {
        "adopt_blocked_barrier_item": (
            "tests/unit/core/landscape/test_scheduler_repository_adopt_barrier_item.py",
            "test_stale_token_refused_with_fence_refusal_and_zero_mutation",
        ),
        "adopt_group_losses": (
            "tests/e2e/recovery/test_suspended_winner_fences.py",
            "test_stale_adopt_group_losses_refused",
        ),
        "complete_barrier": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_complete_barrier_strict_arm_refused",
        ),
        "complete_run": ("tests/unit/core/landscape/test_leader_fence_stale_token.py", "test_complete_run_refused"),
        "create_checkpoint": ("tests/unit/core/landscape/test_leader_fence_stale_token.py", "test_create_checkpoint_refused"),
        "create_row_with_token": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_fenced_create_row_with_token_refused",
        ),
        "delete_checkpoints": ("tests/unit/core/landscape/test_leader_fence_stale_token.py", "test_delete_checkpoints_refused"),
        "evict_worker": (
            "tests/unit/core/landscape/test_run_coordination_repository.py",
            "test_evict_worker_grace_predicate_and_fence",
        ),
        "ingest_row_with_initial_claim": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_ingest_woken_mid_ingest_atomic_rollback",
        ),
        "mark_pending_sink_terminal": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_mark_pending_sink_terminal_refused",
        ),
        "mark_pending_sink_terminal_many": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_mark_pending_sink_terminal_many_refused",
        ),
        "reconcile_source_completions_from_scheduler": (
            "tests/unit/core/landscape/test_state_engine_forbidden_paths.py",
            "test_f10_stale_leader_cannot_reconcile_source_completions",
        ),
        "recover_expired_leases": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_recover_expired_leases_refused",
        ),
        "stage_escalation_loss": (
            "tests/e2e/recovery/test_suspended_winner_fences.py",
            "test_stale_stage_escalation_loss_refused",
        ),
        "terminalize_pending_sinks_with_terminal_outcomes": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_terminalize_pending_sinks_refused",
        ),
        "update_run_status": (
            "tests/unit/core/landscape/test_state_engine_forbidden_paths.py",
            "test_f10_stale_leader_refusal_adds_only_fence_refusal_event",
        ),
    }
    assert set(retained_tests) == expected
    for verb, (relative_path, test_name) in retained_tests.items():
        test_path = ROOT / relative_path
        test_tree = ast.parse(test_path.read_text(encoding="utf-8"), filename=str(test_path))
        matches = [
            node for node in ast.walk(test_tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == test_name
        ]
        assert len(matches) == 1, f"F-10 verb {verb!r} must bind one retained test function {test_name!r}"
        test_source = ast.unparse(matches[0])
        local_functions = {node.name: node for node in ast.walk(test_tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        reachable_calls: set[str] = set()
        pending = [matches[0]]
        visited: set[str] = set()
        while pending:
            function = pending.pop()
            if function.name in visited:
                continue
            visited.add(function.name)
            for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
                call_name = (
                    call.func.id if isinstance(call.func, ast.Name) else call.func.attr if isinstance(call.func, ast.Attribute) else None
                )
                if call_name is None:
                    continue
                reachable_calls.add(call_name)
                if call_name in local_functions:
                    pending.append(local_functions[call_name])
        assert verb in reachable_calls, f"F-10 test {test_name!r} no longer invokes {verb!r}, directly or through a local helper"
        assert "pytest.raises(RunLeadershipLostError)" in test_source, f"F-10 test {test_name!r} no longer asserts stale-token refusal"


def test_f12_waiting_state_is_rejected_by_storage_without_mutation(harness: _Harness) -> None:
    _row_id, _token_id, work_item_id = _enqueue(harness, "waiting", 0)
    before = capture_state_engine_image(harness.db, run_id=RUN_ID)

    with pytest.raises(IntegrityError, match="ck_token_work_items_status"), harness.db.engine.begin() as conn:
        conn.execute(update(token_work_items_table).where(token_work_items_table.c.work_item_id == work_item_id).values(status="waiting"))

    assert capture_state_engine_image(harness.db, run_id=RUN_ID) == before
    assert ("token_work_items", "ck_token_work_items_status") in database_module._REQUIRED_CHECK_CONSTRAINTS


def test_f12_waiting_is_unreachable_from_scheduler_and_restore_code() -> None:
    assert {status.value for status in TokenWorkStatus} == {
        "ready",
        "leased",
        "blocked",
        "pending_sink",
        "terminal",
        "failed",
    }
    production_paths = (
        ROOT / "src/elspeth/core/landscape/scheduler",
        ROOT / "src/elspeth/core/landscape/scheduler_repository.py",
        ROOT / "src/elspeth/engine/orchestrator/resume.py",
    )
    offenders: list[str] = []
    for path in production_paths:
        files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
        for file_path in files:
            tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value == "waiting":
                    offenders.append(f"{file_path.relative_to(ROOT)}:{node.lineno}:string")
                if isinstance(node, ast.Attribute) and node.attr == "WAITING":
                    offenders.append(f"{file_path.relative_to(ROOT)}:{node.lineno}:attribute")
    assert offenders == []
