"""Unit tests for the closed proposal blob-reference extractor.

``proposal_blob_reference_ids`` is the single authority deciding which blobs
a pending proposal retains (blocking their mutation/deletion) and which blob
references a staged proposal must prove ownership of.  It historically
recognized only ``set_pipeline`` ``source.blob_id`` and owned-authority
top-level ``options.blob_ref`` — the guided ``blob:<uuid>`` path sentinels
and nested inline markers were invisible, so a reviewed blob could be
deleted while a proposal depending on it was pending
(elspeth-b3feba9a7c).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from elspeth.web.composer.pipeline_proposal import OWNED_COMPOSITION_STATE_AUTHORITY
from elspeth.web.sessions.proposal_blob_refs import proposal_blob_reference_ids


def _sha() -> str:
    return "a" * 64


class TestLegacySetPipelineShapes:
    def test_source_blob_id_is_extracted(self) -> None:
        blob_id = str(uuid4())
        arguments = {"source": {"plugin": "csv", "blob_id": blob_id, "options": {"schema": {"mode": "observed"}}}}
        assert proposal_blob_reference_ids("set_pipeline", arguments) == (blob_id,)

    def test_source_options_path_sentinel_is_extracted(self) -> None:
        """Direct repro from elspeth-b3feba9a7c: source.options.path='blob:<uuid>'."""
        blob_id = str(uuid4())
        arguments = {"source": {"plugin": "csv", "options": {"path": f"blob:{blob_id}", "schema": {"mode": "observed"}}}}
        assert proposal_blob_reference_ids("set_pipeline", arguments) == (blob_id,)

    def test_source_options_file_sentinel_is_extracted(self) -> None:
        blob_id = str(uuid4())
        arguments = {"source": {"plugin": "json", "options": {"file": f"blob:{blob_id}"}}}
        assert proposal_blob_reference_ids("set_pipeline", arguments) == (blob_id,)

    def test_source_options_blob_ref_is_extracted(self) -> None:
        blob_id = str(uuid4())
        arguments = {"source": {"plugin": "csv", "options": {"blob_ref": blob_id, "path": "/ignored/raw/path.csv"}}}
        assert proposal_blob_reference_ids("set_pipeline", arguments) == (blob_id,)

    def test_nested_inline_marker_in_source_options_is_extracted(self) -> None:
        blob_id = str(uuid4())
        arguments = {
            "source": {
                "plugin": "csv",
                "options": {"queries": {"blob_ref": blob_id, "mode": "inline_content", "sha256": _sha()}},
            }
        }
        assert proposal_blob_reference_ids("set_pipeline", arguments) == (blob_id,)

    def test_blob_id_and_sentinel_dedupe(self) -> None:
        blob_id = str(uuid4())
        arguments = {"source": {"plugin": "csv", "blob_id": blob_id, "options": {"path": f"blob:{blob_id}"}}}
        assert proposal_blob_reference_ids("set_pipeline", arguments) == (blob_id,)

    def test_non_blob_path_yields_nothing(self) -> None:
        arguments = {"source": {"plugin": "csv", "options": {"path": "/data/rows.csv"}}}
        assert proposal_blob_reference_ids("set_pipeline", arguments) == ()

    def test_malformed_sentinel_raises(self) -> None:
        arguments = {"source": {"plugin": "csv", "options": {"path": "blob:not-a-uuid"}}}
        with pytest.raises(ValueError):
            proposal_blob_reference_ids("set_pipeline", arguments)

    def test_non_string_nested_blob_ref_raises(self) -> None:
        arguments = {"source": {"plugin": "csv", "options": {"queries": {"blob_ref": 7}}}}
        with pytest.raises(ValueError):
            proposal_blob_reference_ids("set_pipeline", arguments)


class TestOwnedAuthorityShapes:
    def _owned(self, *, sources: dict, nodes: list | None = None, outputs: list | None = None) -> dict:
        return {
            "authority_kind": OWNED_COMPOSITION_STATE_AUTHORITY,
            "sources": sources,
            "nodes": nodes if nodes is not None else [],
            "edges": [],
            "outputs": outputs if outputs is not None else [],
            "metadata": {"name": "owned", "description": ""},
        }

    def test_named_source_path_sentinels_are_extracted(self) -> None:
        blob_a = str(uuid4())
        blob_b = str(uuid4())
        arguments = self._owned(
            sources={
                "orders": {"plugin": "csv", "options": {"path": f"blob:{blob_a}"}},
                "refunds": {"plugin": "csv", "options": {"file": f"blob:{blob_b}"}},
            }
        )
        assert set(proposal_blob_reference_ids("set_pipeline", arguments)) == {blob_a, blob_b}

    def test_node_and_output_nested_markers_are_extracted(self) -> None:
        blob_node = str(uuid4())
        blob_output = str(uuid4())
        arguments = self._owned(
            sources={"source": {"plugin": "csv", "options": {"path": "/data/rows.csv"}}},
            nodes=[
                {
                    "id": "enrich",
                    "node_type": "transform",
                    "plugin": "llm",
                    "options": {"prompts": [{"blob_ref": blob_node, "mode": "inline_content", "sha256": _sha()}]},
                }
            ],
            outputs=[
                {
                    "name": "result",
                    "plugin": "json",
                    "options": {"template": {"blob_ref": blob_output, "mode": "inline_content", "sha256": _sha()}},
                }
            ],
        )
        assert set(proposal_blob_reference_ids("set_pipeline", arguments)) == {blob_node, blob_output}

    def test_top_level_blob_ref_still_extracted(self) -> None:
        blob_id = str(uuid4())
        arguments = self._owned(sources={"source": {"plugin": "csv", "options": {"blob_ref": blob_id, "path": "/raw.csv"}}})
        assert proposal_blob_reference_ids("set_pipeline", arguments) == (blob_id,)
