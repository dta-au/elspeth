"""Regression checks for the public README release surface."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH
from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH

REPO_ROOT = Path(__file__).resolve().parents[3]
README = REPO_ROOT / "README.md"


def _readme_text() -> str:
    return README.read_text(encoding="utf-8")


def test_readme_operational_cutover_states_the_live_schema_epochs() -> None:
    """Current candidate guidance names its epochs and preservation policy."""
    version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    readme = _readme_text()
    heading = f"\n## What changed in {version}\n"
    assert readme.count(heading) == 1
    current_section = readme.split(heading, maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    text = " ".join(current_section.split())

    assert f"session epoch {SESSION_SCHEMA_EPOCH} and Landscape epoch {SQLITE_SCHEMA_EPOCH}." in text
    assert "preserve them unchanged and configure explicitly new compatible databases" in text
    assert "does not migrate either store in place or authorize downgrading these stores" in text
    assert "compatibility policy has no fixed expiry" in text
    assert f"Package metadata is {version};" in text
    assert "the package version alone does not establish qualification or image publication" in text
    assert "Automatic image publication from main is held" in text
    assert "verify the release tag and image digest" in text


def test_readme_release_links_resolve() -> None:
    text = _readme_text()
    linked_paths = set(re.findall(r"\]\((docs/release/[^)#]+)", text))

    assert linked_paths, "README should reference at least one docs/release/ document"
    for relative_path in linked_paths:
        assert (REPO_ROOT / relative_path).exists(), relative_path
