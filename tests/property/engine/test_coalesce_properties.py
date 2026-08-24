# tests/property/engine/test_coalesce_properties.py
"""Property-based tests for CoalesceExecutor merge policies and invariants.

These tests verify the fundamental properties of ELSPETH's coalesce system:

Merge Policy Properties:
- require_all: Merges only when ALL branches arrive
- first: Merges immediately on first arrival
- quorum: Merges when at least quorum_count branches arrive
- best_effort: Merges on timeout with whatever arrived

Memory Properties:
- Completed keys bounded by _max_completed_keys (FIFO eviction)
- Late arrivals after merge return consistent failure

Data Merge Properties:
- union: Combined fields from all branches (later overrides)
- nested: Each branch as nested object with correct hierarchy
- select: Only selected branch's data

The coalesce system is audit-critical - incorrect merging would orphan tokens
or produce incorrect audit trails.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from elspeth.contracts import TokenInfo
from elspeth.contracts.coalesce_enums import CoalescePolicy, MergeStrategy
from elspeth.contracts.enums import FrameKind
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.schema_contract import FieldContract, SchemaContract
from elspeth.contracts.types import NodeID
from elspeth.contracts.union_merge import merge_union_contracts
from elspeth.core.config import CoalesceSettings
from elspeth.engine.clock import MockClock
from elspeth.engine.coalesce_executor import CoalesceExecutor, _merge_with_original_names
from elspeth.engine.spans import SpanFactory
from tests.strategies.json import row_data


class _TestCoalesceExecutor(CoalesceExecutor):
    """Test wrapper that auto-provides output_schema for union merge.

    Production code computes output_schema via the DAG builder's merge_union_fields().
    Tests bypass the DAG builder, so this wrapper provides an OBSERVED-mode schema
    by default, matching the contract mode used by test fixtures.
    """

    def register_coalesce(
        self,
        settings: CoalesceSettings,
        node_id: NodeID,
        branch_schemas: dict[str, tuple[str, ...]] | None = None,
        output_schema: SchemaContract | None = None,
    ) -> None:
        if settings.on_success is None:
            settings = settings.model_copy(update={"on_success": "default"})
        if output_schema is None and settings.merge == "union":
            output_schema = SchemaContract(mode="OBSERVED", fields=(), locked=False)
        super().register_coalesce(settings, node_id, branch_schemas, output_schema)


@dataclass(frozen=True, slots=True)
class _RecordedNodeState:
    state_id: str


class _FakeExecutionRepository:
    def __init__(self) -> None:
        self._next_state_id = 1

    def begin_node_state(self, **_: Any) -> _RecordedNodeState:
        state = _RecordedNodeState(state_id=f"state-{self._next_state_id:03d}")
        self._next_state_id += 1
        return state

    def complete_node_state(self, **_: Any) -> None:
        return None

    def get_completed_row_ids_for_nodes(self, **_: Any) -> list[str]:
        return []

    def has_completed_row_for_node(self, **_: Any) -> bool:
        return False

    def has_completed_group_for_node(self, **_: Any) -> bool:
        return False


class _FakeDataFlowRepository:
    def __init__(self) -> None:
        self.record_token_outcome_error: Exception | None = None

    def record_token_outcome(self, **_: Any) -> None:
        if self.record_token_outcome_error is not None:
            raise self.record_token_outcome_error


class _FakeTokenManager:
    def coalesce_tokens(
        self,
        parents: tuple[TokenInfo, ...],
        merged_data: Any,
        node_id: NodeID,
        run_id: str,
        parent_completions: list[Any],
    ) -> tuple[TokenInfo, str]:
        # Production CoalesceExecutor._execute_merge() passes merged_data as a
        # PipelineRow (already wrapped with contract). Match TokenManager.coalesce_tokens
        # behavior: use merged_data directly as row_data, don't re-wrap. The real
        # TokenManager.coalesce_tokens returns (merged TokenInfo, join_group_id) —
        # join_group_id is an event carried by the tuple/RowResult, never TokenInfo
        # (ruling 20).
        assert len(parent_completions) == len(parents)
        join_group_id = f"join-{parents[0].row_id}"
        merged_token = TokenInfo(
            token_id=f"merged-{parents[0].row_id}",
            row_id=parents[0].row_id,
            row_data=merged_data,
        )
        return merged_token, join_group_id


# =============================================================================
# Strategies for generating coalesce configurations
# =============================================================================

# Branch names (simple alphanumeric)
branch_names = st.text(
    min_size=1,
    max_size=15,
    alphabet="abcdefghijklmnopqrstuvwxyz_",
).filter(lambda s: s[0].isalpha())


# Generate unique branch lists
@st.composite
def branch_lists(draw: st.DrawFn, min_size: int = 2, max_size: int = 5) -> list[str]:
    """Generate a list of unique branch names."""
    size = draw(st.integers(min_value=min_size, max_value=max_size))
    branches = []
    for i in range(size):
        branches.append(f"branch_{i}")
    return branches


def make_token(
    token_id: str,
    row_id: str,
    branch_name: str,
    row_data: dict[str, Any],
) -> TokenInfo:
    """Create a TokenInfo for testing."""
    from elspeth.contracts import PipelineRow
    from elspeth.contracts.schema_contract import FieldContract, SchemaContract

    # Create OBSERVED contract from row data
    fields = tuple(
        FieldContract(
            normalized_name=key,
            original_name=key,
            python_type=object,
            required=False,
            source="inferred",
        )
        for key in row_data
    )
    contract = SchemaContract(mode="OBSERVED", fields=fields, locked=True)
    pipeline_row = PipelineRow(row_data, contract)

    return TokenInfo(
        token_id=token_id,
        row_id=row_id,
        row_data=pipeline_row,
        lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id=f"fg-{row_id}", member_key=branch_name),),
    )


def make_mock_executor(clock: MockClock | None = None) -> _TestCoalesceExecutor:
    """Create a CoalesceExecutor with test fakes for audit dependencies."""
    execution = _FakeExecutionRepository()
    data_flow = _FakeDataFlowRepository()

    span_factory = SpanFactory()
    token_manager = _FakeTokenManager()

    step_resolver = lambda node_id: 0  # noqa: E731

    return _TestCoalesceExecutor(
        execution,
        span_factory=span_factory,
        token_manager=token_manager,
        run_id="test-run",
        step_resolver=step_resolver,
        clock=clock or MockClock(start=0.0),
        data_flow=data_flow,
        barrier_restore_reads=SimpleNamespace(
            get_completed_row_ids_for_nodes=execution.get_completed_row_ids_for_nodes,
            has_completed_row_for_node=execution.has_completed_row_for_node,
            has_completed_group_for_node=execution.has_completed_group_for_node,
        ),
    )


class TestCoalesceAuditCleanupFailures:
    def test_merge_failure_cleanup_audit_error_leaves_pending_for_recovery(self) -> None:
        executor = make_mock_executor()
        assert executor._data_flow is not None
        assert isinstance(executor._data_flow, _FakeDataFlowRepository)
        executor._data_flow.record_token_outcome_error = AuditIntegrityError("token outcome write failed")
        settings = CoalesceSettings(
            name="test_coalesce",
            branches=["branch_a", "branch_b"],
            policy="require_all",
            merge="union",
            union_collision_policy="fail",
        )
        executor.register_coalesce(settings, node_id=NodeID("node-001"))

        token_a = make_token(
            token_id="token-a",
            row_id="row-001",
            branch_name="branch_a",
            row_data={"shared": "a"},
        )
        token_b = make_token(
            token_id="token-b",
            row_id="row-001",
            branch_name="branch_b",
            row_data={"shared": "b"},
        )

        held = executor.accept(token_a, "test_coalesce")
        assert held.held is True

        with pytest.raises(AuditIntegrityError, match="token outcome write failed"):
            executor.accept(token_b, "test_coalesce")

        # key is (name, fork_group_id); make_token derives
        # fork_group_id=f"fg-{row_id}" (WS4 Task 8).
        key = ("test_coalesce", "fg-row-001")
        assert key in executor._pending
        assert key not in executor._completed_keys


# =============================================================================
# Merge Policy Property Tests
# =============================================================================


class TestRequireAllPolicyProperties:
    """Property tests for require_all merge policy."""

    @given(branches=branch_lists(min_size=2, max_size=5))
    @settings(max_examples=50)
    def test_require_all_holds_until_all_arrive(self, branches: list[str]) -> None:
        """Property: require_all holds tokens until ALL branches arrive."""
        executor = make_mock_executor()
        coalesce_settings = CoalesceSettings(
            name="test_coalesce",
            branches=branches,
            policy="require_all",
            merge="union",
        )
        executor.register_coalesce(coalesce_settings, node_id=NodeID("node-001"))

        row_id = "row-001"

        # Send all but one branch
        for i, branch in enumerate(branches[:-1]):
            token = make_token(
                token_id=f"token-{i}",
                row_id=row_id,
                branch_name=branch,
                row_data={"field": i},
            )
            outcome = executor.accept(token, "test_coalesce")
            assert outcome.held is True, f"Should hold after {i + 1}/{len(branches)} branches"
            assert outcome.merged_token is None

        # Send final branch - should merge
        final_token = make_token(
            token_id=f"token-{len(branches) - 1}",
            row_id=row_id,
            branch_name=branches[-1],
            row_data={"field": len(branches) - 1},
        )
        outcome = executor.accept(final_token, "test_coalesce")

        assert outcome.held is False, "Should merge when all branches arrive"
        assert outcome.merged_token is not None
        assert len(outcome.consumed_tokens) == len(branches)

    @given(branches=branch_lists(min_size=3, max_size=5), missing_count=st.integers(min_value=1, max_value=2))
    @settings(max_examples=30)
    def test_require_all_never_partial_merge(self, branches: list[str], missing_count: int) -> None:
        """Property: require_all NEVER does partial merge, even on flush."""
        assume(missing_count < len(branches))

        executor = make_mock_executor()
        coalesce_settings = CoalesceSettings(
            name="test_coalesce",
            branches=branches,
            policy="require_all",
            merge="union",
        )
        executor.register_coalesce(coalesce_settings, node_id=NodeID("node-001"))

        row_id = "row-001"
        arriving_branches = branches[:-missing_count]

        # Send partial branches
        for i, branch in enumerate(arriving_branches):
            token = make_token(
                token_id=f"token-{i}",
                row_id=row_id,
                branch_name=branch,
                row_data={"field": i},
            )
            executor.accept(token, "test_coalesce")

        # Flush pending - should fail, not merge
        outcomes = executor.flush_pending()

        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert outcome.failure_reason == "incomplete_branches"
        assert outcome.merged_token is None
        assert len(outcome.consumed_tokens) == len(arriving_branches)


class TestFirstPolicyProperties:
    """Property tests for first merge policy."""

    @given(branches=branch_lists(min_size=2, max_size=5), first_branch_idx=st.integers(min_value=0, max_value=4))
    @settings(max_examples=50)
    def test_first_merges_immediately(self, branches: list[str], first_branch_idx: int) -> None:
        """Property: first policy merges immediately on first arrival."""
        first_branch_idx = first_branch_idx % len(branches)

        executor = make_mock_executor()
        coalesce_settings = CoalesceSettings(
            name="test_coalesce",
            branches=branches,
            policy="first",
            merge="union",
        )
        executor.register_coalesce(coalesce_settings, node_id=NodeID("node-001"))

        # Send just one token
        token = make_token(
            token_id="token-0",
            row_id="row-001",
            branch_name=branches[first_branch_idx],
            row_data={"value": 42},
        )
        outcome = executor.accept(token, "test_coalesce")

        assert outcome.held is False, "first policy should merge immediately"
        assert outcome.merged_token is not None
        assert len(outcome.consumed_tokens) == 1


@pytest.mark.filterwarnings("ignore:Coalesce.*quorum_count.*equals branch count:UserWarning")
class TestQuorumPolicyProperties:
    """Property tests for quorum merge policy."""

    @given(
        branch_count=st.integers(min_value=3, max_value=6),
        quorum_count=st.integers(min_value=2, max_value=5),
    )
    @settings(max_examples=50)
    def test_quorum_merges_at_exact_threshold(self, branch_count: int, quorum_count: int) -> None:
        """Property: quorum merges exactly when quorum_count branches arrive."""
        assume(quorum_count <= branch_count)

        branches = [f"branch_{i}" for i in range(branch_count)]
        executor = make_mock_executor()
        coalesce_settings = CoalesceSettings(
            name="test_coalesce",
            branches=branches,
            policy="quorum",
            quorum_count=quorum_count,
            merge="union",
        )
        executor.register_coalesce(coalesce_settings, node_id=NodeID("node-001"))

        row_id = "row-001"

        # Send branches up to quorum - 1 (should all hold)
        for i in range(quorum_count - 1):
            token = make_token(
                token_id=f"token-{i}",
                row_id=row_id,
                branch_name=branches[i],
                row_data={"field": i},
            )
            outcome = executor.accept(token, "test_coalesce")
            assert outcome.held is True, f"Should hold at {i + 1} branches (quorum={quorum_count})"

        # Send quorum-th branch - should merge
        quorum_token = make_token(
            token_id=f"token-{quorum_count - 1}",
            row_id=row_id,
            branch_name=branches[quorum_count - 1],
            row_data={"field": quorum_count - 1},
        )
        outcome = executor.accept(quorum_token, "test_coalesce")

        assert outcome.held is False, "Should merge when quorum is met"
        assert outcome.merged_token is not None
        assert len(outcome.consumed_tokens) == quorum_count

    @given(
        branch_count=st.integers(min_value=3, max_value=5),
        quorum_count=st.integers(min_value=2, max_value=4),
    )
    @settings(max_examples=30)
    def test_quorum_flush_fails_below_threshold(self, branch_count: int, quorum_count: int) -> None:
        """Property: quorum flush fails if quorum not met."""
        assume(quorum_count <= branch_count)
        assume(quorum_count > 1)  # Need at least 2 for meaningful test

        branches = [f"branch_{i}" for i in range(branch_count)]
        executor = make_mock_executor()
        coalesce_settings = CoalesceSettings(
            name="test_coalesce",
            branches=branches,
            policy="quorum",
            quorum_count=quorum_count,
            merge="union",
        )
        executor.register_coalesce(coalesce_settings, node_id=NodeID("node-001"))

        row_id = "row-001"

        # Send fewer than quorum
        arriving_count = quorum_count - 1
        for i in range(arriving_count):
            token = make_token(
                token_id=f"token-{i}",
                row_id=row_id,
                branch_name=branches[i],
                row_data={"field": i},
            )
            executor.accept(token, "test_coalesce")

        # Flush - should fail
        outcomes = executor.flush_pending()

        assert len(outcomes) == 1
        assert outcomes[0].failure_reason == "quorum_not_met"
        assert outcomes[0].merged_token is None


class TestBestEffortPolicyProperties:
    """Property tests for best_effort merge policy."""

    @given(branches=branch_lists(min_size=3, max_size=5))
    @settings(max_examples=30)
    def test_best_effort_merges_on_timeout(self, branches: list[str]) -> None:
        """Property: best_effort merges whatever arrived when timeout expires."""
        # Only send partial branches (not all), so timeout is the trigger
        arriving_count = len(branches) - 1

        clock = MockClock(start=0.0)
        executor = make_mock_executor(clock=clock)
        coalesce_settings = CoalesceSettings(
            name="test_coalesce",
            branches=branches,
            policy="best_effort",
            merge="union",
            timeout_seconds=10.0,
        )
        executor.register_coalesce(coalesce_settings, node_id=NodeID("node-001"))

        row_id = "row-001"

        # Send partial branches (not all - so merge doesn't happen immediately)
        for i in range(arriving_count):
            token = make_token(
                token_id=f"token-{i}",
                row_id=row_id,
                branch_name=branches[i],
                row_data={"field": i},
            )
            outcome = executor.accept(token, "test_coalesce")
            # All should be held (best_effort waits for timeout or all branches)
            assert outcome.held is True, "Should hold when not all branches arrived"

        # Advance past timeout
        clock.advance(11.0)

        # Check timeouts - should merge
        outcomes = executor.check_timeouts("test_coalesce")

        assert len(outcomes) == 1
        assert outcomes[0].merged_token is not None
        assert len(outcomes[0].consumed_tokens) == arriving_count


# =============================================================================
# Late Arrival Property Tests
# =============================================================================


class TestLateArrivalProperties:
    """Property tests for late arrival handling."""

    @given(branches=branch_lists(min_size=2, max_size=3))
    @settings(max_examples=30)
    def test_late_arrival_returns_failure(self, branches: list[str]) -> None:
        """Property: After merge completes, late arrivals return failure."""
        executor = make_mock_executor()
        coalesce_settings = CoalesceSettings(
            name="test_coalesce",
            branches=branches,
            policy="require_all",
            merge="union",
        )
        executor.register_coalesce(coalesce_settings, node_id=NodeID("node-001"))

        row_id = "row-001"

        # Complete the merge with all branches
        for i, branch in enumerate(branches):
            token = make_token(
                token_id=f"token-{i}",
                row_id=row_id,
                branch_name=branch,
                row_data={"field": i},
            )
            executor.accept(token, "test_coalesce")

        # Now send a "late" token for same row_id (simulating duplicate/retry)
        late_token = make_token(
            token_id="token-late",
            row_id=row_id,
            branch_name=branches[0],
            row_data={"field": "late"},
        )
        outcome = executor.accept(late_token, "test_coalesce")

        assert outcome.held is False
        assert outcome.failure_reason == "late_arrival_after_merge"
        assert outcome.merged_token is None

    @given(num_late=st.integers(min_value=1, max_value=5))
    @settings(max_examples=20)
    def test_multiple_late_arrivals_all_fail(self, num_late: int) -> None:
        """Property: Multiple late arrivals all consistently fail."""
        branches = ["branch_a", "branch_b"]
        executor = make_mock_executor()
        coalesce_settings = CoalesceSettings(
            name="test_coalesce",
            branches=branches,
            policy="require_all",
            merge="union",
        )
        executor.register_coalesce(coalesce_settings, node_id=NodeID("node-001"))

        row_id = "row-001"

        # Complete the merge
        for i, branch in enumerate(branches):
            token = make_token(
                token_id=f"token-{i}",
                row_id=row_id,
                branch_name=branch,
                row_data={"field": i},
            )
            executor.accept(token, "test_coalesce")

        # Send multiple late arrivals
        for i in range(num_late):
            late_token = make_token(
                token_id=f"token-late-{i}",
                row_id=row_id,
                branch_name=branches[i % len(branches)],
                row_data={"field": f"late-{i}"},
            )
            outcome = executor.accept(late_token, "test_coalesce")

            assert outcome.failure_reason == "late_arrival_after_merge", f"Late arrival {i} should fail consistently"


# =============================================================================
# Memory Bounded Property Tests
# =============================================================================


class TestMemoryBoundedProperties:
    """Property tests for bounded memory invariants."""

    def test_completed_keys_bounded_by_max(self) -> None:
        """Property: _completed_keys never exceeds _max_completed_keys."""
        executor = make_mock_executor()
        # Set a small max for testing
        executor._max_completed_keys = 100

        branches = ["branch_a", "branch_b"]
        coalesce_settings = CoalesceSettings(
            name="test_coalesce",
            branches=branches,
            policy="require_all",
            merge="union",
        )
        executor.register_coalesce(coalesce_settings, node_id=NodeID("node-001"))

        # Complete more merges than max_completed_keys
        for row_num in range(150):
            row_id = f"row-{row_num:05d}"
            for i, branch in enumerate(branches):
                token = make_token(
                    token_id=f"token-{row_num}-{i}",
                    row_id=row_id,
                    branch_name=branch,
                    row_data={"value": row_num},
                )
                executor.accept(token, "test_coalesce")

        # Verify bounded
        assert len(executor._completed_keys) <= 100, f"_completed_keys has {len(executor._completed_keys)} entries, should be <= 100"

    def test_fifo_eviction_preserves_recent(self) -> None:
        """Property: FIFO eviction keeps most recent, evicts oldest."""
        executor = make_mock_executor()
        executor._max_completed_keys = 10

        branches = ["branch_a", "branch_b"]
        coalesce_settings = CoalesceSettings(
            name="test_coalesce",
            branches=branches,
            policy="require_all",
            merge="union",
        )
        executor.register_coalesce(coalesce_settings, node_id=NodeID("node-001"))

        # Complete 20 merges
        for row_num in range(20):
            row_id = f"row-{row_num:03d}"
            for i, branch in enumerate(branches):
                token = make_token(
                    token_id=f"token-{row_num}-{i}",
                    row_id=row_id,
                    branch_name=branch,
                    row_data={"value": row_num},
                )
                executor.accept(token, "test_coalesce")

        # Most recent 10 should be retained (key is (name, fork_group_id);
        # make_token derives fork_group_id=f"fg-{row_id}" — WS4 Task 8).
        for row_num in range(10, 20):
            key = ("test_coalesce", f"fg-row-{row_num:03d}")
            assert key in executor._completed_keys, f"Recent key {key} should be retained"

        # Oldest 10 should be evicted
        for row_num in range(10):
            key = ("test_coalesce", f"fg-row-{row_num:03d}")
            assert key not in executor._completed_keys, f"Old key {key} should be evicted"


# =============================================================================
# Merge Data Strategy Property Tests
# =============================================================================


class TestMergeDataProperties:
    """Property tests for merge data strategies."""

    @given(
        data_a=row_data,
        data_b=row_data,
    )
    @settings(max_examples=50)
    def test_union_merge_contains_all_fields(self, data_a: dict[str, Any], data_b: dict[str, Any]) -> None:
        """Property: union merge contains fields from all branches."""
        executor = make_mock_executor()
        branches = ["branch_a", "branch_b"]
        coalesce_settings = CoalesceSettings(
            name="test_coalesce",
            branches=branches,
            policy="require_all",
            merge="union",
        )
        executor.register_coalesce(coalesce_settings, node_id=NodeID("node-001"))

        # Send both tokens
        token_a = make_token("t-a", "row-001", "branch_a", data_a)
        token_b = make_token("t-b", "row-001", "branch_b", data_b)

        executor.accept(token_a, "test_coalesce")
        outcome = executor.accept(token_b, "test_coalesce")

        assert outcome.merged_token is not None
        merged_data = outcome.merged_token.row_data

        # All keys from both dicts should be in merged (last write wins for conflicts)
        all_keys = set(data_a.keys()) | set(data_b.keys())
        for key in all_keys:
            assert key in merged_data, f"Key '{key}' missing from union merge"

    @given(
        data_a=row_data,
        data_b=row_data,
    )
    @settings(max_examples=50)
    def test_nested_merge_has_branch_hierarchy(self, data_a: dict[str, Any], data_b: dict[str, Any]) -> None:
        """Property: nested merge creates branch-keyed hierarchy."""
        executor = make_mock_executor()
        branches = ["branch_a", "branch_b"]
        coalesce_settings = CoalesceSettings(
            name="test_coalesce",
            branches=branches,
            policy="require_all",
            merge="nested",
        )
        executor.register_coalesce(coalesce_settings, node_id=NodeID("node-001"))

        token_a = make_token("t-a", "row-001", "branch_a", data_a)
        token_b = make_token("t-b", "row-001", "branch_b", data_b)

        executor.accept(token_a, "test_coalesce")
        outcome = executor.accept(token_b, "test_coalesce")

        assert outcome.merged_token is not None
        merged_data = outcome.merged_token.row_data

        # Should have branch names as top-level keys
        assert "branch_a" in merged_data, "nested merge should have 'branch_a' key"
        assert "branch_b" in merged_data, "nested merge should have 'branch_b' key"
        assert merged_data["branch_a"] == data_a
        assert merged_data["branch_b"] == data_b

    @given(data_selected=row_data, data_other=row_data)
    @settings(max_examples=50)
    def test_select_merge_takes_only_selected_branch(self, data_selected: dict[str, Any], data_other: dict[str, Any]) -> None:
        """Property: select merge takes only the selected branch's data."""
        executor = make_mock_executor()
        branches = ["selected_branch", "other_branch"]
        coalesce_settings = CoalesceSettings(
            name="test_coalesce",
            branches=branches,
            policy="require_all",
            merge="select",
            select_branch="selected_branch",
        )
        executor.register_coalesce(coalesce_settings, node_id=NodeID("node-001"))

        token_selected = make_token("t-sel", "row-001", "selected_branch", data_selected)
        token_other = make_token("t-oth", "row-001", "other_branch", data_other)

        executor.accept(token_selected, "test_coalesce")
        outcome = executor.accept(token_other, "test_coalesce")

        assert outcome.merged_token is not None
        merged_data = outcome.merged_token.row_data

        # Should be exactly the selected branch's data
        # row_data is now a PipelineRow, so convert to dict for comparison
        assert merged_data.to_dict() == data_selected, "select merge should use only selected branch"


