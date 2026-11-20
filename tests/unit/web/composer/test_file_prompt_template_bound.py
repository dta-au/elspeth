"""Execution validation of a FILE-held prompt against the 64 KiB template bound.

The authoring probe cannot see a file's text, so execution validation is where
the bound applies to it (and run admission runs the same plugin validation).
A 20 KB prompt, refused under the old 16 KiB bound, now validates; one byte
over 64 KiB is refused with the position, size and limit, not as a syntax fault.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.web.composer.test_llm_file_prompt_probe import _execution_validate, _marker, _state

LIMIT = 64 * 1024


def _prompt(size: int) -> str:
    head = "Summarise this customer review in one sentence: {{ row.review }}. "
    return head + "x" * (size - len(head))


@pytest.mark.parametrize("size", [pytest.param(20_000, id="twenty-kb"), pytest.param(LIMIT, id="exactly-the-bound")])
def test_file_prompt_up_to_the_bound_validates(tmp_path: Path, size: int) -> None:
    text = _prompt(size)
    session_id, state = _state(tmp_path, prompt_template=_marker(text))

    result = _execution_validate(tmp_path, session_id, state, text)

    assert result.is_valid, [error.message for error in result.errors]


def test_file_prompt_over_the_bound_is_refused_by_position_size_and_limit(tmp_path: Path) -> None:
    text = _prompt(LIMIT + 1)
    session_id, state = _state(tmp_path, prompt_template=_marker(text))

    result = _execution_validate(tmp_path, session_id, state, text)

    assert not result.is_valid
    (error,) = result.errors
    assert (
        f"prompt_template is {LIMIT + 1} bytes; the limit for one prompt template is {LIMIT} bytes. "
        "Shorten it, or move reference text into a file_inject step and read it from the row."
    ) in error.message
    assert "Invalid Jinja2 template" not in error.message
    assert {check.name: check.passed for check in result.checks}["plugin_instantiation"] is False
