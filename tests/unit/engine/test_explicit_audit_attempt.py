"""Scheduler attempts must never silently fall back to the initial audit attempt."""

from collections.abc import Callable
from inspect import Parameter, signature

import pytest

from elspeth.engine.executors.gate import GateExecutor
from elspeth.engine.executors.transform import TransformExecutor
from elspeth.engine.processor import RowProcessor
from elspeth.engine.scheduler_drain import SchedulerDrainHost
from elspeth.engine.token_traversal import TokenTraversalEngine


@pytest.mark.parametrize(
    ("boundary", "parameter"),
    [
        (GateExecutor.execute_config_gate, "attempt_offset"),
        (TransformExecutor.execute_transform, "attempt"),
        (TokenTraversalEngine.handle_gate_node, "attempt_offset"),
        (TokenTraversalEngine.handle_transform_node, "attempt_offset"),
        (TokenTraversalEngine.process_single_token, "attempt_offset"),
        (RowProcessor._execute_transform_with_retry, "attempt_offset"),
        (RowProcessor._process_single_token, "attempt_offset"),
        (RowProcessor._handle_transform_node, "attempt_offset"),
        (SchedulerDrainHost._process_single_token, "attempt_offset"),
    ],
)
def test_audit_attempt_requires_an_explicit_keyword(boundary: Callable[..., object], parameter: str) -> None:
    """An omitted or positional attempt cannot admit a first-attempt audit write."""
    contract = signature(boundary)
    attempt = contract.parameters[parameter]
    assert attempt.default is Parameter.empty
    assert attempt.kind is Parameter.KEYWORD_ONLY

    required = {name: object() for name, field in contract.parameters.items() if field.default is Parameter.empty and name != parameter}
    with pytest.raises(TypeError, match=parameter):
        contract.bind(**required)
    contract.bind(**required, **{parameter: 0})
    contract.bind(**required, **{parameter: 3})
