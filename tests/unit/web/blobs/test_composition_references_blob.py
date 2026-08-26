"""Tests for active-run blob reference discovery across composition state."""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.blobs.service import _composition_references_blob


def test_composition_references_blob_finds_transform_inline_content_ref() -> None:
    composition_state = {
        "sources": {"primary": {"plugin": "csv", "options": {"path": "data.csv"}}},
        "transforms": [
            {
                "name": "classify",
                "plugin": "llm",
                "options": {
                    "system_prompt": {
                        "blob_ref": "blob-123",
                        "mode": "inline_content",
                        "sha256": "a" * 64,
                    }
                },
            }
        ],
        "sinks": {"output": {"plugin": "csv", "options": {"path": "out.csv"}}},
    }

    assert _composition_references_blob(composition_state, "blob-123", "/unused")


def test_composition_references_blob_preserves_legacy_path_match() -> None:
    composition_state = {
        "sources": {"primary": {"plugin": "csv", "options": {"path": "/blob/storage.csv"}}},
    }

    assert _composition_references_blob(composition_state, "other-blob", "/blob/storage.csv")


def test_composition_references_blob_finds_blob_rows_entry_blob_id() -> None:
    """blob_rows persists custody as blobs[].blob_id — the active-run scanner
    must see it, or a delete racing background run startup (before
    _link_blob_rows_to_run creates the run-link rows) succeeds and admission
    then fails on a missing blob (elspeth-0c6a343921 review)."""
    composition_state = {
        "sources": {
            "documents": {
                "plugin": "blob_rows",
                "options": {
                    "blobs": [
                        {
                            "blob_id": "0a274caf-6d51-44a4-b8ef-3d2f5f77a1f0",
                            "payload_ref": "c" * 64,
                            "filename": "doc.pdf",
                            "mime_type": "application/pdf",
                            "size_bytes": 1234,
                        }
                    ],
                    "schema": {"mode": "observed"},
                },
            }
        },
    }

    assert _composition_references_blob(composition_state, "0a274caf-6d51-44a4-b8ef-3d2f5f77a1f0", "/unused")
    assert not _composition_references_blob(composition_state, "1b385db0-7e62-55b5-c9f0-4e3f6f88b2f1", "/unused")


def test_composition_references_blob_crashes_on_corrupt_source_options() -> None:
    composition_state = {
        "sources": {"primary": {"plugin": "csv", "options": ["not", "a", "dict"]}},
    }

    with pytest.raises(AuditIntegrityError, match=r"sources\['primary'\]\.options"):
        _composition_references_blob(composition_state, "blob-123", "/unused")


# --------------------------------------------------------------------------- #
# Emitted-section coverage (elspeth-ca79b2c63a)                                #
#                                                                              #
# The delete guard's node-collection tuple omitted `collectors`, so a blob      #
# referenced ONLY from a collector's options read as unreferenced and deletion  #
# was permitted while a run still held it. Nothing below names a section: the   #
# sections come from the emitter, so a new options-bearing node kind is         #
# covered on the day it is emitted.                                            #
# --------------------------------------------------------------------------- #


def _emitted_pipeline_with_every_node_kind(marker: dict[str, str]) -> dict[str, Any]:
    """Emit a pipeline dict carrying one node of every declared kind.

    `_composition_references_blob` documents its input as the dict emitted by
    `generate_pipeline_dict()` or `pipeline_dict_from_record()` — and the
    latter IS `generate_pipeline_dict(state_from_record(record))`. One emitter,
    both entry points, so what it emits is the authority for what to walk.
    """
    from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
    from elspeth.web.composer.yaml_generator import generate_pipeline_dict

    def node(**overrides: Any) -> NodeSpec:
        base: dict[str, Any] = {
            "id": "n",
            "node_type": "transform",
            "input": "rows",
            "plugin": "p",
            "on_success": "out",
            "on_error": "discard",
            "options": {"payload": dict(marker)},
            "condition": None,
            "routes": None,
            "fork_to": None,
            "branches": None,
            "policy": None,
            "merge": None,
        }
        return NodeSpec(**{**base, **overrides})

    state = CompositionState(
        source=SourceSpec(plugin="csv", on_success="rows", options={"payload": dict(marker)}, on_validation_failure=None, description=None),
        nodes=(
            node(id="t", node_type="transform"),
            node(id="g", node_type="gate", plugin=None, options={}, condition="x", routes={"true": "out"}),
            node(id="a", node_type="aggregation"),
            node(id="c", node_type="coalesce", plugin=None, options={}, branches=("b1", "b2"), policy="require_all", merge="union"),
            node(id="r", node_type="row_union", plugin=None, options={}, branches=("b1", "b2")),
            node(id="q", node_type="queue", plugin=None, input="q", on_success=None, on_error=None, options={}),
            node(id="k", node_type="collector", scope_name="s", scope_opener="t", scope_policy="require_all"),
        ),
        edges=(),
        outputs=(OutputSpec(name="out", plugin="json", options={"payload": dict(marker)}, on_write_failure=None, description=None),),
        metadata=PipelineMetadata(),
        version=1,
    )
    return generate_pipeline_dict(state)


def _sections_carrying_options(pipeline: dict[str, Any]) -> list[str]:
    """Emitted sections whose entries carry a non-empty `options` mapping.

    Keyed on `options` ALONE — a blob reference hides there, and whether the
    entry also names a plugin belongs to a different concern. Do not narrow
    this to a plugin-bearing set.
    """
    carrying: list[str] = []
    for key, value in pipeline.items():
        entries = list(value.values()) if isinstance(value, dict) else value if isinstance(value, list) else []
        if any(isinstance(entry, dict) and entry.get("options") for entry in entries):
            carrying.append(key)
    return sorted(carrying)


def test_delete_guard_sees_a_reference_in_every_options_bearing_emitted_section() -> None:
    blob_id = "0a274caf-6d51-44a4-b8ef-3d2f5f77a1f0"
    marker = {"blob_ref": blob_id, "mode": "inline_content", "sha256": "a" * 64}
    pipeline = _emitted_pipeline_with_every_node_kind(marker)
    sections = _sections_carrying_options(pipeline)
    assert sections, "the emitter produced no options-bearing section — the fixture is inert"

    blind: list[str] = []
    for section in sections:
        isolated = {section: pipeline[section]}
        if not _composition_references_blob(isolated, blob_id, "/unused"):
            blind.append(section)

    assert not blind, (
        f"a blob referenced only from {blind} reads as UNREFERENCED to the delete guard, so deletion is "
        f"permitted while an active run still holds it. Derive the walked keys from the emitter, never list them."
    )
