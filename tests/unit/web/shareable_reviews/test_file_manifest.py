"""Tests for the share-path file-binding and sink-path collectors."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, get_args
from uuid import UUID, uuid4

import pytest

from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
)
from elspeth.web.paths import SINK_LOCAL_PATH_OPTION_KEYS
from elspeth.web.shareable_reviews.file_manifest import (
    BoundBlob,
    FileBinding,
    ShareManifestError,
    SinkPathEntry,
    SinkPathOption,
    _collect_inline_markers,
    collect_file_bindings,
    collect_sink_paths,
    file_binding_from_json,
    file_binding_to_json,
    sink_path_entry_from_json,
    sink_path_entry_to_json,
)

SHA = "a" * 64


def _marker(blob_id: UUID, *, encoding: str | None = None, mode: str = "inline_content") -> dict[str, Any]:
    marker: dict[str, Any] = {"blob_ref": str(blob_id), "mode": mode}
    if mode == "inline_content":
        marker["sha256"] = SHA
    if encoding is not None:
        marker["encoding"] = encoding
    return marker


def _node(node_id: str, options: Mapping[str, Any]) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin="passthrough",
        input="in",
        on_success="out",
        on_error=None,
        options=options,
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _state(
    *,
    source_options: Mapping[str, Any] | None = None,
    source_plugin: str = "csv",
    nodes: tuple[NodeSpec, ...] = (),
    output_options: Mapping[str, Any] | None = None,
) -> CompositionState:
    return CompositionState(
        source=SourceSpec(
            plugin=source_plugin,
            on_success="in",
            options=source_options if source_options is not None else {"path": "x.csv"},
            on_validation_failure="discard",
        ),
        nodes=nodes,
        edges=(),
        outputs=(OutputSpec(name="out", plugin="csv", options=output_options or {"path": "o.csv"}, on_write_failure="discard"),),
        metadata=PipelineMetadata(),
        version=1,
    )


def test_collects_source_blob_ref() -> None:
    blob_id = uuid4()
    state = _state(source_options={"blob_ref": str(blob_id), "path": "/blobs/x.csv", "mode": "bind_source"})

    assert collect_file_bindings(state) == (BoundBlob(blob_id, FileBinding("source_blob_ref", "source", None, None, None)),)


def test_collects_blob_rows_entries_with_index() -> None:
    first, second = uuid4(), uuid4()
    entry = {"payload_ref": "p", "filename": "f.txt", "mime_type": "text/plain", "size_bytes": 3}
    state = _state(
        source_plugin="blob_rows",
        source_options={"blobs": [{"blob_id": str(first), **entry}, {"blob_id": str(second), **entry}]},
    )

    assert collect_file_bindings(state) == (
        BoundBlob(first, FileBinding("source_blob_rows", "source", 0, None, None)),
        BoundBlob(second, FileBinding("source_blob_rows", "source", 1, None, None)),
    )


def test_collects_nested_inline_marker_with_canonical_field_path() -> None:
    plain, latin = uuid4(), uuid4()
    node = _node(
        "lookup",
        {"tables": {"t": {"reference_content": _marker(plain), "other_content": _marker(latin, encoding="latin-1")}}},
    )

    bindings = collect_file_bindings(_state(nodes=(node,)))

    assert bindings == (
        BoundBlob(latin, FileBinding("inline_content", None, None, "node:lookup.options.tables.t.other_content", "latin-1")),
        BoundBlob(plain, FileBinding("inline_content", None, None, "node:lookup.options.tables.t.reference_content", "utf-8")),
    )


def test_collects_inline_markers_on_sources_and_outputs() -> None:
    src_blob, out_blob = uuid4(), uuid4()
    state = _state(
        source_options={"path": "x.csv", "x": _marker(src_blob)},
        output_options={"path": "o.csv", "y": _marker(out_blob)},
    )

    assert collect_file_bindings(state) == (
        BoundBlob(src_blob, FileBinding("inline_content", None, None, "source:source.options.x", "utf-8")),
        BoundBlob(out_blob, FileBinding("inline_content", None, None, "output:out.options.y", "utf-8")),
    )


def test_same_blob_in_two_places_yields_two_bindings() -> None:
    blob_id = uuid4()
    state = _state(nodes=(_node("a", {"c": _marker(blob_id)}), _node("b", {"c": _marker(blob_id)})))

    bindings = collect_file_bindings(state)

    assert [b.blob_id for b in bindings] == [blob_id, blob_id]
    assert [b.binding.field_path for b in bindings] == ["node:a.options.c", "node:b.options.c"]


def test_marker_inside_a_list_raises_marker_in_list() -> None:
    with pytest.raises(ShareManifestError) as excinfo:
        _collect_inline_markers({"items": [{"deep": _marker(uuid4())}]}, "node:a.options.c", False, [])

    assert excinfo.value.reason == "marker_in_list"


def test_marker_inside_a_list_in_state_raises_marker_in_list() -> None:
    state = _state(nodes=(_node("a", {"items": [{"deep": _marker(uuid4())}, _marker(uuid4())]}),))

    with pytest.raises(ShareManifestError) as excinfo:
        collect_file_bindings(state)

    assert excinfo.value.reason == "marker_in_list"


def test_bind_source_widened_marker_is_not_an_inline_binding() -> None:
    state = _state(nodes=(_node("a", {"c": _marker(uuid4(), mode="bind_source")}),))

    with pytest.raises(ShareManifestError) as excinfo:
        collect_file_bindings(state)

    assert excinfo.value.reason == "malformed_binding"


def test_malformed_nested_marker_raises_malformed_binding() -> None:
    state = _state(nodes=(_node("a", {"c": {"blob_ref": "not-a-uuid", "mode": "inline_content"}}),))

    with pytest.raises(ShareManifestError) as excinfo:
        collect_file_bindings(state)

    assert excinfo.value.reason == "malformed_binding"


def test_malformed_top_level_blob_ref_raises_malformed_binding() -> None:
    with pytest.raises(ShareManifestError) as excinfo:
        collect_file_bindings(_state(source_options={"blob_ref": 7}))

    assert excinfo.value.reason == "malformed_binding"


@pytest.mark.parametrize(
    "binding",
    [
        FileBinding("source_blob_ref", "source", None, None, None),
        FileBinding("source_blob_rows", "source", 2, None, None),
        FileBinding("inline_content", None, None, "node:n.options.a.b", "utf-16"),
    ],
)
def test_binding_json_round_trips(binding: FileBinding) -> None:
    assert file_binding_from_json(file_binding_to_json(binding)) == binding


def test_binding_from_json_rejects_extra_and_missing_keys() -> None:
    good = file_binding_to_json(FileBinding("source_blob_ref", "source", None, None, None))

    with pytest.raises(ValueError):
        file_binding_from_json({**good, "extra": 1})
    with pytest.raises(ValueError):
        file_binding_from_json({k: v for k, v in good.items() if k != "kind"})
    with pytest.raises(ValueError):
        file_binding_from_json("nope")
    with pytest.raises(ValueError):
        file_binding_from_json({**good, "kind": "bogus"})
    with pytest.raises(ValueError):
        file_binding_from_json(
            {"kind": "inline_content", "source_name": None, "row_index": None, "field_path": "bad path", "encoding": "utf-8"}
        )


# ── Sink output paths ───────────────────────────────────────────────────

_SID = "5b0f3c0e-8d55-4c1e-9a52-3f1d2b8e7c11"
_OTHER_SID = "9e2d7a40-1b6c-4f3a-8e5d-0c4b3a2f1e99"


def _sink_state(outputs: Mapping[str, Mapping[str, Any]]) -> CompositionState:
    return replace(
        _state(),
        outputs=tuple(
            OutputSpec(name=name, plugin="csv", options=options, on_write_failure="discard") for name, options in outputs.items()
        ),
    )


def test_collects_sink_paths_inside_the_sharers_session_folders(tmp_path: Path) -> None:
    data = tmp_path.resolve()
    state = _sink_state(
        {
            "report": {"path": str(data / "outputs" / _SID / "report.csv"), "schema": {"mode": "observed"}},
            "stable": {"path": "outputs/stable.csv"},
            "blob_out": {"file": f"blobs/{_SID}/out.jsonl"},
            "chroma": {"persist_directory": str(data / "outputs" / _SID / "index" / "chroma")},
        },
    )

    assert collect_sink_paths(state, data_dir=tmp_path, session_id=_SID) == (
        SinkPathEntry("blob_out", "file", "blobs", "out.jsonl"),
        SinkPathEntry("chroma", "persist_directory", "outputs", "index/chroma"),
        SinkPathEntry("report", "path", "outputs", "report.csv"),
        SinkPathEntry("stable", "path", "outputs", "stable.csv"),
    )


def test_collects_every_path_key_of_one_output_sorted_by_option(tmp_path: Path) -> None:
    state = _sink_state({"both": {"path": "outputs/a.csv", "file": "outputs/b.csv"}})

    assert collect_sink_paths(state, data_dir=tmp_path, session_id=_SID) == (
        SinkPathEntry("both", "file", "outputs", "b.csv"),
        SinkPathEntry("both", "path", "outputs", "a.csv"),
    )


@pytest.mark.parametrize(
    "options",
    [
        pytest.param({"path": "/srv/elsewhere/report.csv"}, id="outside-data-dir"),
        pytest.param({"path": f"outputs/{_OTHER_SID}/report.csv"}, id="another-session"),
        pytest.param({"path": f"outputs/{_SID}/../escape.csv"}, id="dotdot-escape"),
        pytest.param({"path": f"blobs/{_SID}/../{_OTHER_SID}/x.csv"}, id="dotdot-into-another-session"),
        pytest.param({"path": "blobs/report.csv"}, id="shared-blob-root"),
        pytest.param({"persist_directory": f"outputs/{_SID}"}, id="the-folder-itself"),
        pytest.param({"path": None}, id="none"),
        pytest.param({"path": 7}, id="not-a-string"),
        pytest.param({"path": "outputs/bad\x00name.csv"}, id="nul-byte"),
        pytest.param({"path": "outputs/a\nINFO fake log line.csv"}, id="control-character"),
        pytest.param({"path": "outputs/" + "/".join(["d" * 100] * 11) + "/x.csv"}, id="longer-than-1024"),
        pytest.param({"path": "outputs/sk-" + "a" * 24 + ".json"}, id="credential-shaped"),
        pytest.param({"url": "outputs/report.csv", "dataset": {"path": "outputs/nested.csv"}}, id="not-a-top-level-path-key"),
    ],
)
def test_sink_paths_outside_the_session_folders_are_not_recorded(tmp_path: Path, options: Mapping[str, Any]) -> None:
    state = _sink_state({"out": options})

    assert collect_sink_paths(state, data_dir=tmp_path, session_id=_SID) == ()


def test_sink_path_option_literal_matches_the_sink_path_keys() -> None:
    assert frozenset(get_args(SinkPathOption)) == frozenset(SINK_LOCAL_PATH_OPTION_KEYS)


def test_sink_path_entry_json_round_trips() -> None:
    entry = SinkPathEntry("out", "persist_directory", "blobs", "index/chroma")

    assert sink_path_entry_to_json(entry) == {
        "output": "out",
        "option": "persist_directory",
        "folder": "blobs",
        "relative_path": "index/chroma",
    }
    assert sink_path_entry_from_json(sink_path_entry_to_json(entry)) == entry


_GOOD_SINK_JSON: dict[str, object] = {"output": "out", "option": "path", "folder": "outputs", "relative_path": "report.csv"}


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("nope", id="not-object"),
        pytest.param({**_GOOD_SINK_JSON, "extra": 1}, id="extra-key"),
        pytest.param({k: v for k, v in _GOOD_SINK_JSON.items() if k != "folder"}, id="missing-key"),
        pytest.param({**_GOOD_SINK_JSON, "output": ""}, id="output-blank"),
        pytest.param({**_GOOD_SINK_JSON, "output": 1}, id="output-int"),
        pytest.param({**_GOOD_SINK_JSON, "option": "url"}, id="option-unknown"),
        pytest.param({**_GOOD_SINK_JSON, "folder": "payloads"}, id="folder-unknown"),
        pytest.param({**_GOOD_SINK_JSON, "relative_path": 3}, id="relative-int"),
        pytest.param({**_GOOD_SINK_JSON, "relative_path": ""}, id="relative-empty"),
        pytest.param({**_GOOD_SINK_JSON, "relative_path": "../x"}, id="relative-dotdot"),
        pytest.param({**_GOOD_SINK_JSON, "relative_path": "/etc/passwd"}, id="relative-absolute"),
        pytest.param({**_GOOD_SINK_JSON, "relative_path": "a/./b"}, id="relative-not-normalised"),
        pytest.param({**_GOOD_SINK_JSON, "relative_path": "a\tb.csv"}, id="relative-control-character"),
        pytest.param({**_GOOD_SINK_JSON, "relative_path": "a" * 1025}, id="relative-longer-than-1024"),
    ],
)
def test_sink_path_entry_from_json_rejects_shape_drift(value: object) -> None:
    with pytest.raises(ValueError):
        sink_path_entry_from_json(value)
