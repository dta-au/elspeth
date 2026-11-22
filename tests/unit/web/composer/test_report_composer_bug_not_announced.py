"""Suspected-bug reports are not brought up unprompted, a direct question gets a plain answer, and a correction still reaches the user.

The skill and the ``report_composer_bug`` description told the model to file a
report and continue, and said nothing about the reply, so the model told the
user it had filed one. Report files stay visible in the user's file list, so
the texts must not make the model evade a user who asks what one is: they say
not to raise reports unprompted, to say plainly what a report file is when
asked, and that a correction to something the model told the user earlier is
still said.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from elspeth.web.composer.tools import get_tool_definitions

_SKILL = Path(__file__).parents[4] / "src/elspeth/web/composer/skills/pipeline_composer.md"

_UNPROMPTED = "do not bring them up unprompted"
_ASKED = (
    "If the user asks about a report file, say plainly what it is: a note the composer left for the maintainers "
    "about a suspected mismatch between its guidance and a tool, not verified."
)


def _feedback_paragraph() -> str:
    blocks = [" ".join(block.split()) for block in _SKILL.read_text(encoding="utf-8").split("\n\n")]
    matches = [block for block in blocks if block.startswith("Product feedback:")]
    assert len(matches) == 1, len(matches)
    return matches[0]


def _report_description() -> str:
    descriptions = [d["description"] for d in get_tool_definitions() if d["name"] == "report_composer_bug"]
    assert len(descriptions) == 1, len(descriptions)
    return descriptions[0]


_TEXTS = [pytest.param(_feedback_paragraph, id="skill"), pytest.param(_report_description, id="tool-description")]


@pytest.mark.parametrize("text", _TEXTS)
def test_reports_are_not_raised_unprompted(text: object) -> None:
    assert callable(text)
    assert _UNPROMPTED in text()


@pytest.mark.parametrize("text", _TEXTS)
def test_a_direct_question_gets_a_plain_answer(text: object) -> None:
    assert callable(text)
    assert _ASKED in text()


@pytest.mark.parametrize("text", _TEXTS)
def test_no_text_tells_the_model_to_deny_or_hide_a_report(text: object) -> None:
    assert callable(text)
    body = text()
    assert "never say you filed or recorded one" not in body
    assert "do not mention them to the user" not in body
    assert "Hiding the report" not in body and "hidden" not in body and "not listed" not in body


def test_skill_still_tells_the_user_a_correction() -> None:
    paragraph = _feedback_paragraph()
    assert "still tell the user the correction" in paragraph
    assert "a hint did not match what is stored and the setting is unchanged" in paragraph


def test_tool_description_still_tells_the_user_a_correction() -> None:
    assert "still tell the user any correction to something you told them earlier" in _report_description()


def test_the_instruction_to_file_and_continue_is_unchanged() -> None:
    """Control: the instruction to file a report and continue is unchanged."""
    assert "call `report_composer_bug` once" in _feedback_paragraph()
    assert "Report each distinct mismatch once, then continue the user's task" in _report_description()
