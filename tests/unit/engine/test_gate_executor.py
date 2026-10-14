"""Gate executor routing, dispatch, outcomes, and audit failure tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from elspeth.contracts import (
    TokenInfo,
)
from elspeth.contracts.enums import (
    NodeStateStatus,
    RoutingKind,
    RoutingMode,
)
from elspeth.contracts.errors import (
    OrchestrationInvariantError,
)
from elspeth.contracts.events import EngineSpanCompleted, EngineSpanName, EngineSpanStatus
from elspeth.contracts.results import GateResult
from elspeth.contracts.routing import RouteDestination, RoutingAction
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.contracts.types import NodeID, SinkName
from elspeth.core.config import GateSettings
from elspeth.engine.executors import (
    GateExecutor,
    GateOutcome,
    MissingEdgeError,
)
from elspeth.engine.spans import SpanFactory
from tests.fixtures.executors import (
    _CallRecorder,
    _make_contract,
    _make_factory,
    _make_span_factory,
    _make_token,
    _single_complete_node_state_kwargs,
)
from tests.fixtures.factories import make_context
from tests.unit.engine.conftest import make_test_step_resolver as _make_step_resolver


def _make_token_manager(children: list[TokenInfo], fork_group_id: str) -> SimpleNamespace:
    return SimpleNamespace(fork_token=_CallRecorder(return_value=(children, fork_group_id)))


# =============================================================================
# TestMissingEdgeError
# =============================================================================


class TestMissingEdgeError:
    """Tests for the MissingEdgeError exception class."""

    def test_stores_node_id_and_label(self) -> None:
        err = MissingEdgeError(node_id=NodeID("gate_1"), label="above")
        assert err.node_id == NodeID("gate_1")
        assert err.label == "above"

    def test_message_includes_both(self) -> None:
        err = MissingEdgeError(node_id=NodeID("gate_1"), label="above")
        assert "gate_1" in str(err)
        assert "above" in str(err)

    def test_is_exception(self) -> None:
        err = MissingEdgeError(node_id=NodeID("n"), label="l")
        assert isinstance(err, Exception)


# =============================================================================
# TestGateOutcome
# =============================================================================


class TestGateOutcome:
    """Tests for the GateOutcome dataclass."""

    def test_default_values(self) -> None:
        result = GateResult(row={"a": 1}, action=RoutingAction.continue_())
        token = _make_token()
        outcome = GateOutcome(result=result, updated_token=token)
        assert outcome.child_tokens == ()
        assert outcome.sink_name is None
        assert outcome.next_node_id is None

    def test_route_to_sink(self) -> None:
        """ROUTE action with sink_name is a valid routing outcome."""
        result = GateResult(row={"a": 1}, action=RoutingAction.route("error", reason=None))
        token = _make_token()
        outcome = GateOutcome(
            result=result,
            updated_token=token,
            sink_name="error_sink",
        )
        assert outcome.sink_name == "error_sink"
        assert outcome.next_node_id is None
        assert outcome.child_tokens == ()

    def test_fork_with_children(self) -> None:
        """FORK_TO_PATHS action with child_tokens is a valid routing outcome."""
        result = GateResult(row={"a": 1}, action=RoutingAction.fork_to_paths(["path_a", "path_b"]))
        token = _make_token()
        child_a = _make_token(token_id="child_1")
        child_b = _make_token(token_id="child_2")
        outcome = GateOutcome(
            result=result,
            updated_token=token,
            child_tokens=(child_a, child_b),
        )
        assert len(outcome.child_tokens) == 2
        assert outcome.child_tokens[0].token_id == "child_1"
        assert outcome.child_tokens[1].token_id == "child_2"
        assert outcome.sink_name is None

    def test_frozen_rejects_field_reassignment(self) -> None:
        """Frozen dataclass prevents post-construction mutation of fields."""
        result = GateResult(row={"a": 1}, action=RoutingAction.continue_())
        token = _make_token()
        outcome = GateOutcome(result=result, updated_token=token)
        with pytest.raises(AttributeError):
            outcome.sink_name = "mutated"  # type: ignore[misc]

    def test_child_tokens_is_tuple(self) -> None:
        """child_tokens is converted to tuple for deep immutability."""
        result = GateResult(row={"a": 1}, action=RoutingAction.fork_to_paths(["a", "b"]))
        token = _make_token()
        child_a = _make_token(token_id="child_1")
        child_b = _make_token(token_id="child_2")
        outcome = GateOutcome(
            result=result,
            updated_token=token,
            child_tokens=(child_a, child_b),
        )
        assert isinstance(outcome.child_tokens, tuple)

    def test_continue_rejects_sink_name(self) -> None:
        """CONTINUE action with sink_name is an impossible routing state."""
        result = GateResult(row={"a": 1}, action=RoutingAction.continue_())
        with pytest.raises(ValueError, match="CONTINUE action cannot have sink_name"):
            GateOutcome(result=result, updated_token=_make_token(), sink_name="oops")

    def test_continue_rejects_child_tokens(self) -> None:
        """CONTINUE action with child_tokens is an impossible routing state."""
        result = GateResult(row={"a": 1}, action=RoutingAction.continue_())
        with pytest.raises(ValueError, match="CONTINUE action cannot have child_tokens"):
            GateOutcome(result=result, updated_token=_make_token(), child_tokens=(_make_token(),))

    def test_route_rejects_child_tokens(self) -> None:
        """ROUTE action cannot produce child tokens."""
        result = GateResult(row={"a": 1}, action=RoutingAction.route("sink_a"))
        with pytest.raises(ValueError, match="ROUTE action cannot have child_tokens"):
            GateOutcome(result=result, updated_token=_make_token(), child_tokens=(_make_token(),), sink_name="s")

    def test_route_requires_destination(self) -> None:
        """ROUTE action must specify sink_name, next_node_id, or discard."""
        result = GateResult(row={"a": 1}, action=RoutingAction.route("label"))
        with pytest.raises(ValueError, match="ROUTE action must have exactly one"):
            GateOutcome(result=result, updated_token=_make_token())

    def test_fork_requires_child_tokens(self) -> None:
        """FORK_TO_PATHS action without children is an impossible state."""
        result = GateResult(row={"a": 1}, action=RoutingAction.fork_to_paths(["a", "b"]))
        with pytest.raises(ValueError, match="FORK_TO_PATHS action must have non-empty child_tokens"):
            GateOutcome(result=result, updated_token=_make_token())

    def test_fork_requires_child_per_destination(self) -> None:
        """FORK_TO_PATHS action must preserve action destination and child cardinality."""
        result = GateResult(row={"a": 1}, action=RoutingAction.fork_to_paths(["a", "b"]))
        with pytest.raises(ValueError, match="FORK_TO_PATHS action destinations"):
            GateOutcome(
                result=result,
                updated_token=_make_token(),
                child_tokens=(_make_token(token_id="child_1"),),
            )

    def test_fork_rejects_sink_name(self) -> None:
        """FORK_TO_PATHS action cannot have sink_name."""
        result = GateResult(row={"a": 1}, action=RoutingAction.fork_to_paths(["a", "b"]))
        with pytest.raises(ValueError, match="FORK_TO_PATHS action cannot have sink_name"):
            GateOutcome(result=result, updated_token=_make_token(), child_tokens=(_make_token(),), sink_name="s")


# =============================================================================
# TestGateExecutor
# =============================================================================


class TestGateExecutor:
    """Tests for GateExecutor covering execute_config_gate."""

    # --- execute_config_gate ---

    @pytest.mark.parametrize(
        ("resume_offset", "claim_offset", "checkpoint_id", "expected_attempt"),
        [(0, 2, None, 2), (3, 0, "cp-recovery", 3)],
        ids=["lease_claim", "checkpoint_resume"],
    )
    def test_config_gate_records_recovery_attempt_and_provenance(
        self, resume_offset: int, claim_offset: int, checkpoint_id: str | None, expected_attempt: int
    ) -> None:
        factory = _make_factory()
        executor = GateExecutor(
            factory.execution,
            _make_span_factory(),
            _make_step_resolver(),
            route_resolution_map={(NodeID("cg_1"), "true"): RouteDestination.discard()},
        )
        config = GateSettings(name="my_gate", input="in_conn", condition="True", routes={"true": "discard", "false": "discard"})
        token = _make_token(contract=_make_contract())
        token = TokenInfo(
            row_id=token.row_id,
            token_id=token.token_id,
            row_data=token.row_data,
            resume_attempt_offset=resume_offset,
            resume_checkpoint_id=checkpoint_id,
        )

        executor.execute_config_gate(config, "cg_1", token, make_context(), attempt_offset=claim_offset)

        assert factory.execution.begin_node_state.call_args.kwargs["attempt"] == expected_attempt
        assert factory.execution.begin_node_state.call_args.kwargs["resume_checkpoint_id"] == checkpoint_id

    def test_config_gate_boolean_true_routes_via_true_label(self) -> None:
        """Boolean True condition evaluates to 'true' label."""
        factory = _make_factory()
        edge_map = {(NodeID("cg_1"), "true"): "edge_true"}
        route_map = {(NodeID("cg_1"), "true"): RouteDestination.processing_node(NodeID("next_node"))}
        executor = GateExecutor(
            factory.execution,
            _make_span_factory(),
            _make_step_resolver(),
            edge_map=edge_map,
            route_resolution_map=route_map,
        )
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition="True",
            routes={"true": "next_conn", "false": "error_sink"},
        )
        contract = _make_contract()
        token = _make_token(contract=contract)
        ctx = make_context()

        outcome = executor.execute_config_gate(
            config,
            "cg_1",
            token,
            ctx,
            attempt_offset=0,
        )

        assert outcome.sink_name is None
        assert outcome.next_node_id == NodeID("next_node")

    def test_config_gate_reuses_condition_parser_per_gate(self) -> None:
        """Repeated tokens for the same gate reuse one parsed condition."""

        class _CountingParser:
            constructed = 0
            evaluated = 0

            def __init__(self, condition: str) -> None:
                type(self).constructed += 1
                self.condition = condition

            def evaluate(self, row: PipelineRow) -> bool:
                type(self).evaluated += 1
                assert row["value"] == "test"
                return True

        factory = _make_factory()
        edge_map = {(NodeID("cg_1"), "true"): "edge_true"}
        route_map = {(NodeID("cg_1"), "true"): RouteDestination.processing_node(NodeID("next_node"))}
        executor = GateExecutor(
            factory.execution,
            _make_span_factory(),
            _make_step_resolver(),
            edge_map=edge_map,
            route_resolution_map=route_map,
        )
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition="True",
            routes={"true": "next_conn", "false": "error_sink"},
        )
        contract = _make_contract()
        first_token = _make_token(contract=contract, token_id="tok_1")
        second_token = _make_token(contract=contract, token_id="tok_2")
        ctx = make_context()

        with patch("elspeth.engine.executors.gate.ExpressionParser", _CountingParser):
            first_outcome = executor.execute_config_gate(config, "cg_1", first_token, ctx, attempt_offset=0)
            second_outcome = executor.execute_config_gate(config, "cg_1", second_token, ctx, attempt_offset=0)

        assert first_outcome.next_node_id == NodeID("next_node")
        assert second_outcome.next_node_id == NodeID("next_node")
        assert _CountingParser.evaluated == 2
        assert _CountingParser.constructed == 1

    def test_config_gate_boolean_false_routes_via_false_label(self) -> None:
        """Boolean False condition evaluates to 'false' label."""
        factory = _make_factory()
        # Edge map must have the route label that will be used for recording
        edge_map = {(NodeID("cg_1"), "false"): "edge_false"}
        route_map = {(NodeID("cg_1"), "false"): RouteDestination.sink(SinkName("error_sink"))}
        executor = GateExecutor(
            factory.execution,
            _make_span_factory(),
            _make_step_resolver(),
            edge_map=edge_map,
            route_resolution_map=route_map,
        )
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition="False",
            routes={"true": "next_conn", "false": "error_sink"},
        )
        contract = _make_contract()
        token = _make_token(contract=contract)
        ctx = make_context()

        outcome = executor.execute_config_gate(
            config,
            "cg_1",
            token,
            ctx,
            attempt_offset=0,
        )

        assert outcome.sink_name == "error_sink"

    def test_config_gate_discard_route_returns_virtual_discard_without_edge(self) -> None:
        """A gate route to 'discard' terminates without requiring a sink edge."""
        factory = _make_factory()
        route_map = {(NodeID("cg_1"), "false"): RouteDestination.discard()}
        executor = GateExecutor(
            factory.execution,
            _make_span_factory(),
            _make_step_resolver(),
            edge_map={},
            route_resolution_map=route_map,
        )
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition="False",
            routes={"true": "next_conn", "false": "discard"},
        )
        token = _make_token(contract=_make_contract())
        ctx = make_context()

        outcome = executor.execute_config_gate(
            config,
            "cg_1",
            token,
            ctx,
            attempt_offset=0,
        )

        assert outcome.discarded is True
        assert outcome.sink_name is None
        assert outcome.next_node_id is None
        assert outcome.child_tokens == ()
        factory.execution.record_routing_event.assert_not_called()

    def test_config_gate_string_result_used_as_label(self) -> None:
        """String condition result used as route label directly."""
        factory = _make_factory()
        edge_map = {(NodeID("cg_1"), "high"): "edge_high"}
        route_map = {(NodeID("cg_1"), "high"): RouteDestination.processing_node(NodeID("next_node"))}
        executor = GateExecutor(
            factory.execution,
            _make_span_factory(),
            _make_step_resolver(),
            edge_map=edge_map,
            route_resolution_map=route_map,
        )
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition="'high'",
            routes={"high": "next_conn", "low": "error_sink"},
        )
        contract = _make_contract()
        token = _make_token(contract=contract)
        ctx = make_context()

        outcome = executor.execute_config_gate(
            config,
            "cg_1",
            token,
            ctx,
            attempt_offset=0,
        )

        assert outcome.sink_name is None
        assert outcome.next_node_id == NodeID("next_node")

    def test_config_gate_route_to_processing_node(self) -> None:
        """Config gate route label can branch to a processing node."""
        factory = _make_factory()
        edge_map = {(NodeID("cg_1"), "high"): "edge_high"}
        route_map = {(NodeID("cg_1"), "high"): RouteDestination.processing_node(NodeID("transform_2"))}
        executor = GateExecutor(
            factory.execution,
            _make_span_factory(),
            _make_step_resolver(),
            edge_map=edge_map,
            route_resolution_map=route_map,
        )
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition="'high'",
            routes={"high": "branch_conn", "low": "error_sink"},
        )
        contract = _make_contract()
        token = _make_token(contract=contract)
        ctx = make_context()

        outcome = executor.execute_config_gate(
            config,
            "cg_1",
            token,
            ctx,
            attempt_offset=0,
        )

        assert outcome.sink_name is None
        assert outcome.next_node_id == NodeID("transform_2")

    def test_config_gate_unknown_route_label_raises_value_error(self) -> None:
        """Unknown route label raises ValueError with FAILED state recorded."""
        factory = _make_factory()
        executor = GateExecutor(factory.execution, _make_span_factory(), _make_step_resolver())
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition="'unknown_label'",
            routes={"high": "next_conn", "low": "error_sink"},
        )
        contract = _make_contract()
        token = _make_token(contract=contract)
        ctx = make_context()

        with pytest.raises(ValueError, match=r"unconfigured route label \(type=str, length=13\).*Expression: 'unknown_label'"):
            executor.execute_config_gate(
                config,
                "cg_1",
                token,
                ctx,
                attempt_offset=0,
            )

        failed_kwargs = _single_complete_node_state_kwargs(factory, status=NodeStateStatus.FAILED)
        assert failed_kwargs["error"].phase == "gate_evaluation_routing"

    def test_post_evaluation_route_failure_marks_gate_span_error(self) -> None:
        """The gate span stays open through route admission and guard failure audit."""
        events: list[EngineSpanCompleted] = []
        spans = SpanFactory(telemetry_emit=events.append)
        factory = _make_factory()
        executor = GateExecutor(factory.execution, spans, _make_step_resolver())
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition="'unknown_label'",
            routes={"known": "next_conn"},
        )

        with (
            spans.trace_scope("run_1", datetime.now(UTC)),
            pytest.raises(ValueError, match=r"unconfigured route label \(type=str, length=13\).*Expression: 'unknown_label'"),
        ):
            executor.execute_config_gate(config, "cg_1", _make_token(), make_context(run_id="run_1"), attempt_offset=0)

        assert len(events) == 1
        assert events[0].name is EngineSpanName.GATE
        assert events[0].status is EngineSpanStatus.ERROR
        assert events[0].exception_type == "ValueError"

    def test_handled_gate_expression_error_marks_gate_span_error(self) -> None:
        """A configured expression-error route remains a failed gate operation."""
        events: list[EngineSpanCompleted] = []
        spans = SpanFactory(telemetry_emit=events.append)
        factory = _make_factory()
        executor = GateExecutor(
            factory.execution,
            spans,
            _make_step_resolver(),
            error_edge_ids={NodeID("cg_1"): "edge_gate_error"},
        )
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition="row['missing'] > 0",
            routes={"true": "next_conn", "false": "error_sink"},
            on_error="discard",
        )

        with spans.trace_scope("run_1", datetime.now(UTC)):
            outcome = executor.execute_config_gate(config, "cg_1", _make_token(), make_context(run_id="run_1"), attempt_offset=0)

        assert outcome.discarded is True
        assert len(events) == 1
        assert events[0].status is EngineSpanStatus.ERROR
        assert events[0].exception_type == "ExpressionEvaluationError"

    @pytest.mark.parametrize(
        "secret_label",
        [
            # short: the retired 80-char bounded preview printed it whole (C3 fix round 1)
            "CUSTOMER_PRIVATE_739",
            "sk-or-v1-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" + ("x" * 100),
        ],
    )
    def test_config_gate_unknown_route_label_error_redacts_row_derived_value(self, secret_label: str) -> None:
        """Unknown row-derived route labels must not leak raw values (nor a digest of them) to audit text."""
        from elspeth.contracts.errors import ExecutionError

        factory = _make_factory()
        executor = GateExecutor(factory.execution, _make_span_factory(), _make_step_resolver())
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition="row['route_label']",
            routes={"known": "next_conn"},
        )
        token = _make_token(data={"value": "test", "route_label": secret_label}, contract=_make_contract())
        ctx = make_context()

        with pytest.raises(ValueError) as exc_info:
            executor.execute_config_gate(config, "cg_1", token, ctx, attempt_offset=0)

        assert str(exc_info.value) == (
            f"Gate 'my_gate' condition returned unconfigured route label (type=str, length={len(secret_label)}); "
            "configured routes: ['known']. Expression: row['route_label']"
        )

        failed_kwargs = _single_complete_node_state_kwargs(factory, status=NodeStateStatus.FAILED)
        error_obj = failed_kwargs.get("error")
        assert isinstance(error_obj, ExecutionError)
        assert secret_label not in error_obj.exception

    def test_config_gate_fork_destination_creates_children(self) -> None:
        """Config gate with 'fork' destination creates child tokens."""
        factory = _make_factory()
        edge_map = {
            (NodeID("cg_1"), "path_a"): "edge_a",
            (NodeID("cg_1"), "path_b"): "edge_b",
            (NodeID("cg_1"), "continue"): "edge_cont",
        }
        route_map = {(NodeID("cg_1"), "true"): RouteDestination.fork()}
        executor = GateExecutor(
            factory.execution,
            _make_span_factory(),
            _make_step_resolver(),
            edge_map=edge_map,
            route_resolution_map=route_map,
        )
        # Boolean condition requires both true and false routes
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition="True",
            routes={"true": "fork", "false": "error_sink"},
            fork_to=["path_a", "path_b"],
        )

        child_a = _make_token(token_id="child_a")
        child_b = _make_token(token_id="child_b")
        token_manager = _make_token_manager([child_a, child_b], "fg_001")

        contract = _make_contract()
        token = _make_token(contract=contract)
        ctx = make_context()

        outcome = executor.execute_config_gate(
            config,
            "cg_1",
            token,
            ctx,
            token_manager=token_manager,
            attempt_offset=0,
        )

        assert len(outcome.child_tokens) == 2
        token_manager.fork_token.assert_called_once()

    def test_config_gate_missing_token_manager_records_failed_state(self) -> None:
        """Dispatch errors (including invariant violations) must close node state as FAILED.

        All exceptions during dispatch/routing close the node state to prevent
        non-terminal OPEN states in the audit trail.
        """
        factory = _make_factory()
        edge_map = {
            (NodeID("cg_1"), "path_a"): "edge_a",
            (NodeID("cg_1"), "path_b"): "edge_b",
            (NodeID("cg_1"), "continue"): "edge_cont",
        }
        route_map = {(NodeID("cg_1"), "true"): RouteDestination.fork()}
        executor = GateExecutor(
            factory.execution,
            _make_span_factory(),
            _make_step_resolver(),
            edge_map=edge_map,
            route_resolution_map=route_map,
        )
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition="True",
            routes={"true": "fork", "false": "error_sink"},
            fork_to=["path_a", "path_b"],
        )
        token = _make_token(contract=_make_contract())
        ctx = make_context()

        with pytest.raises(OrchestrationInvariantError, match="no TokenManager"):
            executor.execute_config_gate(
                config,
                "cg_1",
                token,
                ctx,
                token_manager=None,
                attempt_offset=0,
            )

        _single_complete_node_state_kwargs(factory, status=NodeStateStatus.FAILED)

    def test_config_gate_missing_route_resolution_fails_closed(self) -> None:
        """Missing route resolution mapping raises MissingEdgeError (no fallback)."""
        factory = _make_factory()
        edge_map = {(NodeID("cg_1"), "continue"): "edge_cont"}
        executor = GateExecutor(factory.execution, _make_span_factory(), _make_step_resolver(), edge_map=edge_map)
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition="'high'",
            routes={"high": "branch_conn"},
        )
        contract = _make_contract()
        token = _make_token(contract=contract)
        ctx = make_context()

        with pytest.raises(MissingEdgeError, match="cg_1"):
            executor.execute_config_gate(
                config,
                "cg_1",
                token,
                ctx,
                attempt_offset=0,
            )

        _single_complete_node_state_kwargs(factory, status=NodeStateStatus.FAILED)

    def test_config_gate_exception_records_failed_and_reraises(self) -> None:
        """Exception during config gate eval records FAILED and re-raises.

        Note: GateSettings validates condition syntax at construction, so we
        use a condition that is syntactically valid but raises at evaluation time
        (e.g., accessing a non-existent field on the row data via a runtime error).
        """
        factory = _make_factory()
        executor = GateExecutor(factory.execution, _make_span_factory(), _make_step_resolver())
        # Syntactically valid but references a key that will cause evaluation error
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition="row['nonexistent_field'] > 0",
            routes={"true": "next_conn", "false": "error_sink"},
        )
        contract = _make_contract()
        token = _make_token(contract=contract)
        ctx = make_context()

        from elspeth.core.expression_parser import ExpressionEvaluationError

        with pytest.raises(ExpressionEvaluationError):
            executor.execute_config_gate(
                config,
                "cg_1",
                token,
                ctx,
                attempt_offset=0,
            )

        failed_kwargs = _single_complete_node_state_kwargs(factory, status=NodeStateStatus.FAILED)
        assert failed_kwargs["error"].phase == "gate_evaluation_routing"

    def test_config_gate_expression_error_routes_one_row_with_divert_evidence(self) -> None:
        """A configured expression failure returns a per-row failure outcome."""
        factory = _make_factory()
        executor = GateExecutor(
            factory.execution,
            _make_span_factory(),
            _make_step_resolver(),
            error_edge_ids={NodeID("cg_1"): "edge_gate_error"},
        )
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition="row['nonexistent_field'] > 0",
            routes={"true": "next_conn", "false": "error_sink"},
            on_error="gate_errors",
        )

        ctx = make_context()
        outcome = executor.execute_config_gate(
            config,
            "cg_1",
            _make_token(contract=_make_contract()),
            ctx,
            attempt_offset=0,
        )

        assert outcome.sink_name == "gate_errors"
        assert outcome.discarded is False
        assert outcome.error is not None
        assert outcome.error.exception_type == "ExpressionEvaluationError"
        assert outcome.result.action.mode == RoutingMode.DIVERT
        failed_kwargs = _single_complete_node_state_kwargs(factory, status=NodeStateStatus.FAILED)
        assert failed_kwargs["error"].exception_type == "ExpressionEvaluationError"
        factory.execution.record_routing_event.assert_called_once_with(
            member_token=ctx.require_member_token(),
            state_id="state_001",
            edge_id="edge_gate_error",
            mode=RoutingMode.DIVERT,
            reason={
                "condition": "row['nonexistent_field'] > 0",
                "error_type": "ExpressionEvaluationError",
                "error": "gate expression evaluation failed: missing key",
            },
        )

    @pytest.mark.parametrize(
        ("condition", "row", "row_derived_text", "classification"),
        [
            (
                "row[row['selector']]",
                {"value": "x", "selector": "customer-private-field-7f31"},
                "customer-private-field-7f31",
                "gate expression evaluation failed: missing key",
            ),
            (
                "row['items'][row['index']] > 0",
                {"value": "x", "items": [1], "index": 918273},
                "918273",
                "gate expression evaluation failed: index out of range",
            ),
        ],
    )
    def test_config_gate_handled_error_evidence_never_persists_row_derived_key_or_index(
        self,
        condition: str,
        row: dict[str, Any],
        row_derived_text: str,
        classification: str,
    ) -> None:
        """Handled row failures carry only closed, bounded classifications."""
        factory = _make_factory()
        executor = GateExecutor(
            factory.execution,
            _make_span_factory(),
            _make_step_resolver(),
            error_edge_ids={NodeID("cg_1"): "edge_gate_error"},
        )
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition=condition,
            routes={"true": "next_conn", "false": "error_sink"},
            on_error="gate_errors",
        )

        outcome = executor.execute_config_gate(
            config,
            "cg_1",
            _make_token(data=row, contract=_make_contract()),
            make_context(),
            attempt_offset=0,
        )

        assert outcome.error is not None
        assert outcome.error.message == classification
        failed_kwargs = _single_complete_node_state_kwargs(factory, status=NodeStateStatus.FAILED)
        assert failed_kwargs["error"].exception == classification
        routing_reason = factory.execution.record_routing_event.call_args.kwargs["reason"]
        assert routing_reason["error"] == classification
        persisted_evidence = repr((outcome.error, failed_kwargs["error"], routing_reason))
        assert row_derived_text not in persisted_evidence

    def test_handled_gate_classification_covers_every_evaluation_kind(self) -> None:
        """The handled route's closed sentence is keyed on ``ExpressionEvaluationError.kind``, every kind mapped."""
        from typing import get_args

        from elspeth.core.expression_parser import ExpressionEvaluationKind
        from elspeth.engine.executors.gate import _HANDLED_GATE_EVALUATION_ERRORS

        assert set(_HANDLED_GATE_EVALUATION_ERRORS) == set(get_args(ExpressionEvaluationKind))

    def test_config_gate_error_route_without_divert_edge_fails_closed(self) -> None:
        """Missing structural audit evidence must not silently route the row."""
        factory = _make_factory()
        executor = GateExecutor(factory.execution, _make_span_factory(), _make_step_resolver())
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition="row['nonexistent_field'] > 0",
            routes={"true": "next_conn", "false": "error_sink"},
            on_error="gate_errors",
        )

        with pytest.raises(OrchestrationInvariantError, match="no DIVERT edge registered"):
            executor.execute_config_gate(
                config,
                "cg_1",
                _make_token(contract=_make_contract()),
                make_context(),
                attempt_offset=0,
            )

        _single_complete_node_state_kwargs(factory, status=NodeStateStatus.FAILED)

    def test_config_gate_runtime_error_in_dispatch_records_failed_state(self) -> None:
        """RuntimeError during dispatch records FAILED state (Phase 0 fix #8).

        Bug: The except clause only caught MissingEdgeError, so other exception
        types (RuntimeError, TypeError, etc.) from _dispatch_resolved_destination
        left node_state as non-terminal OPEN in the audit trail.

        Fix: Widened except from MissingEdgeError to Exception.
        """
        factory = _make_factory()
        # Create a route_resolution_map that resolves to a fork destination
        # but provide NO token_manager — this triggers OrchestrationInvariantError
        # We also test with a completely custom RuntimeError via mock
        edge_map = {
            (NodeID("cg_1"), "path_a"): "edge_a",
            (NodeID("cg_1"), "path_b"): "edge_b",
            (NodeID("cg_1"), "continue"): "edge_cont",
        }
        route_map = {(NodeID("cg_1"), "true"): RouteDestination.fork()}
        executor = GateExecutor(
            factory.execution,
            _make_span_factory(),
            _make_step_resolver(),
            edge_map=edge_map,
            route_resolution_map=route_map,
        )
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition="True",
            routes={"true": "fork", "false": "error_sink"},
            fork_to=["path_a", "path_b"],
        )
        contract = _make_contract()
        token = _make_token(contract=contract)
        ctx = make_context()

        # This raises OrchestrationInvariantError (a subclass of Exception but
        # NOT MissingEdgeError). Before the fix, this would NOT be caught by
        # the except clause, leaving node_state OPEN.
        with pytest.raises(OrchestrationInvariantError):
            executor.execute_config_gate(
                config,
                "cg_1",
                token,
                ctx,
                token_manager=None,  # Triggers OrchestrationInvariantError in dispatch
                attempt_offset=0,
            )

        failed_kwargs = _single_complete_node_state_kwargs(factory, status=NodeStateStatus.FAILED)
        assert failed_kwargs["error"].phase == "gate_evaluation_routing"

    # --- Error routing edge cases (exd audit) ---

    def test_config_gate_none_result_raises_type_error(self) -> None:
        """Expression returning None raises TypeError — only bool/str are valid."""
        factory = _make_factory()
        executor = GateExecutor(factory.execution, _make_span_factory(), _make_step_resolver())
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition="None",
            routes={"true": "next_conn", "false": "error_sink"},
        )
        contract = _make_contract()
        token = _make_token(contract=contract)
        ctx = make_context()

        with pytest.raises(TypeError, match="NoneType"):
            executor.execute_config_gate(config, "cg_1", token, ctx, attempt_offset=0)

    def test_config_gate_int_result_raises_type_error(self) -> None:
        """Expression returning int at runtime raises TypeError — only bool/str are valid.

        Provably-numeric conditions (e.g. "40 + 2") are now rejected at config time by
        GateSettings (elspeth-f78e84f509), so this exercises a statically-ambiguous
        condition that only resolves to int once row values are known — confirming the
        executor's runtime type guard remains live.
        """
        factory = _make_factory()
        executor = GateExecutor(factory.execution, _make_span_factory(), _make_step_resolver())
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition="row['a'] + row['b']",
            routes={"true": "next_conn", "false": "error_sink"},
        )
        contract = _make_contract()
        token = _make_token(data={"value": "test", "a": 40, "b": 2}, contract=contract)
        ctx = make_context()

        with pytest.raises(TypeError, match="int"):
            executor.execute_config_gate(config, "cg_1", token, ctx, attempt_offset=0)

    def test_config_gate_type_error_redacts_untrusted_eval_result(self) -> None:
        """Unsupported route values must be described with bounded metadata."""
        from elspeth.contracts.errors import ExecutionError

        secret_value = "sk-or-v1-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" + ("y" * 100)
        factory = _make_factory()
        executor = GateExecutor(factory.execution, _make_span_factory(), _make_step_resolver())
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition="row['route_payload']",
            routes={"true": "next_conn", "false": "error_sink"},
        )
        token = _make_token(
            data={"value": "test", "route_payload": {"secret": secret_value}},
            contract=_make_contract(),
        )
        ctx = make_context()

        with pytest.raises(TypeError) as exc_info:
            executor.execute_config_gate(config, "cg_1", token, ctx, attempt_offset=0)

        message = str(exc_info.value)
        assert secret_value not in message
        assert "secret" not in message  # the row-derived KEY is not printed either
        assert message == (
            "Gate 'my_gate' expression returned unsupported route value (type=mappingproxy), "
            "expected bool or str. Expression: row['route_payload']"
        )

        failed_kwargs = _single_complete_node_state_kwargs(factory, status=NodeStateStatus.FAILED)
        error_obj = failed_kwargs.get("error")
        assert isinstance(error_obj, ExecutionError)
        assert secret_value not in error_obj.exception

    # --- context_after wiring ---

    def test_config_gate_records_context_after(self) -> None:
        """Gate evaluation metadata is passed as context_after for audit completeness."""
        factory = _make_factory()
        edge_map = {(NodeID("cg_1"), "true"): "edge_true"}
        route_map = {(NodeID("cg_1"), "true"): RouteDestination.processing_node(NodeID("next_node"))}
        executor = GateExecutor(
            factory.execution,
            _make_span_factory(),
            _make_step_resolver(),
            edge_map=edge_map,
            route_resolution_map=route_map,
        )
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition="True",
            routes={"true": "next_conn", "false": "error_sink"},
        )
        contract = _make_contract()
        token = _make_token(contract=contract)
        ctx = make_context()

        executor.execute_config_gate(config, "cg_1", token, ctx, attempt_offset=0)

        completed_kwargs = _single_complete_node_state_kwargs(factory, status=NodeStateStatus.COMPLETED)
        context_after = completed_kwargs.get("context_after")
        assert context_after is not None, "context_after should be set on success path"

        # Verify it's the right DTO with correct values
        from elspeth.contracts.node_state_context import GateEvaluationContext

        assert isinstance(context_after, GateEvaluationContext)
        assert context_after.condition == "True"
        assert context_after.result == "True"  # str(True) = "True"
        assert context_after.route_label == "true"  # bool True -> "true" label

    def test_config_gate_failure_does_not_set_context_after(self) -> None:
        """Failed gate evaluations should not set context_after."""
        factory = _make_factory()
        executor = GateExecutor(
            factory.execution,
            _make_span_factory(),
            _make_step_resolver(),
        )
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition="True",
            routes={"true": "next_conn", "false": "error_sink"},
        )
        contract = _make_contract()
        token = _make_token(contract=contract)
        ctx = make_context()

        # This will fail because no route_resolution_map for "true" label
        with pytest.raises(MissingEdgeError):
            executor.execute_config_gate(config, "cg_1", token, ctx, attempt_offset=0)

        failed_kwargs = _single_complete_node_state_kwargs(factory, status=NodeStateStatus.FAILED)
        assert "context_after" not in failed_kwargs

    # Note: test_post_dispatch_failure_marks_state_failed_via_guard was removed
    # because gate.py now reuses input_hash for output_hash (gates don't modify
    # data), eliminating the redundant stable_hash call. The failure scenario
    # this test covered (output_hash computation failure) can no longer occur.
    # NodeStateGuard behavior for actual gate failures is tested by:
    # - test_config_gate_missing_token_manager_records_failed_state
    # - test_config_gate_exception_records_failed_and_reraises
    # - test_config_gate_runtime_error_in_dispatch_records_failed_state


