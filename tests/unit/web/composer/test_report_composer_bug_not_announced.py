"""Suspected-bug reports are for the maintainers, but a correction still reaches the user.

The skill and the ``report_composer_bug`` description told the model to file a
report and continue, and said nothing about the reply, so the model told the
user it had filed one. Both texts now say not to mention the report, and say
that a correction to something the model told the user earlier is still said.
"""

from __future__ import annotations

from pathlib import Path

from elspeth.web.composer.tools import get_tool_definitions

_SKILL = Path(__file__).parents[4] / "src/elspeth/web/composer/skills/pipeline_composer.md"


def _feedback_paragraph() -> str:
    text = _SKILL.read_text(encoding="utf-8")
    [paragraph] = [block for block in text.split("\n\n") if block.startswith("Product feedback:")]
    return " ".join(paragraph.split())


def _report_description() -> str:
    [description] = [d["description"] for d in get_tool_definitions() if d["name"] == "report_composer_bug"]
    return description


def test_skill_keeps_reports_out_of_the_reply_but_not_corrections() -> None:
    paragraph = _feedback_paragraph()
    assert "do not mention them to the user" in paragraph
    assert "never say you filed or recorded one" in paragraph
    assert "still tell the user the correction" in paragraph


def test_tool_description_keeps_reports_out_of_the_reply_but_not_corrections() -> None:
    description = _report_description()
    assert "do not mention them to the user" in description
    assert "still tell the user any correction" in description


def test_the_instruction_to_file_and_continue_is_unchanged() -> None:
    """Control: the instruction to file a report and continue is unchanged."""
    assert "call `report_composer_bug` once" in _feedback_paragraph()
    assert "Report each distinct mismatch once, then continue the user's task" in _report_description()