# =============================================================================
# Token Conservation Property Tests
# =============================================================================


class TestTokenConservationProperties:
    """Property tests for token conservation during coalesce."""

    @given(branches=branch_lists(min_size=2, max_size=5))
    @settings(max_examples=30)
    def test_consumed_tokens_equals_arrived_tokens(self, branches: list[str]) -> None:
        """Property: consumed_tokens count matches number of arrived tokens."""
        executor = make_mock_executor()
        coalesce_settings = CoalesceSettings(
            name="test_coalesce",
            branches=branches,
            policy="require_all",
            merge="union",
        )
        executor.register_coalesce(coalesce_settings, node_id=NodeID("node-001"))

        row_id = "row-001"
        sent_tokens = []

        # Send all branches
        for i, branch in enumerate(branches):
            token = make_token(
                token_id=f"token-{i}",
                row_id=row_id,
                branch_name=branch,
                row_data={"field": i},
            )
            sent_tokens.append(token)
            outcome = executor.accept(token, "test_coalesce")

        # Outcome from last accept has the merge result
        assert len(outcome.consumed_tokens) == len(branches)

        # Verify token IDs match
        consumed_ids = {t.token_id for t in outcome.consumed_tokens}
        sent_ids = {t.token_id for t in sent_tokens}
        assert consumed_ids == sent_ids, "All sent tokens should be consumed"


