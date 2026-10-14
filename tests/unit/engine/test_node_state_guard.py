"""Node-state guard audit lifecycle, failure attribution, and telemetry tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from elspeth.contracts.enums import (
    NodeStateStatus,
)
from elspeth.contracts.errors import (
    OrchestrationInvariantError,
    PassThroughContractViolation,
)
from tests.fixtures.executors import (
    _make_factory,
)
from tests.fixtures.factories import make_context
from tests.fixtures.landscape import make_recorder_with_run, register_test_node

# =============================================================================
# TestNodeStateGuard
# =============================================================================


class TestNodeStateGuard:
    """Unit tests for NodeStateGuard context manager.

    The guard encodes a structural invariant: every opened node_state reaches
    exactly one terminal status (COMPLETED or FAILED).  These tests verify
    the guard's behavior in isolation from executor logic.

    Regression: B1/B2 — executor terminality bugs where post-processing
    failures left node_states permanently OPEN in the audit trail.
    """

    @pytest.mark.parametrize("authority", ["leader", "member"])
    def test_repository_authority_refusal_keeps_open_state_and_original_exception(self, authority: str) -> None:
        from elspeth.contracts.errors import RunLeadershipLostError, RunMembershipLostError
        from elspeth.engine.executors.state_guard import NodeStateGuard

        setup = make_recorder_with_run(source_node_id="source-0")
        register_test_node(setup.data_flow, setup.run_id, "transform-1")
        _, token = setup.data_flow.create_row_with_token(
            setup.source_node_id,
            0,
            {"value": 1},
            source_row_index=0,
            ingest_sequence=0,
            coordination_token=setup.coordination_token,
        )
        guard = NodeStateGuard(
            setup.execution,
            token_id=token.token_id,
            node_id="transform-1",
            member_token=setup.coordination_token.membership,
            step_index=1,
            input_data={"value": 1},
            auto_fail_phase="transform_execution",
        )
        refused: list[Exception] = []
        expected = RunLeadershipLostError if authority == "leader" else RunMembershipLostError
        with pytest.raises(expected) as propagated, guard:
            setup.factory.run_coordination.release_seat(token=setup.coordination_token)
            try:
                if authority == "leader":
                    setup.execution.begin_operation(
                        coordination_token=setup.coordination_token,
                        node_id="transform-1",
                        operation_type="source_load",
                    )
                else:
                    guard.complete(NodeStateStatus.COMPLETED, output_data={"value": 1})
            except expected as exc:
                refused.append(exc)
                raise

        assert propagated.value is refused[0]
        assert setup.execution.get_node_state(guard.state_id).status is NodeStateStatus.OPEN

    def test_auto_fail_phase_is_required_at_construction(self) -> None:
        """Every caller must name its guarded scope; there is no safe fallback."""
        from elspeth.engine.executors.state_guard import NodeStateGuard

        factory = _make_factory()
        with pytest.raises(TypeError, match="auto_fail_phase"):
            NodeStateGuard(
                factory.execution,
                token_id="tok_1",
                node_id="node_1",
                member_token=make_context(run_id="run_1").require_member_token(),
                step_index=1,
                input_data={"v": 1},
            )

        factory.execution.begin_node_state.assert_not_called()

    @pytest.mark.parametrize(
        "invalid_phase",
        [
            pytest.param("", id="empty"),
            pytest.param("   ", id="whitespace"),
            pytest.param(17, id="truthy-non-string"),
            pytest.param("transform_execuion", id="unknown-string"),
        ],
    )
    def test_auto_fail_phase_rejects_values_outside_closed_vocabulary(self, invalid_phase: Any) -> None:
        """Invalid phase attribution fails before opening or completing audit state."""
        from elspeth.engine.executors.state_guard import NodeStateGuard

        factory = _make_factory()
        with pytest.raises(OrchestrationInvariantError, match="auto_fail_phase"):
            NodeStateGuard(
                factory.execution,
                token_id="tok_1",
                node_id="node_1",
                member_token=make_context(run_id="run_1").require_member_token(),
                step_index=1,
                input_data={"v": 1},
                auto_fail_phase=invalid_phase,
            )

        factory.execution.begin_node_state.assert_not_called()
        factory.execution.complete_node_state.assert_not_called()

    def test_normal_exit_without_complete_crashes_and_records_failed(self) -> None:
        """Clean exit without complete() records FAILED and raises.

        The guard structurally enforces that every code path calls complete().
        If the block exits normally without calling it, the guard:
        1. Completes the state as FAILED (preserves audit invariant)
        2. Raises OrchestrationInvariantError (crash on our bug)
        """
        from elspeth.contracts.errors import OrchestrationInvariantError
        from elspeth.engine.executors.state_guard import NodeStateGuard

        factory = _make_factory()
        guard = NodeStateGuard(
            factory.execution,
            token_id="tok_1",
            node_id="node_1",
            member_token=make_context(run_id="run_1").require_member_token(),
            step_index=1,
            input_data={"v": 1},
            auto_fail_phase="transform_execution",
        )
        with pytest.raises(OrchestrationInvariantError, match="exited without complete"), guard:
            pass  # Don't call complete()

        # begin_node_state was called (in __enter__)
        factory.execution.begin_node_state.assert_called_once()
        # complete_node_state WAS called — state recorded as FAILED before crash
        factory.execution.complete_node_state.assert_called_once()
        kwargs = factory.execution.complete_node_state.call_args[1]
        assert kwargs["status"] == NodeStateStatus.FAILED
        assert kwargs["error"].phase == "executor_guard_missing_complete"

    def test_exception_auto_completes_as_failed(self) -> None:
        """Unhandled exception triggers auto-complete as FAILED."""
        from elspeth.engine.executors.state_guard import NodeStateGuard

        factory = _make_factory()
        guard = NodeStateGuard(
            factory.execution,
            token_id="tok_1",
            node_id="node_1",
            member_token=make_context(run_id="run_1").require_member_token(),
            step_index=1,
            input_data={"v": 1},
            auto_fail_phase="transform_execution",
        )
        with pytest.raises(ValueError, match="test crash"), guard:
            raise ValueError("test crash")

        # Auto-completed as FAILED
        factory.execution.complete_node_state.assert_called_once()
        kwargs = factory.execution.complete_node_state.call_args[1]
        assert kwargs["status"] == NodeStateStatus.FAILED
        assert kwargs["state_id"] == "state_001"
        assert "test crash" in kwargs["error"].exception
        assert kwargs["error"].exception_type == "ValueError"
        assert kwargs["error"].phase == "transform_execution"
        assert kwargs["duration_ms"] >= 0

    def test_explicit_ownership_abandonment_preserves_open_state(self) -> None:
        """Ownership loss leaves the stale attempt OPEN and propagates."""
        from elspeth.contracts.errors import SchedulerLeaseLostError
        from elspeth.engine.executors.state_guard import NodeStateGuard

        factory = _make_factory()
        guard = NodeStateGuard(
            factory.execution,
            token_id="tok_1",
            node_id="node_1",
            member_token=make_context(run_id="run_1").require_member_token(),
            step_index=1,
            input_data={"v": 1},
            auto_fail_phase="transform_execution",
        )
        with pytest.raises(SchedulerLeaseLostError), guard:
            guard.abandon_open_state()
            raise SchedulerLeaseLostError(work_item_id="work-old", lease_owner="worker-old", run_id="run_1")

        factory.execution.begin_node_state.assert_called_once()
        factory.execution.complete_node_state.assert_not_called()

    def test_abandonment_rejects_non_ownership_exception(self) -> None:
        """Only scheduler ownership loss may preserve an OPEN attempt."""
        from elspeth.engine.executors.state_guard import NodeStateGuard

        factory = _make_factory()
        guard = NodeStateGuard(
            factory.execution,
            token_id="tok_1",
            node_id="node_1",
            member_token=make_context(run_id="run_1").require_member_token(),
            step_index=1,
            input_data={"v": 1},
            auto_fail_phase="transform_execution",
        )
        with pytest.raises(OrchestrationInvariantError, match="nominal ownership-loss exception"), guard:
            guard.abandon_open_state()
            raise ValueError("not an ownership verdict")

        factory.execution.begin_node_state.assert_called_once()
        factory.execution.complete_node_state.assert_not_called()

    def test_empty_exception_message_still_records_failed(self) -> None:
        """A bare `raise ValueError()` must not abort terminal persistence.

        Regression (elspeth-d67453c22f): ExecutionError rejects empty
        exception strings, and the auto-fail path constructed it from raw
        str(exc_val) BEFORE the complete_node_state write — a messageless
        exception left the node state permanently OPEN.
        """
        from elspeth.engine.executors.state_guard import NodeStateGuard

        factory = _make_factory()
        guard = NodeStateGuard(
            factory.execution,
            token_id="tok_1",
            node_id="node_1",
            member_token=make_context(run_id="run_1").require_member_token(),
            step_index=1,
            input_data={"v": 1},
            auto_fail_phase="transform_execution",
        )
        with pytest.raises(ValueError), guard:
            raise ValueError()

        factory.execution.complete_node_state.assert_called_once()
        kwargs = factory.execution.complete_node_state.call_args[1]
        assert kwargs["status"] == NodeStateStatus.FAILED
        assert kwargs["error"].exception == "ValueError"
        assert kwargs["error"].exception_type == "ValueError"

    def test_whitespace_only_exception_message_still_records_failed(self) -> None:
        """Whitespace-only messages fall back to the exception type name."""
        from elspeth.engine.executors.state_guard import NodeStateGuard

        factory = _make_factory()
        guard = NodeStateGuard(
            factory.execution,
            token_id="tok_1",
            node_id="node_1",
            member_token=make_context(run_id="run_1").require_member_token(),
            step_index=1,
            input_data={"v": 1},
            auto_fail_phase="transform_execution",
        )
        with pytest.raises(RuntimeError), guard:
            raise RuntimeError("   ")

        factory.execution.complete_node_state.assert_called_once()
        kwargs = factory.execution.complete_node_state.call_args[1]
        assert kwargs["status"] == NodeStateStatus.FAILED
        assert kwargs["error"].exception == "RuntimeError"

    def test_raising_dunder_str_still_records_failed(self) -> None:
        """An exception whose __str__ raises must not break audit terminality.

        The original exception re-raises; the audit record falls back to the
        exception type name for its message.
        """
        from elspeth.engine.executors.state_guard import NodeStateGuard

        class _HostileStr(RuntimeError):
            def __str__(self) -> str:
                raise RuntimeError("__str__ exploded")

        factory = _make_factory()
        guard = NodeStateGuard(
            factory.execution,
            token_id="tok_1",
            node_id="node_1",
            member_token=make_context(run_id="run_1").require_member_token(),
            step_index=1,
            input_data={"v": 1},
            auto_fail_phase="transform_execution",
        )
        with pytest.raises(_HostileStr), guard:
            raise _HostileStr("unrenderable")

        factory.execution.complete_node_state.assert_called_once()
        kwargs = factory.execution.complete_node_state.call_args[1]
        assert kwargs["status"] == NodeStateStatus.FAILED
        assert kwargs["error"].exception == "_HostileStr"
        assert kwargs["error"].exception_type == "_HostileStr"

    def test_incomplete_audit_evidence_instantiation_inside_guard_records_failed(self) -> None:
        """Construction-time abstract failures still preserve the guard's terminal-state invariant."""
        from elspeth.contracts.audit_evidence import AuditEvidenceBase
        from elspeth.engine.executors.state_guard import NodeStateGuard

        class _Incomplete(AuditEvidenceBase, RuntimeError):
            def __init__(self, message: str) -> None:
                RuntimeError.__init__(self, message)

        factory = _make_factory()
        guard = NodeStateGuard(
            factory.execution,
            token_id="tok_1",
            node_id="node_1",
            member_token=make_context(run_id="run_1").require_member_token(),
            step_index=1,
            input_data={"v": 1},
            auto_fail_phase="transform_execution",
        )

        with pytest.raises(TypeError, match="abstract"), guard:
            raise _Incomplete("test crash")

        factory.execution.complete_node_state.assert_called_once()
        kwargs = factory.execution.complete_node_state.call_args[1]
        assert kwargs["status"] == NodeStateStatus.FAILED
        assert kwargs["state_id"] == "state_001"
        assert kwargs["error"].exception_type == "TypeError"
        assert "abstract" in kwargs["error"].exception
        assert kwargs["error"].phase == "transform_execution"

    def test_explicit_complete_prevents_auto_fail(self) -> None:
        """If caller calls complete() before exception, guard is no-op."""
        from elspeth.engine.executors.state_guard import NodeStateGuard

        factory = _make_factory()
        guard = NodeStateGuard(
            factory.execution,
            token_id="tok_1",
            node_id="node_1",
            member_token=make_context(run_id="run_1").require_member_token(),
            step_index=1,
            input_data={"v": 1},
            auto_fail_phase="transform_execution",
        )
        with pytest.raises(RuntimeError, match="post-complete crash"), guard:
            guard.complete(NodeStateStatus.COMPLETED, duration_ms=10.0)
            raise RuntimeError("post-complete crash")

        # Only one complete_node_state call (the explicit one)
        factory.execution.complete_node_state.assert_called_once()
        kwargs = factory.execution.complete_node_state.call_args[1]
        assert kwargs["status"] == NodeStateStatus.COMPLETED

    def test_complete_rejects_pending_without_standing_guard_down(self) -> None:
        """PENDING is not terminal and cannot satisfy the guard invariant."""
        from elspeth.engine.executors.state_guard import NodeStateGuard

        factory = _make_factory()
        guard = NodeStateGuard(
            factory.execution,
            token_id="tok_1",
            node_id="node_1",
            member_token=make_context(run_id="run_1").require_member_token(),
            step_index=1,
            input_data={"v": 1},
            auto_fail_phase="transform_execution",
        )

        with pytest.raises(OrchestrationInvariantError, match="terminal"), guard:
            guard.complete(NodeStateStatus.PENDING, duration_ms=10.0)

        factory.execution.complete_node_state.assert_called_once()
        kwargs = factory.execution.complete_node_state.call_args[1]
        assert kwargs["status"] == NodeStateStatus.FAILED
        assert kwargs["error"].exception_type == "OrchestrationInvariantError"
        assert "PENDING" in kwargs["error"].exception
        assert guard.completed is False

    def test_state_id_accessible_inside_block(self) -> None:
        """guard.state_id is available after __enter__."""
        from elspeth.engine.executors.state_guard import NodeStateGuard

        factory = _make_factory()
        guard = NodeStateGuard(
            factory.execution,
            token_id="tok_1",
            node_id="node_1",
            member_token=make_context(run_id="run_1").require_member_token(),
            step_index=1,
            input_data={"v": 1},
            auto_fail_phase="transform_execution",
        )
        with guard:
            assert guard.state_id == "state_001"
            assert guard.state.state_id == "state_001"
            assert guard.completed is False
            guard.complete(NodeStateStatus.COMPLETED, output_data={"v": 1}, duration_ms=0.0)

    def test_state_id_before_enter_raises(self) -> None:
        """Accessing state_id before __enter__ raises OrchestrationInvariantError."""
        from elspeth.engine.executors.state_guard import NodeStateGuard

        guard = NodeStateGuard(
            _make_factory().execution,
            token_id="tok_1",
            node_id="node_1",
            member_token=make_context(run_id="run_1").require_member_token(),
            step_index=1,
            input_data={"v": 1},
            auto_fail_phase="transform_execution",
        )
        with pytest.raises(OrchestrationInvariantError, match="before __enter__"):
            _ = guard.state_id

    def test_auto_fail_when_execution_repo_record_failure_raises_audit_integrity_error(self) -> None:
        """Landscape recorder failures during auto-fail must surface as AuditIntegrityError.

        Regression: Previously the original exception propagated and the DB failure
        was silently logged. This violates crash-on-anomaly: the audit trail has a
        permanent OPEN state (Tier 1 violation) which is more critical than the
        original transform error.
        """
        from elspeth.contracts.errors import AuditIntegrityError
        from elspeth.core.landscape.errors import LandscapeRecordError
        from elspeth.engine.executors.state_guard import NodeStateGuard

        factory = _make_factory()
        factory.execution.complete_node_state.side_effect = LandscapeRecordError("DB is down")
        guard = NodeStateGuard(
            factory.execution,
            token_id="tok_1",
            node_id="node_1",
            member_token=make_context(run_id="run_1").require_member_token(),
            step_index=1,
            input_data={"v": 1},
            auto_fail_phase="transform_execution",
        )
        # AuditIntegrityError must be raised — DB failure is more critical than original error
        with pytest.raises(AuditIntegrityError, match=r"Cannot record FAILED.*DB is down"), guard:
            raise ValueError("original error")

    def test_auto_fail_value_error_from_execution_repo_propagates_plainly(self) -> None:
        """Non-recorder bugs during auto-fail must keep their original type."""
        from elspeth.engine.executors.state_guard import NodeStateGuard

        factory = _make_factory()
        factory.execution.complete_node_state.side_effect = ValueError("execution repo bug")
        guard = NodeStateGuard(
            factory.execution,
            token_id="tok_1",
            node_id="node_1",
            member_token=make_context(run_id="run_1").require_member_token(),
            step_index=1,
            input_data={"v": 1},
            auto_fail_phase="transform_execution",
        )
        with pytest.raises(ValueError, match="execution repo bug"), guard:
            raise RuntimeError("original processing error")

    def test_attempt_passed_to_begin_node_state(self) -> None:
        """attempt parameter is forwarded to begin_node_state."""
        from elspeth.engine.executors.state_guard import NodeStateGuard

        factory = _make_factory()
        with NodeStateGuard(
            factory.execution,
            token_id="tok_1",
            node_id="node_1",
            member_token=make_context(run_id="run_1").require_member_token(),
            step_index=2,
            input_data={"v": 1},
            auto_fail_phase="transform_execution",
            attempt=3,
        ) as guard:
            guard.complete(NodeStateStatus.COMPLETED, duration_ms=1.0)

        kwargs = factory.execution.begin_node_state.call_args[1]
        assert kwargs["attempt"] == 3
        assert kwargs["step_index"] == 2

    # -- Re-raise guard tests: clean-exit path (missing complete()) ----------

    def test_framework_bug_error_propagates_on_clean_exit(self) -> None:
        """FrameworkBugError from execution repo supersedes OrchestrationInvariantError.

        When the block exits normally without complete(), the guard tries to
        record FAILED.  If that execution repo call raises FrameworkBugError, it must
        propagate directly — it's more critical than the "missing complete()" bug.
        """
        from elspeth.contracts.errors import FrameworkBugError
        from elspeth.engine.executors.state_guard import NodeStateGuard

        factory = _make_factory()
        factory.execution.complete_node_state.side_effect = FrameworkBugError("internal inconsistency")
        guard = NodeStateGuard(
            factory.execution,
            token_id="tok_1",
            node_id="node_1",
            member_token=make_context(run_id="run_1").require_member_token(),
            step_index=1,
            input_data={"v": 1},
            auto_fail_phase="transform_execution",
        )
        # Must propagate FrameworkBugError, NOT OrchestrationInvariantError
        with pytest.raises(FrameworkBugError, match="internal inconsistency"), guard:
            pass  # Exit without complete()

    def test_audit_integrity_error_propagates_on_clean_exit(self) -> None:
        """AuditIntegrityError from execution repo supersedes OrchestrationInvariantError.

        Same as above but for AuditIntegrityError — audit corruption is always
        the highest-priority failure signal.
        """
        from elspeth.contracts.errors import AuditIntegrityError
        from elspeth.engine.executors.state_guard import NodeStateGuard

        factory = _make_factory()
        factory.execution.complete_node_state.side_effect = AuditIntegrityError("corrupt state table")
        guard = NodeStateGuard(
            factory.execution,
            token_id="tok_1",
            node_id="node_1",
            member_token=make_context(run_id="run_1").require_member_token(),
            step_index=1,
            input_data={"v": 1},
            auto_fail_phase="transform_execution",
        )
        with pytest.raises(AuditIntegrityError, match="corrupt state table"), guard:
            pass

    def test_landscape_record_error_on_clean_exit_raises_audit_integrity(self) -> None:
        """Landscape recorder failures on clean exit must raise AuditIntegrityError.

        When factory.execution.complete_node_state fails with a recorder failure (e.g., DB
        down) during the clean-exit path, AuditIntegrityError supersedes the
        OrchestrationInvariantError that would have been raised for missing complete().
        Audit corruption is always the highest-priority failure signal.
        """
        from elspeth.contracts.errors import AuditIntegrityError
        from elspeth.core.landscape.errors import LandscapeRecordError
        from elspeth.engine.executors.state_guard import NodeStateGuard

        factory = _make_factory()
        factory.execution.complete_node_state.side_effect = LandscapeRecordError("DB connection lost")
        guard = NodeStateGuard(
            factory.execution,
            token_id="tok_1",
            node_id="node_1",
            member_token=make_context(run_id="run_1").require_member_token(),
            step_index=1,
            input_data={"v": 1},
            auto_fail_phase="transform_execution",
        )
        with pytest.raises(AuditIntegrityError, match=r"Cannot record FAILED.*DB connection lost"), guard:
            pass

    def test_value_error_from_execution_repo_propagates_on_clean_exit(self) -> None:
        """Non-recorder bugs on clean exit must not be reclassified."""
        from elspeth.engine.executors.state_guard import NodeStateGuard

        factory = _make_factory()
        factory.execution.complete_node_state.side_effect = ValueError("execution repo bug")
        guard = NodeStateGuard(
            factory.execution,
            token_id="tok_1",
            node_id="node_1",
            member_token=make_context(run_id="run_1").require_member_token(),
            step_index=1,
            input_data={"v": 1},
            auto_fail_phase="transform_execution",
        )
        with pytest.raises(ValueError, match="execution repo bug"), guard:
            pass

    # -- Re-raise guard tests: exception path (auto-fail) --------------------

    def test_framework_bug_error_supersedes_original_exception(self) -> None:
        """FrameworkBugError from execution repo supersedes the original exception.

        When an exception triggers __exit__ and the auto-fail execution repo call
        raises FrameworkBugError, it must propagate instead of the original
        exception — system-level corruption outranks the triggering error.
        """
        from elspeth.contracts.errors import FrameworkBugError
        from elspeth.engine.executors.state_guard import NodeStateGuard

        factory = _make_factory()
        factory.execution.complete_node_state.side_effect = FrameworkBugError("broken invariant")
        guard = NodeStateGuard(
            factory.execution,
            token_id="tok_1",
            node_id="node_1",
            member_token=make_context(run_id="run_1").require_member_token(),
            step_index=1,
            input_data={"v": 1},
            auto_fail_phase="transform_execution",
        )
        # FrameworkBugError supersedes the original ValueError
        with pytest.raises(FrameworkBugError, match="broken invariant"), guard:
            raise ValueError("original processing error")

    def test_audit_integrity_error_supersedes_original_exception(self) -> None:
        """AuditIntegrityError from execution repo supersedes the original exception.

        Same as above: audit corruption is always the highest-priority signal.
        """
        from elspeth.contracts.errors import AuditIntegrityError
        from elspeth.engine.executors.state_guard import NodeStateGuard

        factory = _make_factory()
        factory.execution.complete_node_state.side_effect = AuditIntegrityError("state table corrupt")
        guard = NodeStateGuard(
            factory.execution,
            token_id="tok_1",
            node_id="node_1",
            member_token=make_context(run_id="run_1").require_member_token(),
            step_index=1,
            input_data={"v": 1},
            auto_fail_phase="transform_execution",
        )
        with pytest.raises(AuditIntegrityError, match="state table corrupt"), guard:
            raise ValueError("original error, now masked by corruption")

    def test_post_commit_failure_prevents_double_write(self) -> None:
        """Regression: complete() raising after commit must not trigger __exit__ overwrite.

        If complete_node_state() commits COMPLETED to the DB but then raises
        (e.g., post-commit validation in the repository), _completed stays False.
        Without a durable-write marker, __exit__ would overwrite the persisted
        COMPLETED with FAILED — corrupting the audit trail.
        """
        from elspeth.core.landscape.errors import LandscapePostCommitError
        from elspeth.engine.executors.state_guard import NodeStateGuard

        factory = _make_factory()
        call_count = [0]
        original_complete = factory.execution.complete_node_state

        def _commit_then_raise(**kwargs: object) -> None:
            """Simulate an execution repo that commits, then raises post-commit."""
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: the explicit complete() — simulate post-commit failure
                raise LandscapePostCommitError("post-commit validation failed")
            # Second call would be __exit__ overwriting — should NOT happen
            original_complete(**kwargs)

        factory.execution.complete_node_state.side_effect = _commit_then_raise
        guard = NodeStateGuard(
            factory.execution,
            token_id="tok_1",
            node_id="node_1",
            member_token=make_context(run_id="run_1").require_member_token(),
            step_index=1,
            input_data={"v": 1},
            auto_fail_phase="transform_execution",
        )
        with pytest.raises(LandscapePostCommitError, match="post-commit validation"), guard:
            guard.complete(NodeStateStatus.COMPLETED, output_data={"v": 1}, duration_ms=0.0)

        # Only ONE call to complete_node_state — the explicit one.
        # __exit__ must NOT attempt a second call because the terminal write is known.
        assert call_count[0] == 1
        assert guard._terminal_persisted is True
        assert guard._completed is False  # Never reached — the call raised

    def test_complete_pre_persistence_failure_records_failed(self) -> None:
        """If complete() fails before persistence, __exit__ must still record FAILED."""
        import rfc8785

        from elspeth.engine.executors.state_guard import NodeStateGuard

        setup = make_recorder_with_run(source_node_id="source-0")
        register_test_node(setup.data_flow, setup.run_id, "transform-1")
        setup.data_flow.create_row_with_token(
            setup.source_node_id,
            0,
            {"name": "test"},
            row_id="row-1",
            token_id="tok-1",
            source_row_index=0,
            ingest_sequence=0,
            coordination_token=setup.coordination_token,
        )

        guard = NodeStateGuard(
            setup.execution,
            token_id="tok-1",
            node_id="transform-1",
            member_token=setup.coordination_token.membership,
            step_index=1,
            input_data={"name": "test"},
            auto_fail_phase="transform_execution",
        )

        with pytest.raises(rfc8785.CanonicalizationError, match="unsupported type"), guard:
            guard.complete(
                NodeStateStatus.COMPLETED,
                output_data={"name": "test"},
                duration_ms=1.0,
                success_reason={"action": "processed", "metadata": {"bad": object()}},
            )

        state = setup.execution.get_node_state(guard.state_id)
        assert state is not None
        assert state.status == NodeStateStatus.FAILED
        assert state.error_json is not None
        assert "CanonicalizationError" in state.error_json

    def test_non_serializable_audit_evidence_records_failed_then_raises_audit_integrity(self) -> None:
        """Broken audit evidence must not strand the state OPEN during auto-fail."""

        from elspeth.contracts.audit_evidence import AuditEvidenceBase
        from elspeth.contracts.errors import AuditIntegrityError
        from elspeth.engine.executors.state_guard import NodeStateGuard

        class _BrokenEvidence(AuditEvidenceBase, RuntimeError):
            def to_audit_dict(self) -> Mapping[str, Any]:
                return {"bad": object()}

        setup = make_recorder_with_run(source_node_id="source-0")
        register_test_node(setup.data_flow, setup.run_id, "transform-1")
        setup.data_flow.create_row_with_token(
            setup.source_node_id,
            0,
            {"name": "test"},
            row_id="row-1",
            token_id="tok-1",
            source_row_index=0,
            ingest_sequence=0,
            coordination_token=setup.coordination_token,
        )

        guard = NodeStateGuard(
            setup.execution,
            token_id="tok-1",
            node_id="transform-1",
            member_token=setup.coordination_token.membership,
            step_index=1,
            input_data={"name": "test"},
            auto_fail_phase="transform_execution",
        )

        with pytest.raises(AuditIntegrityError, match="audit evidence serialization failed"), guard:
            raise _BrokenEvidence("boom")

        state = setup.execution.get_node_state(guard.state_id)
        assert state is not None
        assert state.status == NodeStateStatus.FAILED
        assert state.error_json is not None
        assert "boom" in state.error_json

    def test_plugin_contract_violation_populates_execution_error_context(self) -> None:
        """ADR-008: PluginContractViolation.to_audit_dict() → ExecutionError.context."""
        from elspeth.engine.executors.state_guard import NodeStateGuard

        factory = _make_factory()
        guard = NodeStateGuard(
            factory.execution,
            token_id="tok_1",
            node_id="node_1",
            member_token=make_context(run_id="run_1").require_member_token(),
            step_index=1,
            input_data={"v": 1},
            auto_fail_phase="transform_execution",
        )
        violation = PassThroughContractViolation(
            transform="t",
            transform_node_id="n",
            run_id="r",
            row_id="row",
            token_id="tok",
            static_contract=frozenset({"a"}),
            runtime_observed=frozenset(),
            divergence_set=frozenset({"a"}),
            message="dropped field 'a'",
        )
        with pytest.raises(PassThroughContractViolation), guard:
            raise violation

        # The recorded ExecutionError must carry the structured context.
        # ExecutionError.__post_init__ deep-freezes context, so list values
        # round-trip to tuples (Tier-1 immutability guarantee).
        factory.execution.complete_node_state.assert_called_once()
        kwargs = factory.execution.complete_node_state.call_args[1]
        error = kwargs["error"]
        assert error.context is not None
        assert error.context["exception_type"] == "PassThroughContractViolation"
        assert tuple(error.context["divergence_set"]) == ("a",)
        assert tuple(error.context["static_contract"]) == ("a",)
        assert tuple(error.context["runtime_observed"]) == ()
        # And to_dict() emits it under 'context'.
        d = error.to_dict()
        assert tuple(d["context"]["divergence_set"]) == ("a",)
        assert d["type"] == "PassThroughContractViolation"  # Legacy key preserved.

    def test_non_plugin_contract_violation_leaves_context_none(self) -> None:
        """Regular exceptions do NOT populate ExecutionError.context."""
        from elspeth.engine.executors.state_guard import NodeStateGuard

        factory = _make_factory()
        guard = NodeStateGuard(
            factory.execution,
            token_id="tok_1",
            node_id="node_1",
            member_token=make_context(run_id="run_1").require_member_token(),
            step_index=1,
            input_data={"v": 1},
            auto_fail_phase="transform_execution",
        )
        with pytest.raises(ValueError), guard:
            raise ValueError("just a ValueError")

        factory.execution.complete_node_state.assert_called_once()
        kwargs = factory.execution.complete_node_state.call_args[1]
        error = kwargs["error"]
        # context remains None for non-PluginContractViolation exceptions.
        assert error.context is None
        d = error.to_dict()
        assert "context" not in d  # Omitted entirely when None.