# =============================================================================
# TestDispatchResolvedDestinationPerVariant
# =============================================================================


class TestDispatchResolvedDestinationPerVariant:
    """Direct tests for GateExecutor._dispatch_resolved_destination.

    Each RouteDestinationKind variant is tested in isolation to kill mutants
    that change ``==`` to ``>=``/``<=``/``is`` on the StrEnum comparisons.
    The key assertion pattern: verify the CORRECT outcome AND verify the
    WRONG outcomes do NOT appear (action.kind, sink_name, next_node_id,
    child_tokens).
    """

    def _make_executor(
        self,
        edge_map: dict[tuple[NodeID, str], str] | None = None,
        route_map: dict[tuple[NodeID, str], RouteDestination] | None = None,
    ) -> GateExecutor:
        return GateExecutor(
            _make_factory().execution,
            _make_span_factory(),
            _make_step_resolver(),
            edge_map=edge_map,
            route_resolution_map=route_map,
        )

    def test_continue_destination_raises_invariant_error(self) -> None:
        """Resolved CONTINUE destinations are no longer part of the gate route model."""
        executor = self._make_executor()
        token = _make_token()
        ctx = make_context()

        with pytest.raises(OrchestrationInvariantError, match="Unsupported route destination kind"):
            executor._dispatch_resolved_destination(
                state_id="state_001",
                node_id="gate_1",
                route_label="true",
                destination=RouteDestination.continue_(),
                token=token,
                ctx=ctx,
                token_manager=None,
                reason=None,
                mode=RoutingMode.MOVE,
                fork_branches=None,
            )

    def test_fork_produces_fork_action_with_children(self) -> None:
        """FORK destination produces FORK_TO_PATHS kind with child tokens."""
        edge_map = {
            (NodeID("gate_1"), "path_a"): "edge_a",
            (NodeID("gate_1"), "path_b"): "edge_b",
        }
        executor = self._make_executor(edge_map=edge_map)
        token = _make_token()
        ctx = make_context()

        child_a = _make_token(token_id="child_a")
        child_b = _make_token(token_id="child_b")
        token_manager = _make_token_manager([child_a, child_b], "fg_001")

        outcome = executor._dispatch_resolved_destination(
            state_id="state_001",
            node_id="gate_1",
            route_label="true",
            destination=RouteDestination.fork(),
            token=token,
            ctx=ctx,
            token_manager=token_manager,
            reason=None,
            mode=RoutingMode.COPY,
            fork_branches=["path_a", "path_b"],
        )

        assert outcome.action.kind == RoutingKind.FORK_TO_PATHS
        assert outcome.action.kind != RoutingKind.CONTINUE  # type: ignore[comparison-overlap]
        assert outcome.action.kind != RoutingKind.ROUTE  # type: ignore[comparison-overlap]
        assert len(outcome.child_tokens) == 2
        assert outcome.sink_name is None
        assert outcome.next_node_id is None

    def test_fork_without_branches_raises_invariant_error(self) -> None:
        """FORK destination without fork_branches raises OrchestrationInvariantError."""
        executor = self._make_executor()
        token = _make_token()
        ctx = make_context()

        with pytest.raises(OrchestrationInvariantError, match="no fork branches"):
            executor._dispatch_resolved_destination(
                state_id="state_001",
                node_id="gate_1",
                route_label="true",
                destination=RouteDestination.fork(),
                token=token,
                ctx=ctx,
                token_manager=_make_token_manager([], "fg_unused"),
                reason=None,
                mode=RoutingMode.COPY,
                fork_branches=None,
            )

    def test_fork_without_token_manager_raises_invariant_error(self) -> None:
        """FORK destination without token_manager raises OrchestrationInvariantError."""
        executor = self._make_executor()
        token = _make_token()
        ctx = make_context()

        with pytest.raises(OrchestrationInvariantError, match="no TokenManager"):
            executor._dispatch_resolved_destination(
                state_id="state_001",
                node_id="gate_1",
                route_label="true",
                destination=RouteDestination.fork(),
                token=token,
                ctx=ctx,
                token_manager=None,
                reason=None,
                mode=RoutingMode.COPY,
                fork_branches=["path_a", "path_b"],
            )

    def test_sink_produces_sink_outcome(self) -> None:
        """SINK destination produces outcome with sink_name set."""
        edge_map = {(NodeID("gate_1"), "error"): "edge_err"}
        executor = self._make_executor(edge_map=edge_map)
        token = _make_token()
        ctx = make_context()

        outcome = executor._dispatch_resolved_destination(
            state_id="state_001",
            node_id="gate_1",
            route_label="error",
            destination=RouteDestination.sink(SinkName("error_output")),
            token=token,
            ctx=ctx,
            token_manager=None,
            reason=None,
            mode=RoutingMode.MOVE,
            fork_branches=None,
        )

        assert outcome.action.kind == RoutingKind.ROUTE
        assert outcome.sink_name == "error_output"
        assert outcome.next_node_id is None
        assert outcome.child_tokens == ()

    def test_processing_node_produces_next_node_outcome(self) -> None:
        """PROCESSING_NODE destination produces outcome with next_node_id set."""
        edge_map = {(NodeID("gate_1"), "high"): "edge_high"}
        executor = self._make_executor(edge_map=edge_map)
        token = _make_token()
        ctx = make_context()

        outcome = executor._dispatch_resolved_destination(
            state_id="state_001",
            node_id="gate_1",
            route_label="high",
            destination=RouteDestination.processing_node(NodeID("transform_2")),
            token=token,
            ctx=ctx,
            token_manager=None,
            reason=None,
            mode=RoutingMode.MOVE,
            fork_branches=None,
        )

        assert outcome.action.kind == RoutingKind.ROUTE
        assert outcome.next_node_id == NodeID("transform_2")
        assert outcome.sink_name is None
        assert outcome.child_tokens == ()

    def test_sink_and_processing_node_are_distinguishable(self) -> None:
        """SINK and PROCESSING_NODE produce mutually exclusive fields.

        This kills mutants where ``== SINK`` is changed to ``>= SINK`` or
        ``<= SINK`` — if the ordering bleeds, one branch would incorrectly
        match the other kind.
        """
        edge_map = {
            (NodeID("gate_1"), "route_a"): "edge_a",
            (NodeID("gate_1"), "route_b"): "edge_b",
        }
        executor = self._make_executor(edge_map=edge_map)
        token = _make_token()
        ctx = make_context()

        sink_outcome = executor._dispatch_resolved_destination(
            state_id="state_001",
            node_id="gate_1",
            route_label="route_a",
            destination=RouteDestination.sink(SinkName("my_sink")),
            token=token,
            ctx=ctx,
            token_manager=None,
            reason=None,
            mode=RoutingMode.MOVE,
            fork_branches=None,
        )

        node_outcome = executor._dispatch_resolved_destination(
            state_id="state_001",
            node_id="gate_1",
            route_label="route_b",
            destination=RouteDestination.processing_node(NodeID("next_node")),
            token=token,
            ctx=ctx,
            token_manager=None,
            reason=None,
            mode=RoutingMode.MOVE,
            fork_branches=None,
        )

        # SINK must have sink_name, not next_node_id
        assert sink_outcome.sink_name == "my_sink"
        assert sink_outcome.next_node_id is None

        # PROCESSING_NODE must have next_node_id, not sink_name
        assert node_outcome.next_node_id == NodeID("next_node")
        assert node_outcome.sink_name is None

    def test_fork_does_not_match_continue(self) -> None:
        """FORK must not fall into the CONTINUE branch.

        Kills ``== FORK`` → ``<= FORK`` mutants where StrEnum ordering
        might cause FORK to match the CONTINUE branch first.
        """
        edge_map = {
            (NodeID("gate_1"), "branch_x"): "edge_x",
            (NodeID("gate_1"), "branch_y"): "edge_y",
        }
        executor = self._make_executor(edge_map=edge_map)
        token = _make_token()
        ctx = make_context()

        child_x = _make_token(token_id="child_x")
        child_y = _make_token(token_id="child_y")
        tm = _make_token_manager([child_x, child_y], "fg_002")

        outcome = executor._dispatch_resolved_destination(
            state_id="state_001",
            node_id="gate_1",
            route_label="true",
            destination=RouteDestination.fork(),
            token=token,
            ctx=ctx,
            token_manager=tm,
            reason=None,
            mode=RoutingMode.COPY,
            fork_branches=["branch_x", "branch_y"],
        )

        # Must be FORK_TO_PATHS, definitely not CONTINUE
        assert outcome.action.kind == RoutingKind.FORK_TO_PATHS
        assert outcome.action.kind != RoutingKind.CONTINUE  # type: ignore[comparison-overlap]
        assert len(outcome.child_tokens) == 2
        # fork_token must have been called (not skipped by hitting CONTINUE branch)
        tm.fork_token.assert_called_once()


