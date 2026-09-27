"""Composer capability documentation contract."""

import json
import re
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
USER_MANUAL = REPO_ROOT / "docs/guides/user-manual.md"
PARITY_FIXTURES = REPO_ROOT / "evals/composer-parity/fixtures"


def _manual() -> str:
    """User manual with newline-wrapping collapsed so phrase asserts survive reflow."""
    return " ".join(USER_MANUAL.read_text(encoding="utf-8").split())


def _documented_structure_classes() -> Counter[str]:
    manual = USER_MANUAL.read_text(encoding="utf-8")
    section = manual.split("### Supported pipeline structures", maxsplit=1)[1].split(
        "### Validation, interpretation, and sign-off", maxsplit=1
    )[0]
    return Counter(re.findall(r"^- \*\*[^*]+\*\* \(`([^`]+)`\)", section, flags=re.MULTILINE))


def _parity_fixture_classes() -> Counter[str]:
    return Counter(json.loads(path.read_text(encoding="utf-8"))["class"] for path in PARITY_FIXTURES.glob("*.json"))


def test_user_manual_states_freeform_is_the_composer_authoring_path() -> None:
    manual = _manual()
    assert "New sessions use freeform conversation" in manual
    assert "The LLM proposes the structure" in manual
    assert "Follow-up messages can refine the draft" in manual
    assert "guided" not in manual.lower()


def test_user_manual_lists_supported_canonical_structures() -> None:
    assert _documented_structure_classes() == _parity_fixture_classes()


def test_user_manual_describes_tutorial_as_ordinary_freeform_with_capstone() -> None:
    manual = _manual()
    assert "ordinary freeform Composer" in manual
    assert "same planner, proposal review, and validation as any other session" in manual
    assert "Continue through Run and Audit" in manual
    assert "Graduation then hands you to ordinary authoring" in manual
