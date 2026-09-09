"""Regression checks for the public README release surface."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH
from elspeth.web._acceptance_common.schema_facts import (
    _ROLLBACK_BASELINE_LANDSCAPE_EPOCH,
    _ROLLBACK_BASELINE_SESSION_EPOCH,
)
from elspeth.web.composer.guided.state_machine import GUIDED_SESSION_SCHEMA_VERSION
from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH

REPO_ROOT = Path(__file__).resolve().parents[3]
README = REPO_ROOT / "README.md"


def _readme_text() -> str:
    return README.read_text(encoding="utf-8")


def test_readme_operational_cutover_states_the_live_schema_epochs() -> None:
    """The public README's cutover paragraph must name the epochs the build enforces.

    The README told operators the session store moves to epoch 51 and
    Landscape to 36 while the shipped constants said 53 and 37: five session
    epochs and one Landscape epoch landed after the paragraph was written and
    nothing bound the prose to the constants, unlike the website and the
    staging runbook. Derive every number here so an epoch bump reds this
    test instead of publishing a stale cutover instruction.
    """
    text = " ".join(_readme_text().split())
    version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    rollback_session = _ROLLBACK_BASELINE_SESSION_EPOCH
    rollback_landscape = _ROLLBACK_BASELINE_LANDSCAPE_EPOCH

    assert f"The session store moves from epoch {rollback_session} to {SESSION_SCHEMA_EPOCH};" in text
    assert f"guided schema moves to {GUIDED_SESSION_SCHEMA_VERSION}," in text
    assert f"Landscape moves from epoch {rollback_landscape} to {SQLITE_SCHEMA_EPOCH}." in text
    assert f"a Landscape store left at epoch {rollback_landscape}, and install {version}." in text


def test_readme_release_links_resolve() -> None:
    text = _readme_text()
    linked_paths = set(re.findall(r"\]\((docs/release/[^)#]+)", text))

    assert linked_paths, "README should reference at least one docs/release/ document"
    for relative_path in linked_paths:
        assert (REPO_ROOT / relative_path).exists(), relative_path
