"""Tests for the share-path file-binding collector."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

import pytest

from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
)
from elspeth.web.shareable_reviews.file_manifest import (
    BoundBlob,
    FileBinding,
    ShareManifestError,
    _collect_inline_markers,
    collect_file_bindings,
    file_binding_from_json,
    file_binding_to_json,
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
