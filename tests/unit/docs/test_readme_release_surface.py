"""Regression checks for the public README release surface."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH
from elspeth.web.composer.guided.state_machine import GUIDED_SESSION_SCHEMA_VERSION
from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH

REPO_ROOT = Path(__file__).resolve().parents[3]
README = REPO_ROOT / "README.md"


def _readme_text() -> str:
    return README.read_text(encoding="utf-8")


def test_readme_operational_cutover_states_the_live_schema_epochs() -> None:
    """Current release guidance must name the build's epochs and install version.

    Historical release sections cannot satisfy this check. The predecessor
    epochs are 0.8.0's released shape, independent of the older Scenario B
    acceptance rollback baseline.
    """
    version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    readme = _readme_text()
    heading = f"\n## What changed in {version}\n"
    assert readme.count(heading) == 1
    current_section = readme.split(heading, maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    text = " ".join(current_section.split())

    assert f"session epoch 53 to {SESSION_SCHEMA_EPOCH} " in text
    assert f"Landscape epoch 38 to {SQLITE_SCHEMA_EPOCH};" in text
    assert f"guided schema remains at {GUIDED_SESSION_SCHEMA_VERSION}." in text
    assert "recreate both stale databases in the same service-stop window" in text
    assert f"install {version}." in text


def test_readme_release_links_resolve() -> None:
    text = _readme_text()
    linked_paths = set(re.findall(r"\]\((docs/release/[^)#]+)", text))

    assert linked_paths, "README should reference at least one docs/release/ document"
    for relative_path in linked_paths:
        assert (REPO_ROOT / relative_path).exists(), relative_path
