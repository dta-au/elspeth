"""YAML import of a pipeline whose file-held text is over the total cap: a typed 413, never a 500.

The card writer refuses a prompt file over an inline cap with
``PromptFileTooLargeError``; the import route let it escape. Each test chains
LLM steps whose ``system_prompt`` is a separate uploaded file, so the step
whose file crosses the total is the one whose review card cannot be written.
Everything runs against the real blob service, session service and card writer.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

import pytest

from elspeth.core.blobs_inline import BLOB_INLINE_AGGREGATE_BYTE_CAP, BLOB_INLINE_PER_REF_BYTE_CAP
from tests.unit.web.sessions.test_import_source_binding_prompt_file import CSV, SINK_BINDING, _Env

OLD_AGGREGATE_CAP = 1024 * 1024


def _sizes_totalling(total: int) -> list[int]:
    full, remainder = divmod(total, BLOB_INLINE_PER_REF_BYTE_CAP)
    return [BLOB_INLINE_PER_REF_BYTE_CAP] * full + ([remainder] if remainder else [])


def _chained_llm_yaml(markers: list[tuple[str, str]]) -> str:
    """One csv source, one llm step per (blob id, sha256) holding it as its system prompt, one json sink."""
    steps = []
    for index, (blob_id, digest) in enumerate(markers):
        upstream = "reviews" if index == 0 else f"step_{index - 1}_out"
        downstream = "main" if index == len(markers) - 1 else f"step_{index}_out"
        steps.append(
            f"- name: step_{index}\n  plugin: llm\n  input: {upstream}\n  on_success: {downstream}\n  on_error: discard\n"
            "  options:\n    profile: task-role\n    required_input_fields:\n    - review\n"
            f"    response_field: rating_{index}\n    schema:\n      mode: observed\n"
            "    prompt_template: \"Rate this review 1 to 5. Review: {{ row['review'] }}\"\n"
            f"    system_prompt:\n      blob_ref: {blob_id}\n      mode: inline_content\n      sha256: {digest}\n"
        )
    return (
        "sources:\n  source:\n    plugin: csv\n    on_success: reviews\n    on_validation_failure: discard\n    options:\n"
        "      schema:\n        mode: flexible\n        fields:\n        - 'review: str'\n"
        "transforms:\n" + "".join(steps) + "sinks:\n  main:\n    plugin: json\n    on_write_failure: discard\n    options:\n"
        "      format: json\n      mode: write\n      collision_policy: auto_increment\n      schema:\n        mode: observed\n"
    )


async def _import(env: _Env, session_id: uuid.UUID, sizes: list[int]) -> Any:
    csv = await env.upload(session_id, "reviews.csv", CSV, "text/csv")
    markers = []
    for index, size in enumerate(sizes):
        content = bytes([97 + index % 26]) * size
        blob = await env.upload(session_id, f"system_{index}.txt", content, "text/plain")
        markers.append((str(blob.id), hashlib.sha256(content).hexdigest()))
    return env.import_yaml(
        session_id,
        {"yaml": _chained_llm_yaml(markers), "source_blob_ids": {"source": str(csv.id)}, "sink_path_bindings": SINK_BINDING},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "total",
    [
        pytest.param(OLD_AGGREGATE_CAP + 1, id="old-cap-plus-one"),
        pytest.param(BLOB_INLINE_AGGREGATE_BYTE_CAP, id="exactly-cap"),
    ],
)
async def test_import_up_to_the_cap_is_admitted(tmp_path: Path, total: int) -> None:
    """Admitted means imported: the state is saved. Whether it then validates is the YAML bound's business."""
    env = _Env(tmp_path)
    session_id = await env.session()

    response = await _import(env, session_id, _sizes_totalling(total))

    assert response.status_code == 200, response.text[:2000]
    assert await env.service.get_current_state(session_id) is not None


@pytest.mark.asyncio
async def test_import_one_byte_over_the_cap_is_a_typed_413_with_the_figures(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    session_id = await env.session()
    sizes = [*_sizes_totalling(BLOB_INLINE_AGGREGATE_BYTE_CAP), 1]

    response = await _import(env, session_id, sizes)

    assert response.status_code == 413, response.text[:2000]
    assert response.json()["detail"] == {
        "error_code": "prompt_file_too_large",
        "message": (
            f"File-held text totals {BLOB_INLINE_AGGREGATE_BYTE_CAP + 1} bytes across {len(sizes)} file bindings; "
            f"the limit is {BLOB_INLINE_AGGREGATE_BYTE_CAP} bytes. A file bound in more than one place counts once per place. "
            "Remove a binding or shorten the largest files. Nothing was imported."
        ),
    }
    assert await env.service.get_current_state(session_id) is None
    assert await env.pending(session_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bindings", "admitted"),
    [
        pytest.param(BLOB_INLINE_AGGREGATE_BYTE_CAP // BLOB_INLINE_PER_REF_BYTE_CAP, True, id="cap-exactly"),
        pytest.param(BLOB_INLINE_AGGREGATE_BYTE_CAP // BLOB_INLINE_PER_REF_BYTE_CAP + 1, False, id="one-more-binding"),
    ],
)
async def test_import_counts_one_file_once_per_binding(tmp_path: Path, bindings: int, admitted: bool) -> None:
    env = _Env(tmp_path)
    session_id = await env.session()
    csv = await env.upload(session_id, "reviews.csv", CSV, "text/csv")
    content = b"r" * BLOB_INLINE_PER_REF_BYTE_CAP
    blob = await env.upload(session_id, "system.txt", content, "text/plain")
    marker = (str(blob.id), hashlib.sha256(content).hexdigest())

    response = env.import_yaml(
        session_id,
        {"yaml": _chained_llm_yaml([marker] * bindings), "source_blob_ids": {"source": str(csv.id)}, "sink_path_bindings": SINK_BINDING},
    )

    if admitted:
        assert response.status_code == 200, response.text[:2000]
        return
    assert response.status_code == 413, response.text[:2000]
    assert response.json()["detail"]["message"].startswith(
        f"File-held text totals {bindings * BLOB_INLINE_PER_REF_BYTE_CAP} bytes across {bindings} file bindings; "
    )


@pytest.mark.asyncio
async def test_import_of_one_file_over_the_per_file_cap_is_a_typed_413(tmp_path: Path) -> None:
    """The per-file cap (unchanged) reaches the same handler; it names that cap, not the total."""
    env = _Env(tmp_path)
    session_id = await env.session()

    response = await _import(env, session_id, [BLOB_INLINE_PER_REF_BYTE_CAP + 1])

    assert response.status_code == 413, response.text[:2000]
    detail = response.json()["detail"]
    assert detail["error_code"] == "prompt_file_too_large"
    assert f"the {BLOB_INLINE_PER_REF_BYTE_CAP}-byte (256 KiB) inline limit" in detail["message"]
    assert detail["message"].endswith("Nothing was imported.")
    assert await env.service.get_current_state(session_id) is None
