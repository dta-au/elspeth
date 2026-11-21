"""BLOB_QUOTA_EXCEEDED guidance is true for every refusal that carries the code.

``set_pipeline`` gives this code for three refusals: an inline file over the
per-file limit, the call's inline files over the pipeline total for file-held
text, and the session's blob storage quota. The message names which. The
guidance used to describe only the session quota, so a model refused for the
text total was told to resolve storage, the wrong repair.

Expected phrases are spelled here so the file collects on the tree before the change.
"""

from __future__ import annotations

from pathlib import Path

from elspeth.core.blobs_inline import BLOB_INLINE_AGGREGATE_BYTE_CAP
from elspeth.web.composer.tools.generation import explain_validation_code
from tests.unit.web.composer.test_set_pipeline_inline_file_caps import _ascii, _candidate, _size_refusals, _sizes_totalling


def _guidance() -> tuple[str, str]:
    guidance = explain_validation_code("BLOB_QUOTA_EXCEEDED")
    assert guidance is not None
    return guidance


def test_the_explanation_names_all_three_causes() -> None:
    explanation, _fix = _guidance()

    for cause in ("per-file limit", "pipeline total for file-held text", "session's blob storage quota"):
        assert cause in explanation, cause


def test_the_repair_for_a_text_limit_is_to_shorten_the_named_files_and_storage_only_for_the_quota() -> None:
    _explanation, fix = _guidance()

    assert "For a file or pipeline-total limit, shorten or remove the files the message names." in fix
    assert "For the session quota" in fix
    assert "Do not silently drop rows" in fix


def test_a_text_total_refusal_carries_the_code_whose_guidance_covers_it(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path, _ascii([*_sizes_totalling(BLOB_INLINE_AGGREGATE_BYTE_CAP), 1]))

    ((_component, message),) = _size_refusals(candidate)
    assert "the limit for all file-held text in one pipeline" in message
    explanation, _fix = _guidance()
    assert "pipeline total for file-held text" in explanation