# =============================================================================
# Coalesce Metadata Property Tests
# =============================================================================


class TestCoalesceMetadataProperties:
    """Property tests for coalesce audit metadata."""

    @given(branches=branch_lists(min_size=2, max_size=4))
    @settings(max_examples=30)
    def test_metadata_contains_policy_and_strategy(self, branches: list[str]) -> None:
        """Property: coalesce metadata includes policy and merge strategy."""
        executor = make_mock_executor()
        coalesce_settings = CoalesceSettings(
            name="test_coalesce",
            branches=branches,
            policy="require_all",
            merge="nested",
        )
        executor.register_coalesce(coalesce_settings, node_id=NodeID("node-001"))

        row_id = "row-001"
        for i, branch in enumerate(branches):
            token = make_token(f"token-{i}", row_id, branch, {"field": i})
            outcome = executor.accept(token, "test_coalesce")

        metadata = outcome.coalesce_metadata
        assert metadata is not None
        assert metadata.policy == CoalescePolicy.REQUIRE_ALL
        assert metadata.merge_strategy == MergeStrategy.NESTED
        assert metadata.expected_branches is not None
        assert metadata.branches_arrived is not None
        assert set(metadata.expected_branches) == set(branches)
        assert set(metadata.branches_arrived) == set(branches)

    @given(branches=branch_lists(min_size=2, max_size=3))
    @settings(max_examples=20)
    def test_metadata_arrival_order_is_chronological(self, branches: list[str]) -> None:
        """Property: arrival_order metadata is sorted chronologically."""
        clock = MockClock(start=0.0)
        executor = make_mock_executor(clock=clock)
        coalesce_settings = CoalesceSettings(
            name="test_coalesce",
            branches=branches,
            policy="require_all",
            merge="union",
        )
        executor.register_coalesce(coalesce_settings, node_id=NodeID("node-001"))

        row_id = "row-001"

        # Send branches with time gaps
        for i, branch in enumerate(branches):
            clock.advance(1.0)  # 1 second between each
            token = make_token(f"token-{i}", row_id, branch, {"field": i})
            outcome = executor.accept(token, "test_coalesce")

        assert outcome.coalesce_metadata is not None
        arrival_order = outcome.coalesce_metadata.arrival_order
        assert arrival_order is not None

        # Verify chronological order
        offsets = [entry.arrival_offset_ms for entry in arrival_order]
        assert offsets == sorted(offsets), "arrival_order should be chronologically sorted"

        # Verify offsets are approximately correct (1000ms apart)
        for i, offset in enumerate(offsets):
            expected_offset = i * 1000  # First is 0, second is 1000, etc.
            assert abs(offset - expected_offset) < 1, f"Offset {i} should be ~{expected_offset}ms"