# =============================================================================
# Gap #6: GateExecutor ExecutionError field rename integration
# =============================================================================


class TestGateExecutorExecutionErrorFieldRename:
    """Integration test: ExecutionError 'exception_type' serializes as 'type' in audit trail.

    The ExecutionError dataclass has field ``exception_type`` but serializes as
    ``"type"`` in ``to_dict()`` for hash stability. This test verifies the full
    path: gate expression error -> ExecutionError -> execution repo -> serialized dict
    has the correct key name.
    """

    def test_gate_error_records_type_not_exception_type_in_audit(self) -> None:
        """Gate execution error should record 'type' (not 'exception_type') in the error dict.

        Exercises the full path: GateExecutor catches expression evaluation error,
        creates ExecutionError, passes it to factory.execution.complete_node_state(error=...),
        and the serialized form uses 'type' as the key name.
        """
        from elspeth.contracts.errors import ExecutionError
        from elspeth.core.expression_parser import ExpressionEvaluationError

        factory = _make_factory()
        executor = GateExecutor(factory.execution, _make_span_factory(), _make_step_resolver())
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition="row['nonexistent_field'] > 0",
            routes={"true": "next_conn", "false": "error_sink"},
        )
        contract = _make_contract()
        token = _make_token(contract=contract)
        ctx = make_context()

        # Trigger an expression evaluation error
        with pytest.raises(ExpressionEvaluationError):
            executor.execute_config_gate(config, "cg_1", token, ctx, attempt_offset=0)

        failed_kwargs = _single_complete_node_state_kwargs(factory, status=NodeStateStatus.FAILED)

        # Extract the error argument — it should be an ExecutionError
        error_obj = failed_kwargs.get("error")
        assert error_obj is not None, "error kwarg should be set on FAILED state"
        assert isinstance(error_obj, ExecutionError)

        # Verify the dataclass field is 'exception_type' (internal name)
        assert error_obj.exception_type == "ExpressionEvaluationError"

        # Verify to_dict() serializes as 'type' (audit trail key)
        error_dict = error_obj.to_dict()
        assert "type" in error_dict, "Serialized error should use 'type' key, not 'exception_type'"
        assert "exception_type" not in error_dict, "Serialized error should NOT contain 'exception_type' key"
        assert error_dict["type"] == "ExpressionEvaluationError"

    def test_gate_route_label_error_serializes_type_correctly(self) -> None:
        """Route label mismatch error also serializes 'exception_type' as 'type'.

        This tests the second error path in execute_config_gate: when the route
        label is not in the routes config.
        """
        from elspeth.contracts.errors import ExecutionError

        factory = _make_factory()
        executor = GateExecutor(factory.execution, _make_span_factory(), _make_step_resolver())
        config = GateSettings(
            name="my_gate",
            input="in_conn",
            condition="'unknown_route'",  # Evaluates to str not in routes
            routes={"true": "next_conn", "false": "error_sink"},
        )
        contract = _make_contract()
        token = _make_token(contract=contract)
        ctx = make_context()

        # "unknown_route" is not in routes, so this raises ValueError
        with pytest.raises(ValueError, match=r"unconfigured route label \(type=str, length=13\).*Expression: 'unknown_route'"):
            executor.execute_config_gate(config, "cg_1", token, ctx, attempt_offset=0)

        failed_kwargs = _single_complete_node_state_kwargs(factory, status=NodeStateStatus.FAILED)

        error_obj = failed_kwargs.get("error")
        assert isinstance(error_obj, ExecutionError)

        # Verify serialization: 'type' key with value 'ValueError'
        error_dict = error_obj.to_dict()
        assert error_dict["type"] == "ValueError"
        assert "exception_type" not in error_dict
