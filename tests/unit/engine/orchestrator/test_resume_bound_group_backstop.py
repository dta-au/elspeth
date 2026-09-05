# tests/unit/engine/orchestrator/test_resume_bound_group_backstop.py
"""elspeth-76e936568e — the resume arm of the end-of-run bound-group backstop
is load-bearing.

``ResumeCoordinator._finalize_successful_resume`` calls
``assert_bound_groups_settled_from_audit`` BEFORE it derives the terminal
status, so a resumed run whose bound member never settled takes the resume
failure ceremony instead of converging. The fresh-run arm is pinned end to
end in ``tests/integration/pipeline/test_unsettled_group_end_of_run_backstop``;
the resume arm was not: deleting the call survived 22 tests (verification
seat, 2026-09-05), because every resume test either stubs a graph with no
bound groups or patches the derivation and never looks at the backstop.

These two pins are the deleting-the-call oracle. The control records the
backstop being called with the coordinator's own db, the run id and the
supplied graph, and that it ran before the terminal status was derived. The
refusal drives the same resume with the backstop raising, and asserts the
verdict propagates, the terminal status is never derived, and the run is
finalized FAILED by the resume failure ceremony. Delete the call and the
control sees no backstop call while the refusal converges COMPLETED.

Harness: the empty-journal early-completion resume from
``test_resume_failure`` — no unprocessed rows, no BLOCKED barrier work — so
``resume()`` reaches ``_finalize_successful_resume`` without a processing
pass. The graph stub carries an empty binding registry for the ENTRY guard
(which is real); the backstop itself is patched at its import site in
``resume.py`` because its semantics are pinned in ``test_run_status`` and
``tests/unit/core/checkpoint``; this file pins only the wiring.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from elspeth.contracts import Checkpoint, NodeID, ResumePoint, RunStatus
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.core.checkpoint.recovery import RecoveryManager
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.group_bindings import GroupBindingRegistry
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.engine.orchestrator.core import Orchestrator
from elspeth.engine.orchestrator.run_state import ResumeState
from elspeth.engine.orchestrator.run_status import (
    assert_bound_groups_settled_from_audit,
    derive_resume_terminal_status_from_audit,
)
from elspeth.engine.orchestrator.types import ExecutionCounters
from tests.fixtures.landscape import make_landscape_db
from tests.fixtures.stores import MockPayloadStore
from tests.unit.engine.orchestrator.test_resume_failure import (
    _admit_resume_point,
    _insert_failed_run,
    _make_heartbeat_safe_token,
    _make_orchestrator,
)

_BACKSTOP = "elspeth.engine.orchestrator.resume.assert_bound_groups_settled_from_audit"
_DERIVE = "elspeth.engine.orchestrator.resume.derive_resume_terminal_status_from_audit"


class _EarlyCompletionResume:
    """One early-completion resume, ready to drive, with its collaborators exposed."""

    def __init__(self, run_id: str) -> None:
        self.db = make_landscape_db()
        self.orch: Orchestrator = _make_orchestrator(self.db)
        self.run_id = run_id
        _insert_failed_run(self.db, run_id)
        self.factory = MagicMock(spec=RecorderFactory)
        self.factory.scheduler.count_active_work.return_value = 0
        self.factory.data_flow.sweep_deferred_invariants_or_crash = MagicMock(spec=object)
        self.factory.run_lifecycle.finalize_run = MagicMock(spec=object)
        self.token = _make_heartbeat_safe_token(run_id, self.factory)
        checkpoint = Checkpoint(
            checkpoint_id=f"cp-{run_id}",
            run_id=run_id,
            sequence_number=1,
            created_at=datetime.now(UTC),
            upstream_topology_hash="a" * 64,
            format_version=Checkpoint.CURRENT_FORMAT_VERSION,
        )
        self.resume_point = ResumePoint(checkpoint=checkpoint, sequence_number=checkpoint.sequence_number)
        self.resume_state = ResumeState(
            factory=self.factory,
            run_id=run_id,
            unprocessed_rows=(),
            incomplete_by_row={},
            recovery_manager=MagicMock(spec=RecoveryManager),
            schema_contracts_by_source={NodeID("source"): MagicMock(spec=SchemaContract)},
            source_names_by_source={NodeID("source"): "source"},
            source_lifecycle_by_source={NodeID("source"): "loaded"},
            has_restored_barrier_work=False,
            coordination_token=self.token,
        )
        self.graph = MagicMock(spec=ExecutionGraph)
        self.graph.get_group_bindings.return_value = GroupBindingRegistry(bindings=())

    def drive(self, *, backstop: Any, derive: Any) -> Any:
        with (
            _admit_resume_point(self.orch, self.resume_point),
            patch.object(self.orch._resume_coordinator, "reconstruct_resume_state", return_value=self.resume_state),
            patch.object(self.orch._resume_coordinator, "process_resumed_rows", side_effect=AssertionError("early exit expected")),
            patch(_BACKSTOP, new=backstop),
            patch(_DERIVE, new=derive),
            patch.object(self.orch._ceremony, "emit_telemetry"),
            patch.object(self.orch._checkpoints, "delete_checkpoints"),
        ):
            return self.orch.resume(self.resume_point, MagicMock(spec=object), self.graph, payload_store=MockPayloadStore())


def test_resume_finalize_asks_the_backstop_before_deriving_the_terminal_status() -> None:
    """Control and ordering pin: the backstop is called with the coordinator's
    db, the run id and the supplied graph, and it runs BEFORE derivation."""
    harness = _EarlyCompletionResume("run-backstop-order")
    order: list[str] = []

    def backstop(db: Any, run_id: str, graph: Any) -> None:
        assert db is harness.orch._resume_coordinator._db
        assert run_id == harness.run_id
        assert graph is harness.graph
        order.append("backstop")

    def derive(factory: Any, run_id: str) -> tuple[RunStatus, ExecutionCounters]:
        assert factory is harness.factory
        order.append("derive")
        return RunStatus.COMPLETED, ExecutionCounters(rows_processed=3, rows_succeeded=3)

    result = harness.drive(backstop=backstop, derive=derive)

    assert order == ["backstop", "derive"], order  # deleting the call: ["derive"]
    assert result.status == RunStatus.COMPLETED
    harness.factory.run_lifecycle.finalize_run.assert_called_once_with(harness.run_id, status=RunStatus.COMPLETED, token=harness.token)


def test_resume_finalize_refuses_an_unsettled_bound_group_and_finalizes_failed() -> None:
    harness = _EarlyCompletionResume("run-backstop-refuse")
    verdict = "Run 'run-backstop-refuse' reached end of run with bound-group members that never settled (elspeth-76e936568e)"
    # Specced against the real callables so the stand-ins refuse a call that
    # the real signatures would (mock-discipline gate, elspeth-bc97e06221 B6).
    backstop = MagicMock(spec=assert_bound_groups_settled_from_audit, side_effect=OrchestrationInvariantError(verdict))
    derive = MagicMock(
        spec=derive_resume_terminal_status_from_audit,
        return_value=(RunStatus.COMPLETED, ExecutionCounters(rows_processed=3, rows_succeeded=3)),
    )

    with pytest.raises(OrchestrationInvariantError, match="never settled"):
        harness.drive(backstop=backstop, derive=derive)

    backstop.assert_called_once()
    derive.assert_not_called()  # the verdict pre-empts the terminal-status derivation
    statuses = [call.kwargs["status"] for call in harness.factory.run_lifecycle.finalize_run.call_args_list]
    assert statuses == [RunStatus.FAILED], statuses  # the resume failure ceremony, never COMPLETED
