"""Execution validation names the STEP whose prompt file is over the template bound.

Plugin construction refused such a file too, but its entry named the plugin
('llm'), so with two llm steps the user and the model could not tell which file
to shorten. Expected texts are spelled here so the file collects on the tree
before the change.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer import yaml_generator
from elspeth.web.execution.validation import validate_pipeline_for_trained_operator
from tests.unit.web.composer.test_llm_file_prompt_probe import GOOD_PROMPT, _state
from tests.unit.web.execution.test_validate_blob_inline import _ready_blob_record

LIMIT = 64 * 1024
FIRST = UUID("0d5c3f4e-1a2b-4c3d-8e9f-0a1b2c3d4e5f")
SECOND = UUID("7e6d5c4b-3a2f-4e1d-9c8b-7a6f5e4d3c2b")
OVER = ("Rate {{ row.review }}. " + "x" * LIMIT).encode()
SINK = {"mode": "flexible", "fields": ["review: str", "summary: str", "verdict: str"]}


def _refusal(position: str, size: int) -> str:
    return (
        f"{position} is {size} bytes; the limit for one prompt template is {LIMIT} bytes. "
        "Shorten it, or move reference text into a file_inject step and read it from the row."
    )


def _marker(blob_id: UUID, raw: bytes) -> dict[str, str]:
    return {"blob_ref": str(blob_id), "mode": "inline_content", "sha256": hashlib.sha256(raw).hexdigest()}


def _renamed(value: Any, old: str, new: str) -> Any:
    if isinstance(value, dict):
        return {key: _renamed(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_renamed(item, old, new) for item in value]
    if isinstance(value, str):
        return value.replace(f":{old}", f":{new}")
    return value


def _validate(tmp_path: Path, files: dict[UUID, bytes], **state_args: Any) -> tuple[Any, Any]:
    session_id, state = _state(tmp_path, sink_schema=SINK, **state_args)
    records = {
        blob_id: replace(
            _ready_blob_record(session_id=session_id, blob_id=blob_id, size_bytes=len(raw)), content_hash=hashlib.sha256(raw).hexdigest()
        )
        for blob_id, raw in files.items()
    }
    return state, lambda current: validate_pipeline_for_trained_operator(
        current,
        SimpleNamespace(data_dir=tmp_path),
        yaml_generator,
        blob_get_metadata=lambda blob_id: records[blob_id],
        blob_get_content=lambda blob_id: (records[blob_id], files[blob_id]),
        session_id=str(session_id),
    )


def _two_steps(state: Any, second_prompt: dict[str, str]) -> Any:
    """csv -> llm 'classify' (summary) -> llm 'second' (verdict) -> json."""
    first = state.nodes[0]
    options = _renamed(deep_thaw(first.options), first.id, "second")
    options.update({"prompt_template": second_prompt, "response_field": "verdict"})
    second = replace(first, id="second", input="mid", on_success=first.on_success, options=options)
    return replace(state, nodes=(replace(first, on_success="mid"), second))


def _entries(result: Any) -> list[tuple[str | None, str | None, str]]:
    return [(error.component_id, error.error_code, error.message) for error in result.errors]


def test_two_llm_steps_the_entry_names_the_step_whose_file_is_over_the_bound(tmp_path: Path) -> None:
    small = GOOD_PROMPT.encode()
    state, validate = _validate(tmp_path, {FIRST: small, SECOND: OVER}, prompt_template=_marker(FIRST, small))

    result = validate(_two_steps(state, _marker(SECOND, OVER)))

    assert not result.is_valid
    assert ("second", "plugin_options_invalid", _refusal("prompt_template on step second", len(OVER))) in _entries(result)
    assert not any(component == "classify" for component, _code, _message in _entries(result))


def test_a_file_at_the_bound_is_not_refused_for_its_size(tmp_path: Path) -> None:
    at_bound = ("Rate {{ row.review }}. " + "x" * LIMIT).encode()[:LIMIT]
    assert len(at_bound) == LIMIT
    state, validate = _validate(tmp_path, {FIRST: at_bound}, prompt_template=_marker(FIRST, at_bound))

    result = validate(state)

    assert not any("the limit for one prompt template" in message for _c, _code, message in _entries(result)), _entries(result)


def test_a_query_template_file_over_the_bound_is_named_by_step_and_query() -> None:
    # Driven at the check itself: through execution validation an unreviewed
    # query file stops at interpretation_review_pending first.
    from elspeth.contracts.blobs_inline import BlobInlineRef
    from elspeth.web.execution._validation_materialization import _oversized_llm_template_files

    ref = BlobInlineRef(
        field_path="node:classify.options.queries.q1.template", blob_id=FIRST, sha256=hashlib.sha256(OVER).hexdigest(), encoding="utf-8"
    )

    errors = _oversized_llm_template_files({ref: OVER}, node_plugins={"classify": "llm"})

    assert [(error.component_id, error.error_code, error.message) for error in errors] == [
        ("classify", "plugin_options_invalid", _refusal("queries.q1.template on step classify", len(OVER)))
    ]
    assert _oversized_llm_template_files({ref: OVER}, node_plugins={"classify": "file_inject"}) == ()


def test_a_system_prompt_file_is_plain_text_and_gets_no_template_entry(tmp_path: Path) -> None:
    small = GOOD_PROMPT.encode()
    state, validate = _validate(
        tmp_path,
        {FIRST: small, SECOND: OVER},
        prompt_template=_marker(FIRST, small),
        options={"system_prompt": _marker(SECOND, OVER)},
    )

    result = validate(state)

    assert not any("the limit for one prompt template" in message for _c, _code, message in _entries(result)), _entries(result)
