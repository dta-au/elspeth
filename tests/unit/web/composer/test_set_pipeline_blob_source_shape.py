"""Which source options shape ``set_pipeline`` leaves for a ``blob:<id>`` path.

Measured by execution, not read off the handler: a reviewed source whose
``path`` is the ``blob:<id>`` sentinel is rehydrated to the blob's storage
path and gains ``blob_ref``. Nothing in that step writes ``mode``. The
result therefore carries ``mode: bind_source`` only when the reviewed options
already did, and is otherwise the same mode-less ``blob_ref`` + ``path``
shape the YAML import writes. The inline ref walk has to read both as a
source binding; before the fix it called the mode-less one a malformed file
marker, which lost every prompt file in the same state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from elspeth.core.blobs_inline import _discover_blob_content_refs
from elspeth.web.composer.pipeline_proposal import reviewed_anchor_hash
from elspeth.web.composer.reviewed_source_authority import resolve_reviewed_source_authority
from elspeth.web.composer.tools import build_set_pipeline_candidate
from tests.unit.web.composer import test_set_pipeline_candidate as candidates

_PROMPT_MARKER = {"blob_ref": "11111111-1111-4111-8111-111111111111", "mode": "inline_content", "sha256": "a" * 64}


@pytest.mark.parametrize("reviewed_mode", [None, "bind_source"])
def test_set_pipeline_blob_path_source_keeps_the_reviewed_mode_and_adds_none(tmp_path: Path, reviewed_mode: str | None) -> None:
    engine, session_id, _other_session, blobs = candidates._reviewed_source_harness(tmp_path)
    blob = blobs[0]
    facts = candidates._reviewed_source_facts(blob_id=str(blob.id))
    reviewed = next(iter(facts["reviewed_sources"].values()))
    if reviewed_mode is not None:
        reviewed["options"]["mode"] = reviewed_mode
    assert reviewed["options"]["path"] == f"blob:{blob.id}"
    authority = resolve_reviewed_source_authority(
        engine=engine,
        session_id=session_id,
        user_id="review-owner",
        reviewed_facts=facts,
        expected_reviewed_anchor_hash=reviewed_anchor_hash(facts),
    )

    candidate = build_set_pipeline_candidate(
        candidates._named_reviewed_pipeline(tmp_path, facts),
        candidates._empty_state(),
        candidates._trained_context(
            data_dir=tmp_path,
            session_engine=engine,
            session_id=session_id,
            user_id="review-owner",
            reviewed_source_authority=authority,
        ),
    )

    assert candidate.acceptable is True, candidate.result.to_dict()
    state: dict[str, Any] = candidate.result.updated_state.to_dict()
    options = state["sources"]["source"]["options"]
    # THE MEASURED ANSWER: storage path + blob_ref, and ``mode`` exactly as reviewed.
    assert options["path"] == blob.storage_path
    assert options["blob_ref"] == str(blob.id)
    assert ("mode" in options) is (reviewed_mode is not None)
    # Either way the source binding is not a file marker, and it does not hide one.
    config = {
        "sources": state["sources"],
        "transforms": [{"name": "rate", "plugin": "llm", "options": {"prompt_template": dict(_PROMPT_MARKER)}}],
    }
    assert [ref.field_path for ref in _discover_blob_content_refs(config)] == ["node:rate.options.prompt_template"]
