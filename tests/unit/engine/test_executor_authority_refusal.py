"""Repository authority refusals must survive executor cleanup unchanged."""

from dataclasses import replace

import pytest

from elspeth.contracts.errors import RunLeadershipLostError, RunMembershipLostError
from elspeth.engine.executors import TransformExecutor
from elspeth.engine.executors.sink import SinkExecutor
from tests.fixtures.factories import make_context
from tests.unit.engine.conftest import make_test_step_resolver
from tests.unit.engine.test_executors import _make_factory, _make_span_factory, _make_token, _make_transform


@pytest.fixture(params=["leader", "member"])
def refusal(request: pytest.FixtureRequest) -> RunLeadershipLostError | RunMembershipLostError:
    if request.param == "leader":
        return RunLeadershipLostError(run_id="test-run", worker_id="mock-worker", leader_epoch=1, verb="audit-write")
    return RunMembershipLostError(run_id="test-run", worker_id="mock-worker", verb="audit-write")


def test_transform_invocation_refusal_does_not_record_failed(refusal: Exception) -> None:
    factory = _make_factory()
    executor = TransformExecutor(factory.execution, _make_span_factory(), make_test_step_resolver(), data_flow=factory.data_flow)
    transform = _make_transform()
    transform.process.side_effect = refusal

    with pytest.raises(type(refusal)) as propagated:
        executor.execute_transform(transform, _make_token(), make_context())

    assert propagated.value is refusal
    factory.execution.complete_node_state.assert_not_called()


def test_sink_partial_open_refusal_does_not_cleanup_prior_state(refusal: Exception) -> None:
    factory = _make_factory()
    ctx = make_context()
    executor = SinkExecutor(
        factory.execution, factory.data_flow, _make_span_factory(), ctx.run_id, coordination_token=ctx.require_coordination_token()
    )
    first = replace(_make_token(), resume_attempt_offset=1, resume_checkpoint_id="checkpoint-1")
    second = _make_token(token_id="tok_2")
    factory.execution.begin_node_state.side_effect = [factory.execution.begin_node_state.return_value, refusal]

    with pytest.raises(type(refusal)) as propagated:
        executor._open_primary_states(
            tokens=[first, second],
            rows=[first.row_data.to_dict(), second.row_data.to_dict()],
            sink_node_id="sink",
            step_in_pipeline=1,
            ctx=ctx,
        )

    assert propagated.value is refusal
    factory.execution.complete_node_state.assert_not_called()


def test_sink_cleanup_refusal_is_not_swallowed(refusal: Exception) -> None:
    factory = _make_factory()
    ctx = make_context()
    executor = SinkExecutor(
        factory.execution, factory.data_flow, _make_span_factory(), ctx.run_id, coordination_token=ctx.require_coordination_token()
    )
    factory.execution.complete_node_state.side_effect = refusal

    with pytest.raises(type(refusal)) as propagated:
        executor._best_effort_cleanup(
            [(_make_token(), factory.execution.begin_node_state.return_value)],
            ValueError("plugin failure"),
            "sink_write",
        )

    assert propagated.value is refusal