# =============================================================================
# Schema Contract Merge Invariant Property Tests
# =============================================================================


class TestSchemaMergeInvariantProperties:
    """Property tests for schema contract invariants after merge.

    The coalesce executor produces both merged data AND a merged contract.
    These tests verify the contract invariants that TestMergeDataProperties
    does not cover.
    """

    @given(
        data_a=row_data,
        data_b=row_data,
    )
    @settings(max_examples=50)
    def test_union_merge_contract_contains_all_field_names(self, data_a: dict[str, Any], data_b: dict[str, Any]) -> None:
        """Property: Union merged contract contains fields from all branches."""
        executor = make_mock_executor()
        branches = ["branch_a", "branch_b"]
        coalesce_settings = CoalesceSettings(
            name="test_coalesce",
            branches=branches,
            policy="require_all",
            merge="union",
        )
        executor.register_coalesce(coalesce_settings, node_id=NodeID("node-001"))

        token_a = make_token("t-a", "row-001", "branch_a", data_a)
        token_b = make_token("t-b", "row-001", "branch_b", data_b)

        executor.accept(token_a, "test_coalesce")
        outcome = executor.accept(token_b, "test_coalesce")

        assert outcome.merged_token is not None
        merged_contract = outcome.merged_token.row_data.contract

        # Contract should have fields for all keys from both branches
        all_keys = set(data_a.keys()) | set(data_b.keys())
        contract_fields = {fc.normalized_name for fc in merged_contract.fields}
        for key in all_keys:
            assert key in contract_fields, f"Contract missing field '{key}'"

    @given(
        data_a=row_data,
        data_b=row_data,
    )
    @settings(max_examples=50)
    def test_nested_merge_contract_has_branch_keys_as_object_fields(self, data_a: dict[str, Any], data_b: dict[str, Any]) -> None:
        """Property: Nested merged contract has branch names as object-typed fields."""
        executor = make_mock_executor()
        branches = ["branch_a", "branch_b"]
        coalesce_settings = CoalesceSettings(
            name="test_coalesce",
            branches=branches,
            policy="require_all",
            merge="nested",
        )
        executor.register_coalesce(coalesce_settings, node_id=NodeID("node-001"))

        token_a = make_token("t-a", "row-001", "branch_a", data_a)
        token_b = make_token("t-b", "row-001", "branch_b", data_b)

        executor.accept(token_a, "test_coalesce")
        outcome = executor.accept(token_b, "test_coalesce")

        assert outcome.merged_token is not None
        merged_contract = outcome.merged_token.row_data.contract

        # Contract should have branch names as top-level fields
        assert merged_contract.get_field("branch_a") is not None
        assert merged_contract.get_field("branch_b") is not None

        # Both fields should be typed as object (dict can hold anything)
        assert merged_contract.get_field("branch_a").python_type is object
        assert merged_contract.get_field("branch_b").python_type is object

        # Mode should be FIXED (nested merge produces exact branch structure)
        assert merged_contract.mode == "FIXED"

    @given(data_selected=row_data, data_other=row_data)
    @settings(max_examples=50)
    def test_select_merge_contract_is_selected_branch_contract(self, data_selected: dict[str, Any], data_other: dict[str, Any]) -> None:
        """Property: Select merged contract is exactly the selected branch's contract."""
        executor = make_mock_executor()
        branches = ["selected_branch", "other_branch"]
        coalesce_settings = CoalesceSettings(
            name="test_coalesce",
            branches=branches,
            policy="require_all",
            merge="select",
            select_branch="selected_branch",
        )
        executor.register_coalesce(coalesce_settings, node_id=NodeID("node-001"))

        token_selected = make_token("t-sel", "row-001", "selected_branch", data_selected)
        token_other = make_token("t-oth", "row-001", "other_branch", data_other)

        # Capture selected branch's contract before merge
        selected_contract = token_selected.row_data.contract

        executor.accept(token_selected, "test_coalesce")
        outcome = executor.accept(token_other, "test_coalesce")

        assert outcome.merged_token is not None
        merged_contract = outcome.merged_token.row_data.contract

        # Merged contract should be the selected branch's contract
        assert merged_contract is selected_contract

    @given(branches=branch_lists(min_size=2, max_size=4))
    @settings(max_examples=30)
    def test_merged_contract_is_locked(self, branches: list[str]) -> None:
        """Property: Merged contract is always locked (types are finalized)."""
        executor = make_mock_executor()
        coalesce_settings = CoalesceSettings(
            name="test_coalesce",
            branches=branches,
            policy="require_all",
            merge="union",
        )
        executor.register_coalesce(coalesce_settings, node_id=NodeID("node-001"))

        row_id = "row-001"
        for i, branch in enumerate(branches):
            token = make_token(f"token-{i}", row_id, branch, {"field": i})
            outcome = executor.accept(token, "test_coalesce")

        assert outcome.merged_token is not None
        merged_contract = outcome.merged_token.row_data.contract
        assert merged_contract.locked is True, "Merged contract must be locked"

    @given(branches=branch_lists(min_size=3, max_size=4))
    @settings(max_examples=30)
    def test_nested_merge_contract_branch_required_reflects_arrival(self, branches: list[str]) -> None:
        """Property: Nested contract branch fields are required if branch arrived."""
        executor = make_mock_executor()
        coalesce_settings = CoalesceSettings(
            name="test_coalesce",
            branches=branches,
            policy="require_all",
            merge="nested",
        )
        executor.register_coalesce(coalesce_settings, node_id=NodeID("node-001"))

        row_id = "row-001"
        for i, branch in enumerate(branches):
            token = make_token(f"token-{i}", row_id, branch, {"value": i})
            outcome = executor.accept(token, "test_coalesce")

        assert outcome.merged_token is not None
        merged_contract = outcome.merged_token.row_data.contract

        # All declared branches arrived, so all branch fields should be required
        for branch in branches:
            field = merged_contract.get_field(branch)
            assert field is not None, f"Missing field for branch '{branch}'"
            assert field.required is True, f"Branch '{branch}' field should be required"


