"""A file reference where a local path stands is refused at authoring.

Run admission (``web/execution/service.py``) and the save path
(``web/execution/_validation_authoring.py``) confine every key in
``SOURCE_LOCAL_PATH_OPTION_KEYS``, ``SINK_LOCAL_PATH_OPTION_KEYS`` and
``NESTED_LOCAL_PATH_OPTION_KEYS`` (``web/paths.py``) and admit only a
non-empty string there, so an attached file at one of those keys never runs.
Authoring refuses it over the same keys, as ``plugin_options_invalid`` on that
component, and says nothing about the file being checked later: no
``contract_probe_deferred`` notice for that component.

The positions are generated from the three constants. A control keeps a file
reference at a non-path option of the same plugins out of this refusal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.plugins.infrastructure.validation import get_sink_config_model, get_source_config_model, get_transform_config_model
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec, ValidationSummary
from elspeth.web.paths import NESTED_LOCAL_PATH_OPTION_KEYS, SINK_LOCAL_PATH_OPTION_KEYS, SOURCE_LOCAL_PATH_OPTION_KEYS
from tests.unit.web.composer import test_deferred_contract_notice as graphs

_REFUSAL = "must be a string path; an attached file cannot stand there."

# The registered plugins whose config declares each key; a key no plugin
# declares (``file`` today) is still confined, so it is exercised on csv.
_OWNERS: dict[tuple[str, str], tuple[str, ...]] = {
    ("source", "path"): ("csv", "json", "text"),
    ("sink", "path"): ("csv", "document", "json", "text"),
    ("sink", "persist_directory"): ("chroma_sink",),
    ("nested", "persist_directory"): ("rag_retrieval",),
}
_UNOWNED_PLUGIN = {"source": "csv", "sink": "csv", "nested": "rag_retrieval"}

_POSITIONS: list[tuple[str, str, str]] = [
    (side, key, plugin)
    for side, keys in (
        ("source", SOURCE_LOCAL_PATH_OPTION_KEYS),
        ("sink", SINK_LOCAL_PATH_OPTION_KEYS),
        ("nested", NESTED_LOCAL_PATH_OPTION_KEYS),
    )
    for key in keys
    for plugin in _OWNERS.get((side, key), (_UNOWNED_PLUGIN[side],))
]


def _transform(node_id: str, plugin: str, options: dict[str, Any]) -> NodeSpec:
    return graphs._transform(node_id, plugin, "src_out", "output", options)


def _graph(tmp_path: Path, side: str, key: str, plugin: str, value: object) -> CompositionState:
    """One graph with ``value`` at ``key`` of a ``plugin`` component on ``side``."""
    base = graphs._graph(tmp_path, _transform("pt", "passthrough", {"schema": graphs.OBSERVED}), source_schema=graphs.OBSERVED)
    if side == "source":
        source = SourceSpec(
            plugin=plugin, on_success="src_out", options={key: value, "schema": graphs.OBSERVED}, on_validation_failure="discard"
        )
        return CompositionState(source=source, nodes=base.nodes, edges=(), outputs=base.outputs, metadata=PipelineMetadata(), version=1)
    if side == "sink":
        options: dict[str, Any] = {key: value, "schema": graphs.OBSERVED}
        if plugin in {"text", "document"}:
            options["field"] = "k"
        if plugin == "chroma_sink":
            options.update({"collection": "rows", "mode": "persistent", "document_field": "k", "id_field": "id"})
        output = OutputSpec(name="output", plugin=plugin, options=options, on_write_failure="discard")
        return CompositionState(sources=base.sources, nodes=base.nodes, edges=(), outputs=(output,), metadata=PipelineMetadata(), version=1)
    rag = _transform(
        "rag",
        plugin,
        {
            "provider": "chroma",
            "provider_config": {"collection": "rows", "mode": "persistent", key: value},
            "query_field": "k",
            "output_prefix": "rag",
            "schema": graphs.OBSERVED,
        },
    )
    return CompositionState(sources=base.sources, nodes=(rag,), edges=(), outputs=base.outputs, metadata=PipelineMetadata(), version=1)


def _component(side: str) -> str:
    return {"source": "source", "sink": "output:output", "nested": "node:rag"}[side]


def _subject(side: str, key: str) -> str:
    return {"source": f"Source 'source' {key}", "sink": f"Sink 'output' {key}", "nested": f"Transform 'rag' provider_config.{key}"}[side]


def _refusals(summary: ValidationSummary) -> list[tuple[str, str | None, str]]:
    return [(entry.component, entry.error_code, entry.message) for entry in summary.errors if _REFUSAL in entry.message]


def _notices_on(summary: ValidationSummary, component: str) -> list[str]:
    return [entry.message for entry in summary.warnings if entry.error_code == graphs.DEFERRED and entry.component == component]


def test_the_positions_cover_every_key_of_the_three_constants() -> None:
    covered = {(side, key) for side, key, _plugin in _POSITIONS}
    expected = (
        {("source", key) for key in SOURCE_LOCAL_PATH_OPTION_KEYS}
        | {("sink", key) for key in SINK_LOCAL_PATH_OPTION_KEYS}
        | {("nested", key) for key in NESTED_LOCAL_PATH_OPTION_KEYS}
    )
    assert covered == expected


def test_the_owner_table_matches_the_live_registry() -> None:
    """Every registered plugin whose config declares a confined key is exercised here."""
    manager = get_shared_plugin_manager()

    def declared(get_model: Any, plugins: Any) -> dict[str, set[str]]:
        owners: dict[str, set[str]] = {}
        for plugin in plugins:
            model = get_model(plugin.name)
            for field in model.model_fields if model is not None else ():
                owners.setdefault(field, set()).add(plugin.name)
        return owners

    sources = declared(get_source_config_model, manager.get_sources())
    sinks = declared(get_sink_config_model, manager.get_sinks())
    transforms = declared(get_transform_config_model, manager.get_transforms())
    live = {
        **{("source", key): tuple(sorted(sources[key])) for key in SOURCE_LOCAL_PATH_OPTION_KEYS if key in sources},
        **{("sink", key): tuple(sorted(sinks[key])) for key in SINK_LOCAL_PATH_OPTION_KEYS if key in sinks},
        **{("nested", key): tuple(sorted(transforms["provider_config"])) for key in NESTED_LOCAL_PATH_OPTION_KEYS},
    }
    assert live == _OWNERS


@pytest.mark.parametrize(("side", "key", "plugin"), _POSITIONS, ids=[f"{side}-{key}-{plugin}" for side, key, plugin in _POSITIONS])
def test_a_file_reference_at_a_local_path_key_is_refused_without_a_notice(tmp_path: Path, side: str, key: str, plugin: str) -> None:
    summary = _graph(tmp_path, side, key, plugin, graphs._marker("outputs/anything")).validate()

    assert summary.is_valid is False
    assert _refusals(summary) == [(_component(side), "plugin_options_invalid", f"{_subject(side, key)} {_REFUSAL}")]
    assert _notices_on(summary, _component(side)) == []


@pytest.mark.parametrize(("side", "key", "plugin"), _POSITIONS, ids=[f"{side}-{key}-{plugin}" for side, key, plugin in _POSITIONS])
def test_a_string_at_the_same_key_is_not_refused_by_this_rule(tmp_path: Path, side: str, key: str, plugin: str) -> None:
    folder = "blobs" if side == "source" else "outputs"
    summary = _graph(tmp_path, side, key, plugin, str(tmp_path / folder / str(graphs.SESSION_ID) / "x")).validate()

    assert _refusals(summary) == []


@pytest.mark.parametrize("plugin", ["document", "text"])
def test_a_file_reference_at_a_sink_field_still_defers_with_its_notice(tmp_path: Path, plugin: str) -> None:
    """Control on the same sink plugins: ``field`` is not a path key, so C's deferral stands."""
    base = graphs._graph(tmp_path, _transform("pt", "passthrough", {"schema": graphs.OBSERVED}), source_schema=graphs.FIXED_SOURCE)
    path = str(tmp_path / "outputs" / str(graphs.SESSION_ID) / "o.txt")
    output = OutputSpec(
        name="output",
        plugin=plugin,
        options={"path": path, "field": graphs._marker("k"), "schema": graphs.OBSERVED},
        on_write_failure="discard",
    )
    summary = CompositionState(
        sources=base.sources, nodes=base.nodes, edges=(), outputs=(output,), metadata=PipelineMetadata(), version=1
    ).validate()

    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert _refusals(summary) == []
    assert len(_notices_on(summary, "output:output")) == 1


