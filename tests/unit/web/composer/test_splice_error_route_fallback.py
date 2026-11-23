"""``splice_transform``'s error-route rule is stated in the skill, and its refusal names the fallback.

The tool refuses when either endpoint has an ``on_error`` other than none or
``discard``. The skill's quick-choice row did not say so, so the model kept
trying the splice and being refused; the refusal named the reason but not
the two-step route that works.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from elspeth.web.composer.state import OutputSpec
from elspeth.web.composer.tools._dispatch import execute_tool
from tests.unit.web.composer.test_splice_transform_tool import _arguments, _context, _state

_SKILL = Path(__file__).parents[4] / "src/elspeth/web/composer/skills/pipeline_composer.md"


def _insertion_row() -> str:
    [row] = [line for line in _SKILL.read_text(encoding="utf-8").splitlines() if line.startswith("| Perform a one-transform insertion")]
    return row


def test_skill_insertion_row_states_the_error_route_rule() -> None:
    assert "Neither endpoint may have an `on_error` other than none or `discard`." in _insertion_row()


@pytest.mark.parametrize("endpoint", ["before", "after"])
def test_refusal_names_the_two_step_fallback(endpoint: str) -> None:
    base = _state()
    state = replace(
        base,
        nodes=tuple(replace(node, on_error="rejects") if node.id == endpoint else node for node in base.nodes),
        outputs=(*base.outputs, replace(base.outputs[0], name="rejects", options={**base.outputs[0].options, "path": "rejects.jsonl"})),
    )
    assert all(type(output) is OutputSpec for output in state.outputs)
    context = _context()

    result = execute_tool("splice_transform", _arguments(), state, context.catalog, plugin_snapshot=context.plugin_snapshot)

    assert result.success is False
    [error] = [entry for entry in result.validation.errors if entry.error_code == "splice_topology_unsupported"]
    assert "routed error branches" in error.message
    assert "add the new step with upsert_node" in error.message
    assert "upsert_node with routing_only=true" in error.message
    assert "{" not in error.message  # words, not a ready-made call


def test_eligible_endpoints_still_splice() -> None:
    """Control: both endpoints discard their errors, so the splice is accepted."""
    context = _context()

    result = execute_tool("splice_transform", _arguments(), _state(), context.catalog, plugin_snapshot=context.plugin_snapshot)

    assert result.success is True, [entry.message for entry in result.validation.errors]
