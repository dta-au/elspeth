"""set_pipeline: inline files written by the call meet the per-file cap and the pipeline total.

The refusal keeps its public code (``BLOB_QUOTA_EXCEEDED``) and now says which
limit was hit, with the figures.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from elspeth.core.blobs_inline import BLOB_INLINE_AGGREGATE_BYTE_CAP, BLOB_INLINE_PER_REF_BYTE_CAP
from elspeth.web.composer.tools import build_set_pipeline_candidate
from tests.unit.web.composer.test_set_pipeline_candidate import (
    _empty_state,
    _session_with_user_message,
    _structured_llm_args,
    _trained_context,
)

OLD_AGGREGATE_CAP = 1024 * 1024


def _sizes_totalling(total: int) -> list[int]:
    full, remainder = divmod(total, BLOB_INLINE_PER_REF_BYTE_CAP)
    return [BLOB_INLINE_PER_REF_BYTE_CAP] * full + ([remainder] if remainder else [])


def _ascii(sizes: list[int]) -> list[str]:
    return [chr(97 + index % 26) * size for index, size in enumerate(sizes)]


def _candidate(tmp_path: Path, contents: list[str]) -> Any:
    """One LLM node whose query templates are new inline files holding ``contents``."""
    engine, session_id, message_id = _session_with_user_message()
    args = _structured_llm_args(tmp_path)
    args["source"]["options"]["path"] = str(tmp_path / "blobs" / session_id / "input.csv")
    (template_query,) = args["nodes"][0]["options"]["queries"]
    args["nodes"][0]["options"]["queries"] = [
        {
            **template_query,
            "name": f"q{index}",
            "template": {"inline_blob": {"filename": f"q{index}.txt", "mime_type": "text/plain", "content": content}},
        }
        for index, content in enumerate(contents)
    ]
    context = _trained_context(
        data_dir=tmp_path,
        session_engine=engine,
        session_id=session_id,
        user_message_id=message_id,
        user_message_content="Please prepare the pipeline.",
        composer_model_identifier="test-model",
        composer_model_version="test-version",
        composer_provider="test-provider",
        composer_skill_hash="a" * 64,
        tool_arguments_hash="b" * 64,
    )
    return build_set_pipeline_candidate(args, _empty_state(), context)


def _size_refusals(candidate: Any) -> list[tuple[str | None, str]]:
    return [
        (entry.rejected_component, entry.message)
        for entry in candidate.result.validation.errors
        if entry.error_code == "BLOB_QUOTA_EXCEEDED"
    ]


@pytest.mark.parametrize(
    "total",
    [
        pytest.param(OLD_AGGREGATE_CAP + 1, id="old-cap-plus-one"),
        pytest.param(BLOB_INLINE_AGGREGATE_BYTE_CAP, id="exactly-cap"),
    ],
)
def test_new_files_up_to_the_total_are_accepted(tmp_path: Path, total: int) -> None:
    sizes = _sizes_totalling(total)

    candidate = _candidate(tmp_path, _ascii(sizes))

    assert candidate.acceptable, candidate.result.validation.errors
    assert [len(prepared.content_bytes) for _path, prepared in candidate.prepared_prompt_blobs] == sizes


def test_new_files_one_byte_over_the_total_are_refused_with_the_figures(tmp_path: Path) -> None:
    sizes = [*_sizes_totalling(BLOB_INLINE_AGGREGATE_BYTE_CAP), 1]

    candidate = _candidate(tmp_path, _ascii(sizes))

    assert not candidate.acceptable
    assert _size_refusals(candidate) == [
        (
            None,
            f"The new inline files in this call total {BLOB_INLINE_AGGREGATE_BYTE_CAP + 1} bytes across {len(sizes)} files; "
            f"the limit for all file-held text in one pipeline is {BLOB_INLINE_AGGREGATE_BYTE_CAP} bytes. "
            "Remove a file or shorten the largest ones.",
        )
    ]


# The argument model caps ``content`` at 262,144 CHARACTERS, so only text with
# multi-byte characters reaches the per-file BYTE cap with an admitted argument.
_TWO_BYTE = "\u00e9"


@pytest.mark.parametrize(
    ("content", "refused"),
    [
        pytest.param(_TWO_BYTE * (BLOB_INLINE_PER_REF_BYTE_CAP // 2), False, id="exactly-per-file-cap"),
        pytest.param(_TWO_BYTE * (BLOB_INLINE_PER_REF_BYTE_CAP // 2) + "a", True, id="per-file-cap-plus-one"),
    ],
)
def test_per_file_cap_is_unchanged_and_named(tmp_path: Path, content: str, refused: bool) -> None:
    size = len(content.encode("utf-8"))

    candidate = _candidate(tmp_path, [content])

    if not refused:
        assert size == BLOB_INLINE_PER_REF_BYTE_CAP
        assert candidate.acceptable, candidate.result.validation.errors
        return
    assert size == BLOB_INLINE_PER_REF_BYTE_CAP + 1
    assert not candidate.acceptable
    assert _size_refusals(candidate) == [
        (
            None,
            f"Inline file over the per-file limit of {BLOB_INLINE_PER_REF_BYTE_CAP} bytes: "
            f"node:classify.options.queries.q0.template is {size} bytes. Shorten each named file.",
        )
    ]
