"""An edit to an existing prompt reaches the compose loop, whose tools carry change_substring.

The bounded planner runs only on a structurally empty state (the service
dispatch is AST-pinned in test_compose_service_structure.py). An existing
prompt lives in a node, so its edits take the compose loop; that surface must
offer the compact edit under every tool-contract dialect.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web.composer.pipeline_planner import planner_tool_definitions
from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions
from elspeth.web.composer.service import _state_is_structurally_empty
from tests.unit.web.composer.test_compose_loop_llm_audit import _empty_state
from tests.unit.web.composer.test_prompt_patch_reviews import _approved_state


def _names(tools: list[dict[str, Any]]) -> set[str]:
    return {tool["function"]["name"] for tool in tools}


def test_a_state_holding_only_a_prompt_node_is_not_planner_eligible() -> None:
    prompt_node = _approved_state().nodes[0]
    assert type(prompt_node.options["prompt_template"]) is str

    node_only = _empty_state().with_node(prompt_node)

    assert _state_is_structurally_empty(_empty_state()) is True
    assert _state_is_structurally_empty(node_only) is False


@pytest.mark.parametrize("dialect", list(ToolContractDialect))
def test_compose_loop_offers_the_compact_prompt_edit(dialect: ToolContractDialect) -> None:
    loop = _names(composer_loop_tool_definitions(dialect))
    planner = _names(planner_tool_definitions(dialect=dialect))

    assert {"change_substring", "inspect_prompt_targets"} <= loop
    assert "patch_node_options" in loop
    # The planner is the empty-pipeline surface; it never sees an existing prompt.
    assert "change_substring" not in planner
