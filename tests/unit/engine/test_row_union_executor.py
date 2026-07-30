# tests/unit/engine/test_row_union_executor.py
"""Unit tests for RowUnionExecutor (elspeth-a5b86149d4 v1 contract).

The row_union barrier holds fork-branch tokens per (row_union_name, row_id)
and releases the ORIGINAL tokens as one indivisible group in declared branch
order once every declared branch has arrived. v1 is require_all with no
partial release: timeouts, lost branches, and end-of-source flushes fail the
whole pending group closed.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from elspeth.contracts import TokenInfo
from elspeth.contracts.enums import NodeStateStatus, TerminalOutcome
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.contracts.types import NodeID
from elspeth.core.config import RowUnionSettings
from elspeth.core.landscape.data_flow_repository import DataFlowRepository
from elspeth.core.landscape.execution_repository import ExecutionRepository
from elspeth.engine.clock import MockClock
from elspeth.engine.row_union_executor import RowUnionExecutor, RowUnionOutcome, RowUnionRestoreEntry
from elspeth.testing import make_field, make_row

_state_counter = itertools.count(1)


def _branch_loss_lookup(*, run_id: str, barrier_name: str, row_id: str) -> bool:
    """Callable spec for the durable branch-loss point-read port."""
    return False


def _next_state_id() -> str:
    return f"state_{next(_state_counter):04d}"


def _make_contract() -> SchemaContract:
    fields = [
        make_field("amount", original_name="amount", python_type=int, required=True, source="declared"),
    ]
    return SchemaContract(fields=tuple(fields), mode="FLEXIBLE", locked=True)


def _make_token(
    row_id: str = "row_1",
    token_id: str = "tok_1",
    branch_name: str = "branch_a",
    data: dict[str, Any] | None = None,
) -> TokenInfo:
    row_data = make_row(data if data is not None else {"amount": 100}, contract=_make_contract())
    return TokenInfo(row_id=row_id, token_id=token_id, row_data=row_data, branch_name=branch_name)


def _make_settings(
    name: str = "variant_union",
    branches: list[str] | dict[str, str] | None = None,
    timeout_seconds: float | None = None,
) -> RowUnionSettings:
    return RowUnionSettings(
        name=name,
        branches=branches if branches is not None else ["branch_a", "branch_b"],
        on_success="union_out",
        timeout_seconds=timeout_seconds,
    )


def _make_executor(
    clock: MockClock | None = None,
    *,
    max_completed_keys: int = 10000,
) -> tuple[RowUnionExecutor, MagicMock, MagicMock, MockClock]:
    execution = MagicMock(spec=ExecutionRepository)
    execution.begin_node_state.side_effect = lambda **kw: SimpleNamespace(state_id=_next_state_id())
    execution.get_completed_row_ids_for_nodes.return_value = set()
    execution.has_completed_row_for_node.return_value = False
    execution.has_released_row_for_node.return_value = False
    data_flow = MagicMock(spec=DataFlowRepository)
    if clock is None:
        clock = MockClock(start=100.0)
    executor = RowUnionExecutor(
        execution,
        object(),
        "run_1",
        step_resolver=lambda node_id: 5,
        clock=clock,
        data_flow=data_flow,
        max_completed_keys=max_completed_keys,
        barrier_restore_reads=SimpleNamespace(
            get_completed_row_ids_for_nodes=execution.get_completed_row_ids_for_nodes,
            has_completed_row_for_node=execution.has_completed_row_for_node,
            has_released_row_for_node=execution.has_released_row_for_node,
            has_branch_loss_for_group=MagicMock(
                spec=_branch_loss_lookup,
                return_value=False,
            ),
        ),
    )
    return executor, execution, data_flow, clock


def _register(executor: RowUnionExecutor, settings: RowUnionSettings | None = None) -> RowUnionSettings:
    settings = settings or _make_settings()
    executor.register_row_union(settings, NodeID("node_union"))
    return settings


class TestAcceptHoldAndRelease:
    def test_first_arrival_is_held(self) -> None:
        executor, execution, data_flow, _clock = _make_executor()
        _register(executor)
        outcome = executor.accept(_make_token(branch_name="branch_a"), "variant_union")
        assert outcome.held is True
        assert outcome.released_tokens == ()
        execution.begin_node_state.assert_called_once()
        data_flow.record_token_outcome.assert_not_called()

    def test_full_group_releases_original_tokens_in_declared_order(self) -> None:
        executor, _execution, _data_flow, _clock = _make_executor()
        _register(executor)
        tok_b = _make_token(token_id="tok_b", branch_name="branch_b")
        tok_a = _make_token(token_id="tok_a", branch_name="branch_a")
        assert executor.accept(tok_b, "variant_union").held is True
        outcome = executor.accept(tok_a, "variant_union")
        assert outcome.held is False
        assert outcome.failure_reason is None
        # Declared order [branch_a, branch_b], not arrival order [b, a] —
        # and the SAME TokenInfo objects, identity preserved, no merge.
        assert outcome.released_tokens == (tok_a, tok_b)
        assert outcome.released_tokens[0] is tok_a
        assert outcome.released_tokens[1] is tok_b
        assert outcome.row_union_name == "variant_union"

    def test_release_completes_node_states_without_terminal_outcomes(self) -> None:
        executor, execution, data_flow, _clock = _make_executor()
        _register(executor)
        executor.accept(_make_token(token_id="tok_a", branch_name="branch_a"), "variant_union")
        executor.accept(_make_token(token_id="tok_b", branch_name="branch_b"), "variant_union")
        statuses = [call.kwargs["status"] for call in execution.complete_node_state.call_args_list]
        assert statuses == [NodeStateStatus.COMPLETED, NodeStateStatus.COMPLETED]
        # Released tokens are NOT terminal at the barrier: they continue
        # downstream, so no token outcome is recorded here.
        data_flow.record_token_outcome.assert_not_called()

    def test_duplicate_arrival_crashes(self) -> None:
        executor, _execution, _data_flow, _clock = _make_executor()
        _register(executor)
        executor.accept(_make_token(token_id="tok_1", branch_name="branch_a"), "variant_union")
        with pytest.raises(OrchestrationInvariantError, match="Duplicate arrival"):
            executor.accept(_make_token(token_id="tok_2", branch_name="branch_a"), "variant_union")

    def test_unregistered_name_crashes(self) -> None:
        executor, _execution, _data_flow, _clock = _make_executor()
        with pytest.raises(OrchestrationInvariantError, match="not registered"):
            executor.accept(_make_token(), "ghost_union")

    def test_token_without_branch_crashes(self) -> None:
        executor, _execution, _data_flow, _clock = _make_executor()
        _register(executor)
        token = TokenInfo(row_id="row_1", token_id="tok_1", row_data=make_row({"amount": 1}, contract=_make_contract()))
        with pytest.raises(OrchestrationInvariantError, match="branch_name"):
            executor.accept(token, "variant_union")

    def test_unexpected_branch_crashes(self) -> None:
        executor, _execution, _data_flow, _clock = _make_executor()
        _register(executor)
        with pytest.raises(OrchestrationInvariantError, match="not in expected branches"):
            executor.accept(_make_token(branch_name="ghost_branch"), "variant_union")


class TestLateArrival:
    def test_late_arrival_after_release_fails_closed(self) -> None:
        executor, _execution, data_flow, _clock = _make_executor()
        _register(executor)
        executor.accept(_make_token(token_id="tok_a", branch_name="branch_a"), "variant_union")
        executor.accept(_make_token(token_id="tok_b", branch_name="branch_b"), "variant_union")
        late = executor.accept(_make_token(token_id="tok_late", branch_name="branch_a"), "variant_union")
        assert late.held is False
        assert late.late_arrival is True
        assert late.outcomes_recorded is True
        assert late.failure_reason == "late_arrival_after_release"
        assert data_flow.record_token_outcome.call_args.kwargs["outcome"] is TerminalOutcome.FAILURE

    def test_straggler_after_timeout_closure_carries_timeout_reason(self) -> None:
        clock = MockClock(start=100.0)
        executor, _execution, _data_flow, _ = _make_executor(clock=clock)
        _register(executor, _make_settings(timeout_seconds=5.0))
        executor.accept(_make_token(token_id="tok_a", branch_name="branch_a"), "variant_union")
        clock.advance(10.0)
        assert executor.check_timeouts("variant_union")[0].failure_reason == "row_union_timeout"

        late = executor.accept(_make_token(token_id="tok_late", branch_name="branch_b"), "variant_union")

        assert late.late_arrival is True
        # The group never released — a straggler's audit record must carry
        # the group's true closure reason, not "late_arrival_after_release".
        assert late.failure_reason == "row_union_timeout"

    def test_straggler_after_flush_closure_carries_flush_reason(self) -> None:
        executor, _execution, _data_flow, _clock = _make_executor()
        _register(executor)
        executor.accept(_make_token(token_id="tok_a", branch_name="branch_a"), "variant_union")
        assert executor.flush_pending()[0].failure_reason == "row_union_incomplete_at_flush"

        late = executor.accept(_make_token(token_id="tok_late", branch_name="branch_b"), "variant_union")

        assert late.late_arrival is True
        assert late.failure_reason == "row_union_incomplete_at_flush"

    def test_landscape_fallback_distinguishes_released_from_failed_closure(self) -> None:
        executor, execution, _data_flow, _clock = _make_executor()
        _register(executor)
        execution.has_completed_row_for_node.return_value = True
        execution.has_released_row_for_node.return_value = False

        late = executor.accept(_make_token(token_id="tok_late", branch_name="branch_a"), "variant_union")

        assert late.late_arrival is True
        # A FAILED closure found through the Landscape point read must not be
        # reported as a release.
        assert late.failure_reason == "row_union_group_failed"
        assert executor.is_group_released("variant_union", "row_1") is False

    def test_landscape_failed_closure_preserves_durable_branch_loss_reason(self) -> None:
        executor, execution, _data_flow, _clock = _make_executor()
        _register(executor)
        execution.has_completed_row_for_node.return_value = True
        execution.has_released_row_for_node.return_value = False
        executor._barrier_restore_reads.has_branch_loss_for_group.return_value = True

        late = executor.accept(_make_token(token_id="tok_late", branch_name="branch_a"), "variant_union")

        assert late.late_arrival is True
        assert late.failure_reason == "row_union_branch_lost"
        executor._barrier_restore_reads.has_branch_loss_for_group.assert_called_once_with(
            run_id="run_1",
            barrier_name="variant_union",
            row_id="row_1",
        )

    def test_landscape_fallback_released_closure_is_late_arrival_after_release(self) -> None:
        executor, execution, _data_flow, _clock = _make_executor()
        _register(executor)
        execution.has_completed_row_for_node.return_value = True
        execution.has_released_row_for_node.return_value = True

        late = executor.accept(_make_token(token_id="tok_late", branch_name="branch_a"), "variant_union")

        assert late.late_arrival is True
        assert late.failure_reason == "late_arrival_after_release"
        assert executor.is_group_released("variant_union", "row_1") is True


class TestTimeouts:
    def test_reports_whether_any_registered_barrier_has_a_timeout(self) -> None:
        # Source idle polling reads this to decide whether the run needs a
        # wall-clock sweep at all; calling it IS the surface assertion.
        without_timeout, _execution, _data_flow, _clock = _make_executor()
        _register(without_timeout)
        assert without_timeout.has_timeout_configured() is False

        with_timeout, _execution, _data_flow, _clock = _make_executor()
        _register(with_timeout, _make_settings(timeout_seconds=5.0))
        assert with_timeout.has_timeout_configured() is True

    def test_timeout_fails_whole_group(self) -> None:
        clock = MockClock(start=100.0)
        executor, execution, data_flow, _ = _make_executor(clock=clock)
        _register(executor, _make_settings(timeout_seconds=5.0))
        tok_a = _make_token(token_id="tok_a", branch_name="branch_a")
        executor.accept(tok_a, "variant_union")
        clock.advance(10.0)
        outcomes = executor.check_timeouts("variant_union")
        assert len(outcomes) == 1
        assert outcomes[0].failure_reason == "row_union_timeout"
        assert outcomes[0].consumed_tokens == (tok_a,)
        assert outcomes[0].outcomes_recorded is True
        assert execution.complete_node_state.call_args.kwargs["status"] is NodeStateStatus.FAILED
        assert data_flow.record_token_outcome.call_args.kwargs["outcome"] is TerminalOutcome.FAILURE

    def test_no_timeout_configured_never_times_out(self) -> None:
        clock = MockClock(start=100.0)
        executor, _execution, _data_flow, _ = _make_executor(clock=clock)
        _register(executor)
        executor.accept(_make_token(branch_name="branch_a"), "variant_union")
        clock.advance(10_000.0)
        assert executor.check_timeouts("variant_union") == []


class TestFlushPending:
    def test_flush_fails_incomplete_groups(self) -> None:
        executor, _execution, data_flow, _clock = _make_executor()
        _register(executor)
        tok_a = _make_token(token_id="tok_a", branch_name="branch_a")
        executor.accept(tok_a, "variant_union")
        outcomes = executor.flush_pending()
        assert len(outcomes) == 1
        assert outcomes[0].failure_reason == "row_union_incomplete_at_flush"
        assert outcomes[0].consumed_tokens == (tok_a,)
        data_flow.record_token_outcome.assert_called_once()

    def test_flush_with_no_pending_is_empty(self) -> None:
        executor, _execution, _data_flow, _clock = _make_executor()
        _register(executor)
        assert executor.flush_pending() == []


class TestBranchLoss:
    def test_lost_branch_fails_arrived_siblings(self) -> None:
        executor, _execution, data_flow, _clock = _make_executor()
        _register(executor)
        tok_a = _make_token(token_id="tok_a", branch_name="branch_a")
        executor.accept(tok_a, "variant_union")
        outcome = executor.notify_branch_lost(
            row_union_name="variant_union",
            row_id="row_1",
            lost_branch="branch_b",
            reason="diverted_to_error_sink",
        )
        assert outcome is not None
        assert outcome.failure_reason == "row_union_branch_lost"
        assert outcome.consumed_tokens == (tok_a,)
        data_flow.record_token_outcome.assert_called_once()
        assert executor.has_recorded_branch_loss("variant_union", "row_1", "branch_b") is True

    def test_lost_branch_with_no_pending_marks_group_dead(self) -> None:
        executor, _execution, _data_flow, _clock = _make_executor()
        _register(executor)
        outcome = executor.notify_branch_lost(
            row_union_name="variant_union",
            row_id="row_1",
            lost_branch="branch_b",
            reason="diverted_to_error_sink",
        )
        assert outcome is None
        # A sibling arriving afterwards must fail closed, never wait forever.
        late = executor.accept(_make_token(token_id="tok_a", branch_name="branch_a"), "variant_union")
        assert late.held is False
        assert late.failure_reason is not None

    def test_lost_branch_after_release_records_nothing(self) -> None:
        # Released tokens keep branch_name, so a terminal divert downstream
        # of the union re-enters the loss path; a released group is not a
        # pre-barrier loss and must not pollute the loss indexes.
        executor, _execution, data_flow, _clock = _make_executor()
        _register(executor)
        executor.accept(_make_token(token_id="tok_a", branch_name="branch_a"), "variant_union")
        executor.accept(_make_token(token_id="tok_b", branch_name="branch_b"), "variant_union")
        assert executor.is_group_released("variant_union", "row_1") is True

        outcome = executor.notify_branch_lost(
            row_union_name="variant_union",
            row_id="row_1",
            lost_branch="branch_a",
            reason="routed_to_sink",
        )

        assert outcome is None
        assert executor.has_recorded_branch_loss("variant_union", "row_1", "branch_a") is False
        data_flow.record_token_outcome.assert_not_called()
        # The released closure survives untouched: a genuine straggler for
        # this key is still reported against the release, not a branch loss.
        assert executor.is_group_released("variant_union", "row_1") is True

    def test_lost_branch_after_evicted_release_uses_durable_completion_before_recording(self) -> None:
        executor, execution, _data_flow, _clock = _make_executor(max_completed_keys=1)
        _register(executor)
        for row_id in ("row_1", "row_2"):
            executor.accept(_make_token(row_id=row_id, token_id=f"{row_id}-a", branch_name="branch_a"), "variant_union")
            executor.accept(_make_token(row_id=row_id, token_id=f"{row_id}-b", branch_name="branch_b"), "variant_union")
        execution.has_completed_row_for_node.reset_mock()
        execution.has_completed_row_for_node.return_value = True
        execution.has_released_row_for_node.return_value = True

        outcome = executor.notify_branch_lost(
            row_union_name="variant_union",
            row_id="row_1",
            lost_branch="branch_a",
            reason="routed_to_sink",
        )

        assert outcome is None
        assert executor.has_recorded_branch_loss("variant_union", "row_1", "branch_a") is False
        execution.has_completed_row_for_node.assert_called_once_with(
            run_id="run_1",
            node_id="node_union",
            row_id="row_1",
        )
        assert executor.is_group_released("variant_union", "row_1") is True

    def test_is_group_released_false_for_failure_closed_group(self) -> None:
        executor, _execution, _data_flow, _clock = _make_executor()
        _register(executor)
        executor.accept(_make_token(token_id="tok_a", branch_name="branch_a"), "variant_union")
        executor.notify_branch_lost(
            row_union_name="variant_union",
            row_id="row_1",
            lost_branch="branch_b",
            reason="diverted_to_error_sink",
        )
        assert executor.is_group_released("variant_union", "row_1") is False

    def test_recorded_loss_indexes_are_bounded_with_durable_fallback_after_eviction(self) -> None:
        executor, _execution, _data_flow, _clock = _make_executor(max_completed_keys=1)
        _register(executor)
        executor._barrier_restore_reads.has_branch_loss_for_group.side_effect = (  # white-box cache regression
            lambda *, run_id, barrier_name, row_id: run_id == "run_1" and barrier_name == "variant_union" and row_id == "row_1"
        )
        for row_id in ("row_1", "row_2"):
            executor.notify_branch_lost(
                row_union_name="variant_union",
                row_id=row_id,
                lost_branch="branch_b",
                reason="diverted_to_error_sink",
            )

        assert len(executor._recorded_losses) <= 1  # resident-memory bound is the contract
        assert len(executor._recorded_loss_groups) <= 1  # resident-memory bound is the contract
        late = executor.accept(
            _make_token(row_id="row_1", token_id="tok_a", branch_name="branch_a"),
            "variant_union",
        )

        assert late.held is False
        assert late.failure_reason == "row_union_branch_lost"
        executor._barrier_restore_reads.has_branch_loss_for_group.assert_called_once_with(
            run_id="run_1",
            barrier_name="variant_union",
            row_id="row_1",
        )

    def test_durable_release_precedes_a_recent_branch_loss_hint(self) -> None:
        executor, execution, _data_flow, _clock = _make_executor(max_completed_keys=1)
        _register(executor)
        executor.notify_branch_lost(
            row_union_name="variant_union",
            row_id="row_1",
            lost_branch="branch_b",
            reason="error_routed",
        )
        # Close a different group to evict row_1 from the completion cache
        # while leaving its recent branch-loss hint resident.
        executor.accept(_make_token(row_id="row_2", token_id="tok_2a", branch_name="branch_a"), "variant_union")
        executor.accept(_make_token(row_id="row_2", token_id="tok_2b", branch_name="branch_b"), "variant_union")
        execution.has_completed_row_for_node.return_value = True
        execution.has_released_row_for_node.return_value = True

        late = executor.accept(
            _make_token(row_id="row_1", token_id="tok_late", branch_name="branch_a"),
            "variant_union",
        )

        assert late.held is False
        assert late.failure_reason == "late_arrival_after_release"
        execution.has_released_row_for_node.assert_called_once()


class TestRestoreFromJournal:
    def test_durable_loss_point_read_fails_pending_sibling_instead_of_reopening_group(self) -> None:
        executor, _execution, data_flow, _clock = _make_executor()
        _register(executor)
        restored = _make_token(token_id="tok_a", branch_name="branch_a")
        executor._barrier_restore_reads.has_branch_loss_for_group.return_value = True

        outcomes = executor.restore_from_journal(entries=(RowUnionRestoreEntry(restored, "variant_union", "state-a", 90.0),))

        assert len(outcomes) == 1
        assert outcomes[0].failure_reason == "row_union_branch_lost"
        assert outcomes[0].consumed_tokens == (restored,)
        data_flow.record_token_outcome.assert_called_once()
        executor._barrier_restore_reads.has_branch_loss_for_group.assert_called_once_with(
            run_id="run_1",
            barrier_name="variant_union",
            row_id="row_1",
        )

    def test_all_durable_loss_reads_finish_before_any_recovery_state_mutation(self) -> None:
        executor, execution, data_flow, _clock = _make_executor()
        _register(executor)
        restored_a = _make_token(row_id="row_1", token_id="tok_a", branch_name="branch_a")
        restored_b = _make_token(row_id="row_2", token_id="tok_b", branch_name="branch_a")
        executor._barrier_restore_reads.has_branch_loss_for_group.side_effect = (
            True,
            RuntimeError("second durable loss read failed"),
        )

        with pytest.raises(RuntimeError, match="second durable loss read failed"):
            executor.restore_from_journal(
                entries=(
                    RowUnionRestoreEntry(restored_a, "variant_union", "state-a", 90.0),
                    RowUnionRestoreEntry(restored_b, "variant_union", "state-b", 91.0),
                )
            )

        execution.complete_node_state.assert_not_called()
        data_flow.record_token_outcome.assert_not_called()
        assert executor._pending == {}

    def test_reconcile_released_group_refuses_recorded_loss_key(self) -> None:
        # Pins the pristine-group guard the coordination seam depends on: a
        # replayed loss for the key makes the group non-pristine, so the
        # restore path must filter stale losses BEFORE restore_branch_losses
        # (barrier_coordination drops losses contradicted by durable release
        # evidence) rather than expect reconcile to tolerate them.
        executor, _execution, _data_flow, _clock = _make_executor()
        _register(executor)
        executor.restore_branch_losses((("variant_union", "row_1", "branch_b"),))

        with pytest.raises(OrchestrationInvariantError, match="non-pristine"):
            executor.reconcile_released_group(
                entries=(
                    RowUnionRestoreEntry(_make_token(token_id="tok_a", branch_name="branch_a"), "variant_union", None, 90.0),
                    RowUnionRestoreEntry(_make_token(token_id="tok_b", branch_name="branch_b"), "variant_union", None, 91.0),
                )
            )

    def test_reconcile_released_group_completes_only_still_open_states(self) -> None:
        executor, execution, _data_flow, _clock = _make_executor()
        _register(executor)
        tok_a = _make_token(token_id="tok_a", branch_name="branch_a")
        tok_b = _make_token(token_id="tok_b", branch_name="branch_b")

        outcome = executor.reconcile_released_group(
            entries=(
                RowUnionRestoreEntry(tok_b, "variant_union", "state-b", 91.0),
                RowUnionRestoreEntry(tok_a, "variant_union", None, 90.0),
            )
        )

        assert outcome.released_tokens == (tok_a, tok_b)
        execution.complete_node_state.assert_called_once()
        assert execution.complete_node_state.call_args.kwargs["state_id"] == "state-b"

    def test_partial_group_resumes_and_releases_when_sibling_arrives(self) -> None:
        executor, execution, _data_flow, _clock = _make_executor()
        _register(executor)
        restored = _make_token(token_id="tok_a", branch_name="branch_a")

        outcomes = executor.restore_from_journal(
            entries=(
                RowUnionRestoreEntry(
                    token=restored,
                    row_union_name="variant_union",
                    state_id="state-restored",
                    arrival_time=90.0,
                ),
            )
        )

        assert outcomes == ()
        sibling = _make_token(token_id="tok_b", branch_name="branch_b")
        released = executor.accept(sibling, "variant_union")
        assert released.released_tokens == (restored, sibling)
        completed_state_ids = [call.kwargs["state_id"] for call in execution.complete_node_state.call_args_list]
        assert completed_state_ids[0] == "state-restored"
        assert len(completed_state_ids) == 2

    def test_fully_adopted_group_returns_release_for_scheduler_completion(self) -> None:
        executor, execution, _data_flow, _clock = _make_executor()
        _register(executor)
        tok_a = _make_token(token_id="tok_a", branch_name="branch_a")
        tok_b = _make_token(token_id="tok_b", branch_name="branch_b")

        outcomes = executor.restore_from_journal(
            entries=(
                RowUnionRestoreEntry(tok_a, "variant_union", "state-a", 90.0),
                RowUnionRestoreEntry(tok_b, "variant_union", "state-b", 91.0),
            )
        )

        assert len(outcomes) == 1
        assert outcomes[0].released_tokens == (tok_a, tok_b)
        assert [call.kwargs["state_id"] for call in execution.complete_node_state.call_args_list] == ["state-a", "state-b"]


class TestOutcomeInvariants:
    def test_held_with_released_tokens_rejected(self) -> None:
        with pytest.raises(OrchestrationInvariantError):
            RowUnionOutcome(held=True, released_tokens=(_make_token(),))

    def test_released_and_failure_mutually_exclusive(self) -> None:
        with pytest.raises(OrchestrationInvariantError):
            RowUnionOutcome(held=False, released_tokens=(_make_token(),), failure_reason="boom")

    def test_registered_names_listed(self) -> None:
        executor, _execution, _data_flow, _clock = _make_executor()
        _register(executor)
        assert executor.get_registered_names() == ["variant_union"]
