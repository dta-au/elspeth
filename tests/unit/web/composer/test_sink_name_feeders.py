"""``set_output``'s ``sink_name`` text names every feeder the validator counts.

The description said the sink name "MUST equal some upstream's on_success
value", but an output fed only by a step's ``on_error`` is valid and was
built that way in recorded sessions. A literal reader could refuse a valid
error output, so the sentence names every kind of feeder.
"""

from __future__ import annotations

from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools import get_tool_definitions


def _sink_name_description() -> str:
    [definition] = [tool for tool in get_tool_definitions() if tool["name"] == "set_output"]
    return definition["parameters"]["properties"]["sink_name"]["description"]


def test_sink_name_description_does_not_require_an_on_success_feeder() -> None:
    assert "MUST equal some upstream's on_success" not in _sink_name_description()


def test_sink_name_description_names_every_feeder_kind() -> None:
    text = _sink_name_description()
    for feeder in ("on_success", "on_error", "routes", "fork_to", "on_validation_failure", "on_write_failure"):
        assert f"`{feeder}`" in text, feeder


def test_an_output_fed_only_by_on_error_receives_data() -> None:
    """The validator fact the sentence states: an ``on_error`` feeder alone is enough."""
    state = CompositionState(
        source=SourceSpec(
            plugin="csv", on_success="rows", options={"path": "in.csv", "schema": {"mode": "observed"}}, on_validation_failure="discard"
        ),
        nodes=(
            NodeSpec(
                id="check",
                node_type="transform",
                plugin="passthrough",
                input="rows",
                on_success="results",
                on_error="rejects",
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
        outputs=tuple(
            OutputSpec(
                name=name, plugin="json", options={"path": f"{name}.jsonl", "schema": {"mode": "observed"}}, on_write_failure="discard"
            )
            for name in ("results", "rejects", "orphan")
        ),
        metadata=PipelineMetadata(),
        version=1,
    )

    unfed = {warning.component for warning in state.validate().warnings if "never receive data" in warning.message}

    # "orphan" has no feeder at all: the control that the warning fires.
    assert unfed == {"output:orphan"}