# =============================================================================
# Union Contract Merge Bijection Property Tests (P9 regression)
# =============================================================================


def make_renamed_token(
    token_id: str,
    row_id: str,
    branch_name: str,
    fields: dict[str, tuple[str, Any]],
) -> TokenInfo:
    """Create a token whose contract carries renames (original != normalized).

    fields maps normalized_name -> (original_name, value).
    """
    from elspeth.contracts import PipelineRow

    contract = SchemaContract(
        mode="OBSERVED",
        fields=tuple(
            FieldContract(
                normalized_name=normalized,
                original_name=original,
                python_type=object,
                required=False,
                source="inferred",
            )
            for normalized, (original, _) in fields.items()
        ),
        locked=True,
    )
    row = PipelineRow({normalized: value for normalized, (_, value) in fields.items()}, contract)
    return TokenInfo(
        token_id=token_id,
        row_id=row_id,
        row_data=row,
        lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id=f"fg-{row_id}", member_key=branch_name),),
    )


# Small alphabet forces frequent cross-branch original_name collisions —
# the P9 shape (two branches renaming one upstream field differently).
_rename_names = st.text(alphabet="abcde", min_size=1, max_size=4)


@st.composite
def renamed_branch_contracts(draw: st.DrawFn) -> dict[str, SchemaContract]:
    """Branch contracts whose fields may rename shared upstream names.

    Each branch contract stays individually bijective (dict keys are unique
    normalized names; entries repeating an original within the branch are
    dropped) — only CROSS-branch original_name collisions survive, which is
    exactly what the merge must resolve.
    """
    n_branches = draw(st.integers(min_value=2, max_value=4))
    contracts: dict[str, SchemaContract] = {}
    for i in range(n_branches):
        mapping = draw(st.dictionaries(_rename_names, _rename_names, min_size=0, max_size=5))
        seen_originals: set[str] = set()
        branch_fields: list[FieldContract] = []
        for normalized, original in mapping.items():
            if original in seen_originals:
                continue
            seen_originals.add(original)
            branch_fields.append(
                FieldContract(
                    normalized_name=normalized,
                    original_name=original,
                    python_type=object,
                    required=False,
                    source="inferred",
                )
            )
        contracts[f"branch_{i}"] = SchemaContract(mode="OBSERVED", fields=tuple(branch_fields), locked=True)
    return contracts


