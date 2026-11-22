"""patch_output_options must not claim an output's write-failure choice is missing.

In the composer the write-failure choice is a field of the output that sits
beside ``options`` (``OutputSpec.on_write_failure``), never inside it. The
JSON sink's post-call hint checks the config snapshot for that key, so a
snapshot built from ``options`` alone made the hint fire after every
successful patch of a JSON output, although the stored output had a choice.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from elspeth.plugins.sinks.json_sink import JSONSink
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata
from elspeth.web.composer.tools import execute_tool, outputs
from elspeth.web.composer.tools._common import ToolContext, ToolResult
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

_SINK = "battery_out"


def _missing_choice_hints() -> tuple[str, ...]:
    """The hints the JSON sink emits when the choice is absent from the snapshot.

    Derived from the plugin rather than copied, so the test does not pin the
    wording: ``format`` is absent too, so only the missing-choice hint remains.
    """
    hints = JSONSink.get_post_call_hints(tool_name="patch_output_options", config_snapshot={})
    assert len(hints) == 1
    return hints


def _state(*, output_format: str) -> CompositionState:
    output = OutputSpec(
        name=_SINK,
        plugin="json",
        options={
            "path": "outputs/battery_out.jsonl",
            "format": output_format,
            "schema": {"mode": "observed"},
            "mode": "write",
            "collision_policy": "auto_increment",
        },
        on_write_failure="discard",
    )
    return CompositionState(
        source=None,
        nodes=(),
        edges=(),
        outputs=(output,),
        metadata=PipelineMetadata(),
        version=44,
    )


def _context(tmp_path: Path) -> ToolContext:
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    return ToolContext(
        catalog=PolicyCatalogView.for_trained_operator(catalog, snapshot),
        plugin_snapshot=snapshot,
        data_dir=str(tmp_path),
        session_id=str(uuid4()),
    )


def _patch(surface: str, patch: dict[str, object], state: CompositionState, context: ToolContext) -> ToolResult:
    arguments = {"sink_name": _SINK, "patch": patch}
    if surface == "handler":
        return outputs._handle_patch_output_options(arguments, state, context)
    return execute_tool(
        "patch_output_options",
        arguments,
        state,
        context.catalog,
        plugin_snapshot=context.plugin_snapshot,
        data_dir=context.data_dir,
        session_id=context.session_id,
    )


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
def test_patch_of_json_output_with_a_stored_choice_gets_no_missing_choice_hint(tmp_path: Path, surface: str) -> None:
    # Live shape: a path patch on a JSON Lines output whose stored choice is "discard".
    state = _state(output_format="jsonl")
    result = _patch(surface, {"path": "outputs/battery_errors.jsonl"}, state, _context(tmp_path))

    assert result.success
    saved = next(o for o in result.updated_state.outputs if o.name == _SINK)
    assert saved.on_write_failure == "discard"
    for hint in _missing_choice_hints():
        assert hint not in result.post_call_hints
    assert "post_call_hints" not in result.to_dict()


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
def test_patch_of_json_output_still_gets_hints_that_are_true(tmp_path: Path, surface: str) -> None:
    # Control: a hint whose condition holds on the saved output still reaches the model.
    state = _state(output_format="json")
    result = _patch(surface, {"format": "json"}, state, _context(tmp_path))

    assert result.success
    expected = JSONSink.get_post_call_hints(
        tool_name="patch_output_options",
        config_snapshot={"format": "json", "on_write_failure": "discard"},
    )
    assert len(expected) == 1
    assert result.post_call_hints == expected


def test_json_sink_still_reports_a_choice_absent_from_its_snapshot() -> None:
    # Instrument control: the plugin-side hint is unchanged, so the negative
    # assertions above would see it if the snapshot dropped the choice again.
    assert _missing_choice_hints()
    with_choice = JSONSink.get_post_call_hints(tool_name="patch_output_options", config_snapshot={"on_write_failure": "discard"})
    assert with_choice == ()
