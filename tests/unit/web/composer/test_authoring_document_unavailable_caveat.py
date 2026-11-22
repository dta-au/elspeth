"""The authoring document's refusal case is stated where the model reads about it.

``get_pipeline_state(component='set_pipeline_arguments')`` is refused when a
source bound with ``set_source_from_blob`` is not the pipeline's only source
named ``source``. The ``component`` description advertised the document without
that caveat, and the refusal did not say what to read instead, so the model
spent a round on a read that could not succeed.
"""

from __future__ import annotations

from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools import get_tool_definitions
from elspeth.web.composer.tools.sessions import _execute_get_pipeline_state
from tests.unit.web.composer.test_authoring_reconciliation import _trained_context


def _component_description() -> str:
    [definition] = [tool for tool in get_tool_definitions() if tool["name"] == "get_pipeline_state"]
    return definition["parameters"]["properties"]["component"]["description"]


def _named_upload_state() -> CompositionState:
    """One uploaded-file source named ``submissions``: the recorded graph shape."""
    return CompositionState(
        sources={
            "submissions": SourceSpec(
                plugin="csv",
                on_success="rows",
                options={"blob_ref": "2e9e41eb-e34d-4918-b334-3c1e9ee0f8ff", "path": "/data/blobs/submissions.csv"},
                on_validation_failure="discard",
            )
        },
        nodes=(
            NodeSpec(
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
            ),
        ),
        edges=(),
        outputs=(
            OutputSpec(
                name="results", plugin="json", options={"path": "out.jsonl", "schema": {"mode": "observed"}}, on_write_failure="discard"
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


def test_component_description_states_when_the_authoring_document_is_refused() -> None:
    text = _component_description()
    assert "a source bound with set_source_from_blob is named other than 'source' or sits beside other sources" in text
    assert "read the node or output by its id instead" in text


def test_refusal_says_what_to_read_instead() -> None:
    result = _execute_get_pipeline_state({"component": "set_pipeline_arguments"}, _named_upload_state(), _trained_context())

    assert result.success is False
    [error] = [entry for entry in result.validation.errors if entry.error_code == "round_trip_unavailable"]
    assert "Read the node or output you need by its id instead." in error.message


def test_the_suggested_read_works_on_the_same_graph() -> None:
    """Control: the read the refusal points to succeeds where the document is refused."""
    state = _named_upload_state()

    node = _execute_get_pipeline_state({"component": "classify"}, state, _trained_context())
    output = _execute_get_pipeline_state({"component": "results"}, state, _trained_context())

    assert node.success is True
    assert output.success is True
