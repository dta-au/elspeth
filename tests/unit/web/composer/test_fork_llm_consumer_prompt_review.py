"""Re-pointing an LLM step into a fork branch: does its approved prompt review stand?

On the enclave the consumers sharing a connection are LLM steps. The fork the
skill teaches re-sends each step whole (as the model read it) with only its
``input`` changed. This pins what that does to the step's prompt review: the
review rows, the approved prompt artifact hash, and whether execution would
report a pending interpretation review afterwards.

Measured at the tool (the same ``upsert_node`` handler the accept route
dispatches for an approved proposal), with the reviewed-state fixtures of
``test_prompt_patch_reviews``.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec
from elspeth.web.interpretation_state import InterpretationReviewPending, materialize_state_for_execution
from tests.unit.web.composer.test_prompt_patch_reviews import _approved_state
from tests.unit.web.composer.test_tools import _mock_catalog, execute_tool

_LLM = "code_themes"


def _with_second_reader(state: CompositionState) -> CompositionState:
    return state.with_node(
        NodeSpec(
            id="audit",
            node_type="transform",
            plugin="passthrough",
            input="source_out",
            on_success="audit_out",
            on_error="discard",
            options={"schema": {"mode": "observed"}},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
    ).with_output(
        OutputSpec(
            name="audit_out", plugin="csv", options={"path": "/data/audit.csv", "schema": {"mode": "observed"}}, on_write_failure="discard"
        )
    )


def _review(state: CompositionState) -> dict[str, Any]:
    options = deep_thaw(next(node for node in state.nodes if node.id == _LLM).options)
    return {
        "statuses": {row["kind"]: row["status"] for row in options["interpretation_requirements"]},
        "approved_hash": options.get("approved_prompt_artifact_hash"),
        "execution_pending": isinstance(materialize_state_for_execution(state), InterpretationReviewPending),
    }


@pytest.mark.parametrize("structured", [False, True], ids=["inline-prompt", "prompt-parts"])
def test_re_pointing_an_llm_step_into_a_fork_branch_keeps_its_prompt_review(structured: bool) -> None:
    state = _with_second_reader(_approved_state(structured=structured))
    before = _review(state)
    assert before["execution_pending"] is False
    assert before["approved_hash"] is not None

    nodes = {
        node["id"]: dict(node)
        for node in execute_tool("get_pipeline_state", {"component": "set_pipeline_arguments"}, state, _mock_catalog()).to_dict()["data"][
            "nodes"
        ]
    }
    calls = [
        {
            "id": "fan_out",
            "node_type": "gate",
            "input": "source_out",
            "condition": "True",
            "routes": {"true": "fork", "false": "fork"},
            "fork_to": ["rows_for_llm", "rows_for_audit"],
        },
        {**nodes["audit"], "input": "rows_for_audit"},
        {**nodes[_LLM], "input": "rows_for_llm"},
    ]
    for arguments in calls:
        result = execute_tool("upsert_node", arguments, state, _mock_catalog())
        assert result.success is True, result.to_dict()
        state = result.updated_state

    assert next(node for node in state.nodes if node.id == _LLM).input == "rows_for_llm"
    assert _review(state) == before
    assert not any(entry.error_code == "duplicate_connection_consumer" for entry in state.validate().errors)
