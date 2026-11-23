"""A successful mutation that cannot echo ``applied_component`` says so in one line.

The echo is built from the exact ``set_pipeline`` arguments, which do not
exist for a graph whose ``set_source_from_blob`` source is named (or sits
beside other sources). Every mutation on such a graph then came back without
the echo and without saying why. Mitigation only: one short factual
``post_call_hints`` line; the per-component echo is later work.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools import ToolResult
from tests.unit.web.composer.test_applied_component_echo import _run

_MARK = "applied_component"

_SKILL = Path(__file__).parents[4] / "src/elspeth/web/composer/skills/pipeline_composer.md"


def _node(description: str | None = None) -> NodeSpec:
    return NodeSpec(
        id="classify",
        node_type="transform",
        plugin="passthrough",
        input="rows",
        on_success="results",
        on_error="discard",
        options={"schema": {"mode": "observed"}},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
        description=description,
    )


def _state(*, named_upload: bool) -> CompositionState:
    if named_upload:
        sources = {
            "submissions": SourceSpec(
                plugin="csv",
                on_success="rows",
                options={
                    "blob_ref": "2e9e41eb-e34d-4918-b334-3c1e9ee0f8ff",
                    "path": "/data/blobs/submissions.csv",
                    "schema": {"mode": "observed"},
                },
                on_validation_failure="discard",
            )
        }
    else:
        sources = {
            "source": SourceSpec(
                plugin="csv", on_success="rows", options={"path": "in.csv", "schema": {"mode": "observed"}}, on_validation_failure="discard"
            )
        }
    return CompositionState(
        sources=sources,
        nodes=(_node(),),
        edges=(),
        outputs=(
            OutputSpec(
                name="results", plugin="json", options={"path": "out.jsonl", "schema": {"mode": "observed"}}, on_write_failure="discard"
            ),
            OutputSpec(
                name="spare", plugin="json", options={"path": "spare.jsonl", "schema": {"mode": "observed"}}, on_write_failure="discard"
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


def _resend_node() -> dict[str, Any]:
    return {
        "id": "classify",
        "node_type": "transform",
        "plugin": "passthrough",
        "input": "rows",
        "on_success": "results",
        "on_error": "discard",
        "options": {"schema": {"mode": "observed"}},
        "description": "Pass each row through unchanged.",
    }


def _notices(result: ToolResult) -> list[str]:
    return [hint for hint in result.post_call_hints if _MARK in hint]


def test_mutation_on_a_graph_without_an_exact_form_says_the_echo_is_omitted() -> None:
    result = _run("upsert_node", _resend_node(), _state(named_upload=True))

    assert result.success is True, [entry.message for entry in result.validation.errors]
    assert result.applied_component is None
    assert len(_notices(result)) == 1, result.post_call_hints
    notice = _notices(result)[0]
    assert "omitted for this graph" in notice
    assert "get_pipeline_state" in notice
    assert len(notice) <= 160


def test_mutation_on_a_single_source_graph_echoes_and_adds_no_line() -> None:
    """Control: where the echo exists, nothing changes."""
    result = _run("upsert_node", _resend_node(), _state(named_upload=False))

    assert result.success is True, [entry.message for entry in result.validation.errors]
    assert result.applied_component is not None
    assert _notices(result) == []


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        pytest.param("remove_output", {"sink_name": "spare"}, id="removal"),
        pytest.param(
            "upsert_node",
            {"id": "classify", "node_type": "transform", "input": "rows", "on_error": "discard", "routing_only": True},
            id="routing-only",
        ),
    ],
)
def test_mutations_that_never_echo_add_no_line(tool: str, arguments: dict[str, Any]) -> None:
    """Controls: a removal and a routing-only edit carry no echo by design, so they get no line either."""
    result = _run(tool, arguments, _state(named_upload=True))

    assert result.success is True, [entry.message for entry in result.validation.errors]
    assert result.applied_component is None
    assert _notices(result) == []


def test_skill_no_longer_promises_the_echo_unconditionally() -> None:
    text = " ".join(_SKILL.read_text(encoding="utf-8").split())
    assert "A successful incremental mutation carries `applied_component`" not in text
    assert "says the echo is omitted for this graph" in text


def test_described_node_state_is_untouched_by_the_notice() -> None:
    """The line is a result fact only; the stored graph is what the mutation wrote."""
    result = _run("upsert_node", _resend_node(), _state(named_upload=True))

    assert result.updated_state.nodes == (replace(_node(), description="Pass each row through unchanged."),)
