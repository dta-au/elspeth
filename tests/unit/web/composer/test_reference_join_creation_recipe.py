"""The skill says how to create a ``reference_join`` node with its table, and an absent table says "required".

The skill only taught re-pointing an existing table with ``wire_blob_inline_ref``;
on a node the call creates there is nothing to re-point, so the model wrote the
table as literal text or guessed a marker. The ``file_inject`` paragraph already
gives the creation marker; ``reference_join`` now gives the same one for both
forms. Separately, an absent ``reference_content`` (or ``key_field``) was refused
as "Input should be a valid string", which reads as a wrong type, not a missing
field.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from elspeth.plugins.transforms.reference_join import ReferenceJoinConfig

_SKILL = Path(__file__).parents[4] / "src/elspeth/web/composer/skills/pipeline_composer.md"
_MARKER = '`{"blob_ref": "<blob_id>", "mode": "inline_content", "sha256": "<content_hash>"}`'


def _reference_join_paragraph() -> str:
    blocks = [" ".join(block.split()) for block in _SKILL.read_text(encoding="utf-8").split("\n\n")]
    matches = [block for block in blocks if "For a `reference_join` table" in block]
    assert len(matches) == 1, len(matches)
    return matches[0]


def _complete() -> dict[str, Any]:
    return {
        "schema": {"mode": "observed"},
        "reference_content": "code,label\nA,Alpha\n",
        "reference_format": "csv",
        "key_field": "code",
        "reference_key_name": "code",
        "output": {"label": "ref['label']"},
    }


def _errors(options: dict[str, Any]) -> list[tuple[tuple[str | int, ...], str]]:
    with pytest.raises(ValidationError) as caught:
        ReferenceJoinConfig.model_validate(options)
    return [(error["loc"], error["msg"]) for error in caught.value.errors()]


def test_skill_gives_the_creation_marker_for_both_table_forms() -> None:
    paragraph = _reference_join_paragraph()
    assert "When the call creates the node or the table" in paragraph
    creation = paragraph[paragraph.index("When the call creates the node or the table") :]
    assert _MARKER in creation
    assert "`content_hash` from `get_blob_metadata`" in creation


def test_skill_says_a_table_held_as_text_is_replaced_with_wire_blob_inline_ref() -> None:
    assert "If a table is already held as text, `wire_blob_inline_ref` at its path replaces the text" in _reference_join_paragraph()


@pytest.mark.parametrize("field", ["reference_content", "key_field"])
def test_an_absent_required_option_is_refused_as_required(field: str) -> None:
    options = _complete()
    del options[field]

    errors = _errors(options)

    assert ((field,), "Field required") in errors, errors
    assert all(message != "Input should be a valid string" for _, message in errors), errors


def test_an_explicit_null_is_still_refused_as_the_wrong_type() -> None:
    """Control: a value the author did set is passed through as written."""
    options = {**_complete(), "key_field": None}

    assert (("key_field",), "Input should be a valid string") in _errors(options)


def test_a_complete_single_table_config_still_validates() -> None:
    """Control: nothing the author set is dropped."""
    config = ReferenceJoinConfig.model_validate(_complete())

    assert config.reference_content == "code,label\nA,Alpha\n"
    assert config.on_miss == "fail"
