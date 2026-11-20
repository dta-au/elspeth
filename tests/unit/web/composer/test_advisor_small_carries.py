"""Advisor instructions preserve the boundary of a requested edit."""

from pathlib import Path

import elspeth.web.composer as composer_package
from elspeth.web.composer.advisor_context import _ADVISOR_CHECKPOINT_SYSTEM_INSTRUCTIONS
from elspeth.web.composer.tools._common import _STEP_DESCRIPTION_DESCRIPTION


def test_advisor_derives_scope_from_user_and_contracts() -> None:
    instructions = _ADVISOR_CHECKPOINT_SYSTEM_INSTRUCTIONS
    assert "not earlier assistant choices" in instructions
    assert "unrequested changes to existing descriptions, options or unrelated routes" in instructions
    assert "Node on_error is separate from sink options" in instructions
    assert "explicit supported discard policy is not a missing error policy" in instructions
    assert "required-control policy or routing contracts" in instructions
    assert "Preserve it during routing-only edits" in _STEP_DESCRIPTION_DESCRIPTION


def test_capability_skill_states_what_a_replacing_upsert_node_keeps() -> None:
    skill = (Path(composer_package.__file__).parent / "skills" / "pipeline_capabilities.md").read_text(encoding="utf-8")
    paragraph = " ".join(skill.split("For an existing transform's routing edit,", 1)[1].split("\n\n", 1)[0].split())
    assert "an omitted `description` is dropped" in paragraph
    assert "the supplied `options` replace the old ones" in paragraph
    assert "an omitted `on_error` keeps the existing choice" in paragraph
    assert "does not preserve its previous value" not in paragraph
