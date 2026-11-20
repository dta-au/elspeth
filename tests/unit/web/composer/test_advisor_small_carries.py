"""Advisor instructions preserve the boundary of a requested edit."""

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
