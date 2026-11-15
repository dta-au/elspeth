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


def _section(readme: str, heading: str) -> str:
    assert readme.count(heading) == 1, heading
    section = readme.split(heading, maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    return " ".join(section.split())


def test_readme_operational_cutover_states_the_live_schema_epochs() -> None:
    """Current release guidance names its epochs and preservation policy."""
    version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    readme = _readme_text()
    text = _section(readme, f"\n## What changed in {version}\n")

    assert f"Session epoch {SESSION_SCHEMA_EPOCH} and Landscape epoch {SQLITE_SCHEMA_EPOCH} are unchanged from the previous release" in text
    assert "this release adds no schema-epoch cutover" in text
    assert "existing session and Landscape stores need no action" in text
    assert "Backwards compatibility is permitted and considered from 0.8.5 within explicitly supported boundaries" in text
    assert "authorizes no in-place migration, reset or downgrade" in text
    assert f"Package metadata is {version};" in text
    assert "the package version alone does not establish qualification or image publication" in text
    assert "Automatic image publication from main is held" in text
    assert "verify the release tag and image digest" in text
    assert "0.8.14 moves ELSPETH to Python 3.14" in text
    assert "Python 3.14 is now required for `elspeth`, `elspeth-lints` and the LLM gateway" in text
    assert "3.12 and 3.13 are no longer supported or tested" in text
    assert "Startup refuses `JSON_LOGS=true`" in text
    assert "a run checkpointed under 3.13 refuses to resume under 3.14" in text
    assert "drain in-flight runs and pending approvals before upgrading" in text
    assert "it has no `/usr/bin/python3`" in text
    assert "the repository ruleset must be updated to match" in text


def test_readme_keeps_the_0_8_13_context_budget_as_a_historical_pin() -> None:
    """The 0.8.13 section keeps its own statements and no current-release claim."""
    text = _section(_readme_text(), "\n## Previously in 0.8.13\n")

    assert "0.8.13 adds the Composer context budget" in text
    assert "`ELSPETH_WEB__COMPOSER_CONTEXT_MAX_PROMPT_TOKENS` is required and has no default" in text
    assert "the web server does not start without it" in text
    assert "The default of `ELSPETH_WEB__COMPOSER_PLANNER_MAX_REQUEST_BYTES` drops from 2 MiB to 960 KiB" in text
    assert "The budget does not bound every Composer request" in text
    assert "Advisor and review calls are not yet covered ([#364](https://github.com/dta-au/elspeth/issues/364))" in text
    assert "Package metadata is" not in text


def test_readme_keeps_the_0_8_12_remediation_as_a_historical_pin() -> None:
    """The 0.8.12 section keeps its own statements and no current-release claim."""
    text = _section(_readme_text(), "\n## Previously in 0.8.12\n")

    assert "0.8.12 is the Composer tool remediation" in text
    assert "This contains the defect rather than redesigning it; auto-commit sessions and blob-only proposals are not covered" in text
    assert "MCP file paths are confined to the configured data directory" in text
    assert "Package metadata is" not in text


def test_readme_keeps_the_0_8_10_cutover_as_a_historical_pin() -> None:
    """The 0.8.10 section keeps its own epoch cutover statements."""
    text = _section(_readme_text(), "\n## Previously in 0.8.10\n")

    assert f"Session epoch {SESSION_SCHEMA_EPOCH} replaces session epoch 77" in text
    assert "retains its committed graph-review authority" in text
    assert f"Landscape epoch {SQLITE_SCHEMA_EPOCH} replaces epoch 49" in text
    assert "Preserve epoch 75, both epoch 76 session-store contracts, epoch 77 session stores and epoch 49 Landscape stores" in text
    assert f"use a separately provisioned epoch {SESSION_SCHEMA_EPOCH} session store" in text
    assert f"epoch {SQLITE_SCHEMA_EPOCH} Landscape store" in text
    assert "Keep old workers isolated from the new stores" in text
    assert "Compatible stores still require exact schema admission" in text
    assert "unsupported stores are preserved and refused before incompatible writes" in text
    assert "Quota API quantities now use decimal strings for exact signed-64-bit values" in text
    assert "ELSPETH_WEB__TRUSTED_PROXY_HOSTS" in text


def test_readme_release_links_resolve() -> None:
    text = _readme_text()
    linked_paths = set(re.findall(r"\]\((docs/release/[^)#]+)", text))

    assert linked_paths, "README should reference at least one docs/release/ document"
    for relative_path in linked_paths:
        assert (REPO_ROOT / relative_path).exists(), relative_path
