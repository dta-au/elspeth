"""Independent recovery protocol checks using scripted, offline providers."""

from typing import Any
from unittest.mock import patch

import pytest
from jsonschema import Draft202012Validator

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web.composer import service as service_module
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.recovery_controls import recovery_tool_definitions
from tests.unit.web.composer.test_service import (
    _composer_service_with_session,
    _empty_state,
    _make_llm_response,
    _make_settings,
    _mock_catalog,
)

_CONTROL_NAMES = {"handoff_to_user", "restore_parked_graph"}
_QUESTION = "The output instructions disagree. Which report should we create?"
_CONTROLS = [
    pytest.param("handoff_to_user", {"action": "park", "message": _QUESTION}, "park", id="park"),
    pytest.param(
        "handoff_to_user",
        {"action": "reset_to_turn_start", "message": _QUESTION},
        "reset_to_turn_start",
        id="turn-start",
    ),
    pytest.param(
        "handoff_to_user",
        {"action": "reset_to_safe_checkpoint", "message": _QUESTION},
        "reset_to_safe_checkpoint",
        id="safe-checkpoint",
    ),
    pytest.param(
        "restore_parked_graph",
        {"park_id": "previous-draft", "message": _QUESTION},
        "restore_parked_graph",
        id="restore",
    ),
]


@pytest.mark.parametrize("dialect", list(ToolContractDialect))
def test_recovery_palettes_preserve_explicit_target_choices(dialect: ToolContractDialect) -> None:
    tools = recovery_tool_definitions(dialect)
    definitions = {tool["function"]["name"]: tool["function"] for tool in tools}
    assert set(definitions) == _CONTROL_NAMES
    handoff = definitions["handoff_to_user"]
    assert set(handoff["parameters"]["properties"]["action"]["enum"]) == {
        "park",
        "reset_to_turn_start",
        "reset_to_safe_checkpoint",
    }
    for definition in definitions.values():
        Draft202012Validator.check_schema(definition["parameters"])
        assert definition["parameters"]["additionalProperties"] is False
        assert set(definition["parameters"]["required"]) == set(definition["parameters"]["properties"])
        if dialect is ToolContractDialect.OPENAI_STRICT:
            assert definition["strict"] is True
        else:
            assert "strict" not in definition


@pytest.mark.parametrize("name,arguments,action", _CONTROLS)
def test_primary_control_exits_before_dispatch_repair_and_advisor(name: str, arguments: dict[str, str], action: str) -> None:
    """A sole recovery control settles as a terminal intent from the shared owner.

    ``ComposerServiceImpl._terminal_recovery_result`` is the owner that turns a
    primary completion's control into the terminal result before any dispatch,
    repair or Advisor owner can run; the primary palette is
    ``composer_loop_tool_definitions``.
    """
    service, _session_id = _composer_service_with_session(_mock_catalog(), _make_settings())
    state = _empty_state()
    advertised = {tool["function"]["name"] for tool in service_module.composer_loop_tool_definitions(service._planner_dialect)}
    assert advertised >= _CONTROL_NAMES, "Provider palette omitted recovery controls"
    recorder = BufferingRecorder()
    completion = _make_llm_response(tool_calls=[{"id": "handoff-1", "name": name, "arguments": arguments}])
    result = service._terminal_recovery_result(completion, state, recorder)
    assert result is not None, "Recovery reached ordinary dispatch"
    assert result.graph_recovery_intent is not None
    assert result.graph_recovery_intent.action == action
    assert result.graph_recovery_intent.message == _QUESTION
    assert result.graph_recovery_intent.tool_call_id == "handoff-1"
    assert result.state is state
    assert result.runtime_preflight is None
    assert result.tool_invocations == ()


@pytest.mark.parametrize("defect", ["hidden_control", "disabled_terminal"])
def test_primary_recovery_probe_detects_protocol_regressions(defect: str) -> None:
    """Control the actual palette/termination probe with in-memory mutations."""
    if defect == "hidden_control":
        original = service_module.composer_loop_tool_definitions

        def without_handoff(dialect: ToolContractDialect) -> list[dict[str, Any]]:
            return [tool for tool in original(dialect) if tool["function"]["name"] != "handoff_to_user"]

        mutation = patch.object(service_module, "composer_loop_tool_definitions", side_effect=without_handoff)
        expected_message = "Provider palette omitted recovery controls"
    else:
        mutation = patch.object(service_module, "parse_recovery_tool_batch", return_value=None)
        expected_message = "Recovery reached ordinary dispatch"
    with mutation, pytest.raises(AssertionError, match=expected_message):
        test_primary_control_exits_before_dispatch_repair_and_advisor("handoff_to_user", {"action": "park", "message": _QUESTION}, "park")


@pytest.mark.parametrize("control_first", [True, False])
def test_mixed_primary_control_batch_executes_neither_call(control_first: bool) -> None:
    """A control mixed with any other call is refused before either applies."""
    service, _session_id = _composer_service_with_session(_mock_catalog(), _make_settings())
    calls = [
        {"id": "stop", "name": "handoff_to_user", "arguments": {"action": "park", "message": _QUESTION}},
        {"id": "mutate", "name": "set_metadata", "arguments": {"patch": {"name": "must-not-apply"}}},
    ]
    if not control_first:
        calls.reverse()
    state = _empty_state()
    recorder = BufferingRecorder()
    with pytest.raises(ValueError, match="sole tool call") as refused:
        service._terminal_recovery_result(_make_llm_response(tool_calls=calls), state, recorder)
    assert refused.value.__dict__["llm_calls"] == ()
    assert recorder.invocations == ()
    assert state.metadata.name != "must-not-apply"
