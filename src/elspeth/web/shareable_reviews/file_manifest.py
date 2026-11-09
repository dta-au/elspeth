"""Collect every uploaded-file binding a composition carries.

A shared pipeline graph is attached to the files the owner uploaded. Three
spellings tie a composition to a blob, and the share path (export) and the
import path both need the same answer to "which blobs does this graph use, and
where":

1. a source's top-level ``options["blob_ref"]`` (a string UUID, usually with a
   ``path``): the source reads that blob's file;
2. a ``blob_rows`` source's ``options["blobs"]`` entries, each carrying a
   ``blob_id``: one row per blob;
3. a nested widened ``mode: "inline_content"`` marker at any dict depth inside
   source, node or output options: the blob's text is inlined into that field.

The collector is pure: it reads a ``CompositionState`` and returns one
``BoundBlob`` per binding site. The same blob bound in two places yields two
bindings, because each site is rebound independently on import.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, cast, get_args
from uuid import UUID

from elspeth.contracts.blobs_inline import (
    ALLOWED_CONTENT_ENCODINGS,
    BlobInlineRef,
    ContentEncoding,
    is_widened_blob_ref,
)
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, SourceSpec

__all__ = [
    "BindingKind",
    "BoundBlob",
    "FileBinding",
    "ShareManifestError",
    "collect_file_bindings",
    "file_binding_from_json",
    "file_binding_to_json",
]

BindingKind = Literal["source_blob_ref", "source_blob_rows", "inline_content"]
_BINDING_KINDS: Final = frozenset(get_args(BindingKind))
_BINDING_JSON_KEYS: Final = frozenset({"kind", "source_name", "row_index", "field_path", "encoding"})
# Placeholder digest: path validation is delegated to BlobInlineRef, which also
# checks a digest, and the collector has no use for the real one.
_50594ad296428dbc8d86f1aa3976f703cd5fdf728102f2289557dfbceb95273d_SHA256: Final = "0" * 64


@dataclass(frozen=True, slots=True)
class FileBinding:
    """Where in a composition a blob is bound."""

    kind: BindingKind
    source_name: str | None
    row_index: int | None
    field_path: str | None
    encoding: ContentEncoding | None


@dataclass(frozen=True, slots=True)
class BoundBlob:
    blob_id: UUID
    binding: FileBinding


class ShareManifestError(Exception):
    """A composition's file bindings cannot be shared faithfully."""

    def __init__(self, reason: Literal["marker_in_list", "malformed_binding"], detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _checked_field_path(field_path: str) -> str:
    """Return ``field_path`` once the inline-ref grammar has accepted it."""
    try:
        BlobInlineRef(
            field_path=field_path,
            blob_id=UUID(int=0),
            sha256=_50594ad296428dbc8d86f1aa3976f703cd5fdf728102f2289557dfbceb95273d_SHA256,
            encoding="utf-8",
        )
    except ValueError as exc:
        raise ShareManifestError("malformed_binding", f"{field_path!r} is not a valid option field path") from exc
    return field_path


def _parse_uuid(raw: object, where: str) -> UUID:
    if type(raw) is not str:
        raise ShareManifestError("malformed_binding", f"{where} must be a string UUID, got {type(raw).__name__}")
    try:
        return UUID(raw)
    except ValueError as exc:
        raise ShareManifestError("malformed_binding", f"{where} is not a valid UUID") from exc


@trust_boundary(
    tier=3,
    source="an arbitrary-depth value nested inside a source/node/output options block — planner/LLM-authored "
    "plugin option content reached through the composer tool loop, of no proven shape at any depth",
    source_param="value",
    suppresses=("R5",),
    invariant=(
        "raises ShareManifestError(marker_in_list) for a widened blob marker nested inside a list, and "
        "ShareManifestError(malformed_binding) for a blob_ref mapping that is not a well-formed inline_content marker "
        "or whose field path is not canonical; any other shape is walked or ignored, never coerced"
    ),
    test_ref="tests/unit/web/shareable_reviews/test_file_manifest.py::test_marker_inside_a_list_raises_marker_in_list",
    test_fingerprint="9afc829d25002c8c725a0613cf02cd1997aa6ddb8a0eb44ea6c4043d13ac0b5d",
)
def _collect_inline_markers(value: object, field_path: str, in_list: bool, found: list[BoundBlob]) -> None:
    if isinstance(value, Mapping):
        if "blob_ref" in value:
            if in_list:
                raise ShareManifestError("marker_in_list", f"{field_path} holds a file marker inside a list")
            marker = is_widened_blob_ref(dict(value))
            if marker is None or marker.mode != "inline_content":
                raise ShareManifestError("malformed_binding", f"{field_path} is not a well-formed inline_content file marker")
            found.append(
                BoundBlob(
                    marker.blob_id,
                    FileBinding("inline_content", None, None, _checked_field_path(field_path), marker.encoding),
                )
            )
            return
        for key in sorted(value, key=str):
            _collect_inline_markers(value[key], f"{field_path}.{key}", in_list, found)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _collect_inline_markers(item, f"{field_path}[{index}]", True, found)


def _collect_options(prefix: str, spec: NodeSpec | OutputSpec, found: list[BoundBlob]) -> None:
    for key in sorted(spec.options):
        _collect_inline_markers(spec.options[key], f"{prefix}.options.{key}", False, found)


def _collect_source(name: str, source: SourceSpec, found: list[BoundBlob]) -> None:
    for key in sorted(source.options):
        value = source.options[key]
        if key == "blob_ref":
            blob_id = _parse_uuid(value, f"source {name!r} options.blob_ref")
            found.append(BoundBlob(blob_id, FileBinding("source_blob_ref", name, None, None, None)))
        elif key == "blobs" and source.plugin == "blob_rows":
            rows = deep_thaw(value)
            if type(rows) is not list:
                raise ShareManifestError("malformed_binding", f"source {name!r} options.blobs must be a list")
            for index, entry in enumerate(rows):
                if type(entry) is not dict or "blob_id" not in entry:
                    raise ShareManifestError("malformed_binding", f"source {name!r} options.blobs[{index}] has no blob_id")
                blob_id = _parse_uuid(entry["blob_id"], f"source {name!r} options.blobs[{index}].blob_id")
                found.append(BoundBlob(blob_id, FileBinding("source_blob_rows", name, index, None, None)))
        else:
            _collect_inline_markers(value, f"source:{name}.options.{key}", False, found)


def collect_file_bindings(state: CompositionState) -> tuple[BoundBlob, ...]:
    """Return every file binding in ``state``.

    Order: sources by name, then nodes in state order, then outputs in state
    order; within a component, option keys in sorted order.
    """
    found: list[BoundBlob] = []
    for name in sorted(state.sources):
        _collect_source(name, state.sources[name], found)
    for node in state.nodes:
        _collect_options(f"node:{node.id}", node, found)
    for output in state.outputs:
        _collect_options(f"output:{output.name}", output, found)
    return tuple(found)


def file_binding_to_json(binding: FileBinding) -> dict[str, object]:
    return {
        "kind": binding.kind,
        "source_name": binding.source_name,
        "row_index": binding.row_index,
        "field_path": binding.field_path,
        "encoding": binding.encoding,
    }


def _optional_str(value: object, key: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError(f"file binding {key} must be a string or null")
    return value


def file_binding_from_json(value: object) -> FileBinding:
    """Rebuild a ``FileBinding``; raises ``ValueError`` on any shape drift."""
    if type(value) is not dict:
        raise ValueError("file binding must be a JSON object")
    payload = cast(dict[str, object], value)
    if set(payload) != _BINDING_JSON_KEYS:
        raise ValueError(f"file binding keys must be exactly {sorted(_BINDING_JSON_KEYS)}")
    kind = payload["kind"]
    if type(kind) is not str or kind not in _BINDING_KINDS:
        raise ValueError(f"file binding kind must be one of {sorted(_BINDING_KINDS)}")
    source_name = _optional_str(payload["source_name"], "source_name")
    field_path = _optional_str(payload["field_path"], "field_path")
    encoding = _optional_str(payload["encoding"], "encoding")
    row_index = payload["row_index"]
    if row_index is not None and (type(row_index) is not int or row_index < 0):
        raise ValueError("file binding row_index must be a non-negative integer or null")
    if encoding is not None and encoding not in ALLOWED_CONTENT_ENCODINGS:
        raise ValueError(f"file binding encoding must be one of {sorted(ALLOWED_CONTENT_ENCODINGS)}")
    if kind == "inline_content":
        if field_path is None or encoding is None or source_name is not None or row_index is not None:
            raise ValueError("inline_content binding needs field_path and encoding only")
        try:
            _checked_field_path(field_path)
        except ShareManifestError as exc:
            raise ValueError(exc.detail) from exc
    else:
        if source_name is None or field_path is not None or encoding is not None:
            raise ValueError(f"{kind} binding needs source_name only")
        if (kind == "source_blob_rows") != (row_index is not None):
            raise ValueError("row_index is set exactly for source_blob_rows")
    return FileBinding(
        cast(BindingKind, kind),
        source_name,
        row_index,
        field_path,
        cast("ContentEncoding | None", encoding),
    )
