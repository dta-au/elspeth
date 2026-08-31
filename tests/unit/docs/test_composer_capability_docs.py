"""Composer capability-parity documentation contract."""

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
        "### Choosing between guided and freeform", maxsplit=1
    )[0]
    return Counter(re.findall(r"^- \*\*[^*]+\*\* \(`([^`]+)`\)", section, flags=re.MULTILINE))


def _parity_fixture_classes() -> Counter[str]:
    return Counter(json.loads(path.read_text(encoding="utf-8"))["class"] for path in PARITY_FIXTURES.glob("*.json"))


def test_user_manual_states_interaction_not_capability_distinction() -> None:
    manual = _manual()
    assert "differ in interaction, not in capability" in manual
    # Both surfaces resolve to one shared planner and one canonical draft.
    assert "talking to the **same** pipeline planner" in manual
    assert "produce the same canonical pipeline draft" in manual
    assert "the same canonical structures are available on both surfaces" in manual


def test_user_manual_lists_supported_canonical_structures() -> None:
    assert _documented_structure_classes() == _parity_fixture_classes()


def test_user_manual_describes_tutorial_as_shared_planner_profile() -> None:
    manual = _manual()
    assert "guided workflow profile" in manual
    assert "same staged planner and the same proposal schema" in manual
    assert "not a separate or reduced-capability mode" in manual
