"""TC-007: recovery-control argument guidance matches handler admission.

The recovery tools tell the model that whitespace-only ``message`` and
``park_id`` values are rejected. These tests pin that the parser itself
refuses them (not only the downstream intent invariant), for every action and
both tool-contract dialects, and that the advertised descriptions say so.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web.composer.recovery_controls import parse_recovery_tool_call, recovery_tool_definitions

_BLANKS = [
    pytest.param(" ", id="space"),
    pytest.param("   ", id="spaces"),
    pytest.param("\t\n ", id="mixed-whitespace"),
]
_MESSAGE = "Which report should we create?"


@pytest.mark.parametrize("blank", _BLANKS)
@pytest.mark.parametrize("action", ["park", "reset_to_turn_start", "reset_to_safe_checkpoint"])
def test_handoff_rejects_whitespace_only_message_in_the_handler(action: str, blank: str) -> None:
    with pytest.raises(ValueError, match=r"^Recovery message must not be blank$"):
        parse_recovery_tool_call("handoff_to_user", {"action": action, "message": blank}, "call-1")


@pytest.mark.parametrize("blank", _BLANKS)
@pytest.mark.parametrize("field", ["park_id", "message"])
def test_restore_rejects_whitespace_only_selector_or_message_in_the_handler(field: str, blank: str) -> None:
    arguments: dict[str, Any] = {"park_id": "park-1", "message": _MESSAGE}
    arguments[field] = blank
    with pytest.raises(ValueError, match=r"^Recovery message and parked identity must not be blank$"):
        parse_recovery_tool_call("restore_parked_graph", arguments, "call-1")


def test_padded_but_nonblank_values_are_admitted_exactly() -> None:
    """Rejection is for whitespace-only values; the parser does not trim real ones."""
    intent = parse_recovery_tool_call("restore_parked_graph", {"park_id": " park-1 ", "message": " hi "}, "call-1")
    assert intent is not None
    assert (intent.park_id, intent.message) == (" park-1 ", " hi ")


def _properties_by_tool(dialect: ToolContractDialect) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for definition in recovery_tool_definitions(dialect):
        function = definition["function"]
        assert isinstance(function, dict)
        parameters = function["parameters"]
        assert isinstance(parameters, dict)
        result[function["name"]] = parameters["properties"]
    return result


@pytest.mark.parametrize("dialect", list(ToolContractDialect))
def test_recovery_descriptions_state_bounds_and_whitespace_rejection(dialect: ToolContractDialect) -> None:
    properties = _properties_by_tool(dialect)
    message = "Message to the user (1 to 16000 characters; whitespace-only values are rejected)."
    assert properties["handoff_to_user"]["message"]["description"] == message
    assert properties["restore_parked_graph"]["message"]["description"] == message
    assert properties["restore_parked_graph"]["park_id"]["description"] == (
        "Exact advertised parked graph identity (1 to 128 characters; whitespace-only values are rejected)."
    )