@pytest.mark.parametrize(
    ("side", "plugin", "key"),
    [("source", "csv", "delimiter"), ("sink", "chroma_sink", "collection"), ("nested", "rag_retrieval", "collection")],
)
def test_a_file_reference_at_a_non_path_key_of_the_same_plugin_is_not_refused_by_this_rule(
    tmp_path: Path, side: str, plugin: str, key: str
) -> None:
    """Control: only the confined keys carry this refusal; other options keep their own rules."""
    marker = graphs._marker("rows")
    if side == "source":
        state = _graph(tmp_path, side, "path", plugin, str(tmp_path / "blobs" / str(graphs.SESSION_ID) / "input.csv"))
        source = state.sources["source"]
        state = CompositionState(
            source=SourceSpec(
                plugin=plugin, on_success="src_out", options={**source.options, key: marker}, on_validation_failure="discard"
            ),
            nodes=state.nodes,
            edges=(),
            outputs=state.outputs,
            metadata=PipelineMetadata(),
            version=1,
        )
    elif side == "sink":
        state = _graph(tmp_path, side, "persist_directory", plugin, str(tmp_path / "outputs" / str(graphs.SESSION_ID) / "chroma"))
        output = state.outputs[0]
        state = CompositionState(
            sources=state.sources,
            nodes=state.nodes,
            edges=(),
            outputs=(OutputSpec(name="output", plugin=plugin, options={**output.options, key: marker}, on_write_failure="discard"),),
            metadata=PipelineMetadata(),
            version=1,
        )
    else:
        state = _graph(tmp_path, side, "persist_directory", plugin, str(tmp_path / "outputs" / str(graphs.SESSION_ID) / "chroma"))
        rag = state.nodes[0]
        provider_config = {**rag.options["provider_config"], key: marker}
        state = CompositionState(
            sources=state.sources,
            nodes=(_transform("rag", plugin, {**rag.options, "provider_config": provider_config}),),
            edges=(),
            outputs=state.outputs,
            metadata=PipelineMetadata(),
            version=1,
        )

    assert _refusals(state.validate()) == []