class TestUnionContractMergeBijectionProperties:
    """Union contract merges never raise bare ValueError (P9 regression).

    SchemaContract.__post_init__ enforces an original_name -> normalized_name
    bijection with a bare ValueError. Cross-branch renames of one upstream
    field must be resolved by the merge (identity original_name on colliding
    fields), never surfaced as a first-row crash after green validation.
    """

    @given(branch_contracts=renamed_branch_contracts(), require_all=st.booleans())
    @settings(max_examples=200)
    def test_merge_union_contracts_never_raises_value_error(
        self,
        branch_contracts: dict[str, SchemaContract],
        require_all: bool,
    ) -> None:
        merged = merge_union_contracts(
            branch_contracts,
            require_all=require_all,
            branch_order=tuple(branch_contracts),
        )
        expected = {fc.normalized_name for contract in branch_contracts.values() for fc in contract.fields}
        assert {fc.normalized_name for fc in merged.fields} == expected
        originals = [fc.original_name for fc in merged.fields]
        assert len(set(originals)) == len(originals), "merged contract must keep the original_name bijection"

    @given(branch_contracts=renamed_branch_contracts(), data=st.data())
    @settings(max_examples=200)
    def test_merge_with_original_names_never_raises_value_error(
        self,
        branch_contracts: dict[str, SchemaContract],
        data: st.DataObject,
    ) -> None:
        """The precomputed-schema (typed union) path shares the same hazard."""
        all_names = sorted({fc.normalized_name for contract in branch_contracts.values() for fc in contract.fields})
        precomputed = SchemaContract(
            mode="FIXED",
            fields=tuple(
                FieldContract(
                    normalized_name=name,
                    original_name=name,
                    python_type=object,
                    required=False,
                    source="declared",
                )
                for name in all_names
            ),
            locked=True,
        )
        field_origins = {
            name: data.draw(
                st.sampled_from([branch for branch, contract in branch_contracts.items() if contract.find_field(name) is not None]),
                label=name,
            )
            for name in all_names
        }
        merged = _merge_with_original_names(precomputed, branch_contracts, field_origins)
        originals = [fc.original_name for fc in merged.fields]
        assert len(set(originals)) == len(originals), "merged contract must keep the original_name bijection"


