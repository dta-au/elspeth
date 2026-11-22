"""Post-call hints after a patch speak only about the keys the patch touched.

``patch_source_options`` and ``patch_output_options`` resolve plugin hints
after a successful patch. A hint about a key the patch did not touch repeats
advice about a choice that was already saved (for example the fixed-schema
reminder after a ``skip_rows`` patch), so the snapshot holds only the patched
keys' saved values. The output snapshot also carries the output's own
write-failure choice, which lives beside ``options``.
"""

from __future__ import annotations

from typing import Literal

import pytest

from elspeth.plugins.sinks.json_sink import JSONSink
from elspeth.plugins.sources.csv_source import CSVSource
from elspeth.plugins.sources.json_source import JSONSource
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools import ToolContext, ToolResult, execute_tool
from elspeth.web.composer.tools.outputs import _handle_patch_output_options
from elspeth.web.composer.tools.sources import _handle_patch_source_options
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

Surface = Literal["handler", "dispatch"]
_FIXED_SCHEMA = {"mode": "fixed", "fields": ["text: str"]}


def _context() -> ToolContext:
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    return ToolContext(catalog=PolicyCatalogView.for_trained_operator(catalog, snapshot), plugin_snapshot=snapshot)


def _state(*, source_plugin: str = "csv", output_format: str = "jsonl") -> CompositionState:
    source_options: dict[str, object] = {"path": "data/input.csv", "schema": _FIXED_SCHEMA}
    if source_plugin == "json":
        source_options = {"path": "data/input.jsonl", "format": "jsonl", "schema": _FIXED_SCHEMA}
    return CompositionState(
        sources={
            "submissions": SourceSpec(
                plugin=source_plugin,
                on_success="out",
                on_validation_failure="discard",
                options=source_options,
            )
        },
        nodes=(),
        edges=(),
        outputs=(
            OutputSpec(
                name="out",
                plugin="json",
                on_write_failure="discard",
                options={
                    "path": "outputs/result.jsonl",
                    "format": output_format,
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
            ),
        ),
        metadata=PipelineMetadata(name="Patch hints"),
        version=139,
    )


def _call(surface: Surface, tool: str, arguments: dict[str, object], state: CompositionState) -> ToolResult:
    context = _context()
    if surface == "handler":
        handler = _handle_patch_source_options if tool == "patch_source_options" else _handle_patch_output_options
        return handler(arguments, state, context)
    return execute_tool(tool, arguments, state, context.catalog, plugin_snapshot=context.plugin_snapshot)


def _fixed_schema_hints(plugin_cls: type[CSVSource] | type[JSONSource]) -> tuple[str, ...]:
    hints = plugin_cls.get_post_call_hints(tool_name="patch_source_options", config_snapshot={"schema": _FIXED_SCHEMA})
    assert len(hints) == 1
    return hints


def _json_array_hints() -> tuple[str, ...]:
    hints = JSONSink.get_post_call_hints(
        tool_name="patch_output_options", config_snapshot={"format": "json", "on_write_failure": "discard"}
    )
    assert len(hints) == 1
    return hints


# -- patch_source_options ---------------------------------------------------


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("skip_rows", [1, None])
def test_unrelated_csv_source_patch_does_not_repeat_the_fixed_schema_hint(surface: Surface, skip_rows: int | None) -> None:
    # Live shape: a skip_rows patch on a CSV source whose schema is already fixed.
    state = _state()
    result = _call(surface, "patch_source_options", {"source_name": "submissions", "patch": {"skip_rows": skip_rows}}, state)

    assert result.success
    assert result.updated_state.sources["submissions"].options["schema"]["mode"] == "fixed"
    for hint in _fixed_schema_hints(CSVSource):
        assert hint not in result.post_call_hints
    assert result.post_call_hints == ()


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
def test_unrelated_json_source_patch_does_not_repeat_the_fixed_schema_hint(surface: Surface) -> None:
    state = _state(source_plugin="json")
    result = _call(surface, "patch_source_options", {"source_name": "submissions", "patch": {"encoding": "utf-8"}}, state)

    assert result.success
    assert result.updated_state.sources["submissions"].options["schema"]["mode"] == "fixed"
    assert result.post_call_hints == ()


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize(("source_plugin", "plugin_cls"), [("csv", CSVSource), ("json", JSONSource)])
def test_source_patch_that_sets_a_fixed_schema_still_gets_the_hint(
    surface: Surface, source_plugin: str, plugin_cls: type[CSVSource] | type[JSONSource]
) -> None:
    # Control: the patch touches the key the hint reads, so the hint is still attached.
    state = _state(source_plugin=source_plugin)
    new_schema = {"mode": "fixed", "fields": ["text: str", "score: int"]}
    result = _call(surface, "patch_source_options", {"source_name": "submissions", "patch": {"schema": new_schema}}, state)

    assert result.success
    assert result.post_call_hints == _fixed_schema_hints(plugin_cls)


# -- patch_output_options ---------------------------------------------------


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
def test_unrelated_output_patch_does_not_repeat_the_json_array_hint(surface: Surface) -> None:
    # The saved output already uses array mode; the patch only moves the file.
    state = _state(output_format="json")
    result = _call(surface, "patch_output_options", {"sink_name": "out", "patch": {"path": "outputs/moved.json"}}, state)

    assert result.success
    assert result.updated_state.outputs[0].options["format"] == "json"
    for hint in _json_array_hints():
        assert hint not in result.post_call_hints
    assert result.post_call_hints == ()


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
def test_output_patch_that_sets_array_mode_still_gets_the_hint(surface: Surface) -> None:
    # Control: the patch touches the key the hint reads.
    state = _state(output_format="jsonl")
    result = _call(surface, "patch_output_options", {"sink_name": "out", "patch": {"format": "json"}}, state)

    assert result.success
    assert result.post_call_hints == _json_array_hints()


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
def test_output_patch_snapshot_still_carries_the_write_failure_choice(surface: Surface) -> None:
    # The write-failure choice is never a patched option key, so it must come
    # from the output itself or the missing-choice hint returns.
    missing_choice = JSONSink.get_post_call_hints(tool_name="patch_output_options", config_snapshot={})
    assert len(missing_choice) == 1
    state = _state(output_format="jsonl")
    result = _call(surface, "patch_output_options", {"sink_name": "out", "patch": {"path": "outputs/moved.jsonl"}}, state)

    assert result.success
    assert missing_choice[0] not in result.post_call_hints