class TestUnionRenameCollisionExecutor:
    """P9 repro shape through the executor: two branches rename one field."""

    def test_cross_branch_rename_merges_and_metadata_keeps_real_origins(self) -> None:
        executor = make_mock_executor()
        coalesce_settings = CoalesceSettings(
            name="merge_currencies",
            branches=["branch_a", "branch_b"],
            policy="require_all",
            merge="union",
        )
        executor.register_coalesce(coalesce_settings, node_id=NodeID("node-001"))

        token_a = make_renamed_token("t-a", "row-001", "branch_a", {"id": ("id", 1), "amount_aud": ("amount", 100)})
        token_b = make_renamed_token("t-b", "row-001", "branch_b", {"id": ("id", 1), "amount_usd": ("amount", 150)})

        executor.accept(token_a, "merge_currencies")
        outcome = executor.accept(token_b, "merge_currencies")

        assert outcome.merged_token is not None
        contract = outcome.merged_token.row_data.contract
        assert contract.get_field("amount_aud").original_name == "amount_aud"
        assert contract.get_field("amount_usd").original_name == "amount_usd"
        assert contract.get_field("id").original_name == "id"

        # The contract drops the ambiguous lineage; the audit metadata keeps it.
        assert outcome.coalesce_metadata is not None
        origins = outcome.coalesce_metadata.union_field_origins
        assert origins is not None
        assert origins["amount_aud"] == "branch_a"
        assert origins["amount_usd"] == "branch_b"
