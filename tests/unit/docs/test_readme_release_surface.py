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


UPGRADING_HEADING = "\n## Upgrading from earlier releases\n"


def _upgrade_notes(readme: str, release: str) -> str:
    """One release's entry inside the README's upgrading section.

    Earlier releases no longer have a README section each: the section holds
    one ``###`` entry per release with only what an operator must do or know.
    Scoping each pin to its own entry keeps a fact from being satisfied by a
    different release's text.
    """
    assert readme.count(UPGRADING_HEADING) == 1, UPGRADING_HEADING
    upgrading = readme.split(UPGRADING_HEADING, maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    heading = f"\n### {release}\n"
    assert upgrading.count(heading) == 1, heading
    entry = upgrading.split(heading, maxsplit=1)[1].split("\n### ", maxsplit=1)[0]
    return " ".join(entry.split())


def test_readme_upgrading_section_states_the_standing_policy() -> None:
    """The preamble states the order of application and the no-migration policy once."""
    readme = _readme_text()
    preamble = " ".join(readme.split(UPGRADING_HEADING, maxsplit=1)[1].split("\n### ", maxsplit=1)[0].split())

    assert "Apply each release's upgrade actions in order, oldest first" in preamble
    assert "Backwards compatibility is permitted and considered from 0.8.5 within explicitly supported boundaries" in preamble
    assert "None of these releases authorizes an in-place migration, reset or downgrade" in preamble
    assert "](CHANGELOG.md)" in preamble


def test_readme_operational_cutover_states_the_live_schema_epochs() -> None:
    """Current release guidance names its epochs and preservation policy."""
    version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    readme = _readme_text()
    text = _section(readme, f"\n## What changed in {version}\n")

    assert f"{version} changes canonical hashing for new runs" in text
    assert "improves worker coordination and recovery" in text
    assert f"Session epoch {SESSION_SCHEMA_EPOCH} and Landscape epoch {SQLITE_SCHEMA_EPOCH} are unchanged from 0.8.17" in text
    assert "this release adds no schema-epoch cutover" in text
    assert "existing 0.8.17 session and Landscape stores open unchanged" in text
    assert "Finish CLI runs and interrupted web runs before upgrading" in text
    assert "Upgrading from 0.8.16 or earlier means applying the 0.8.17 and 0.8.16 upgrade notes first" in text
    assert "Backwards compatibility is permitted and considered from 0.8.5 within explicitly supported boundaries" in text
    assert "authorizes no in-place migration, reset or downgrade" in text
    assert f"Package metadata is {version};" in text
    assert "the package version alone does not establish qualification or image publication" in text
    assert "Automatic image publication from main is held" in text
    assert "verify the release tag and image digest" in text


def test_readme_keeps_the_0_8_17_upgrade_actions_as_a_historical_pin() -> None:
    """The 0.8.17 entry keeps its store and prompt-pinning guidance."""
    text = _upgrade_notes(_readme_text(), "0.8.17")

    assert "0.8.17 changed no schema epoch; 0.8.16 session and Landscape stores open unchanged" in text
    assert "Existing stores need no action" in text
    assert "Upgrading from 0.8.15 or earlier means applying the 0.8.16 upgrade notes first" in text
    assert "`get_pipeline_state` tool description and the advisor's evidence header gained one sentence each" in text
    assert "Package metadata is" not in text


def test_readme_keeps_the_0_8_16_upgrade_actions_as_a_historical_pin() -> None:
    """The 0.8.16 entry keeps its run-resume and inline-prompt actions and no current-release claim."""
    text = _upgrade_notes(_readme_text(), "0.8.16")

    assert "0.8.16 changed no schema epoch; 0.8.15 session and Landscape stores open unchanged" in text
    assert "Web runs admitted under 0.8.15 do not resume, because the plugin policy hash changes" in text
    assert "finish interrupted web runs before upgrading, or run them again afterwards" in text
    assert "A run approval recorded under 0.8.15 must be given again" in text
    assert "Long inline prompts belong in files" in text
    assert "inline LLM prompt over 100 words or 1,000 characters is refused" in text
    assert "now makes the pipeline invalid; it used to be valid with a warning" in text
    assert "The advisor reply cap now defaults to 16384 tokens (was 8192)" in text
    assert "Package metadata is" not in text


def test_readme_keeps_the_0_8_15_cutover_as_a_historical_pin() -> None:
    """The 0.8.15 entry keeps its cutover and upgrade actions and no current-release claim."""
    text = _upgrade_notes(_readme_text(), "0.8.15")

    assert "It superseded 0.8.14, which was tagged but never published" in text
    assert "the 0.8.14 upgrade actions apply" in text
    assert "A run interrupted before upgrading to 0.8.15 cannot be resumed after it" in text
    assert "Session epoch 79 replaced session epoch 78" in text
    assert "Landscape epoch 51 replaced epoch 50" in text
    assert "`prompt_review_snapshots` and `prompt_file_approvals`" in text
    assert "the Landscape store added `run_prompt_inputs`" in text
    assert "Startup refuses the older stores; preserve them and use separately provisioned new ones" in text
    assert "API clients that approve prompt review cards must send `reviewed_anchor`" in text
    assert "Package metadata is" not in text


def test_readme_keeps_the_0_8_14_python_move_as_a_historical_pin() -> None:
    """The 0.8.14 entry keeps its upgrade actions and no current-release claim."""
    text = _upgrade_notes(_readme_text(), "0.8.14")

    assert "0.8.14 moves ELSPETH to Python 3.14" in text
    assert "It changed no schema epoch" in text
    assert "Python 3.14 is now required for `elspeth`, `elspeth-lints` and the LLM gateway" in text
    assert "3.12 and 3.13 are no longer supported or tested" in text
    assert "Startup refuses `JSON_LOGS=true`" in text
    assert "a run checkpointed under 3.13 refuses to resume under 3.14" in text
    assert "drain in-flight runs and pending approvals before upgrading" in text
    assert "Web runs admitted before the upgrade also refuse to resume" in text
    assert "it has no `/usr/bin/python3`" in text
    assert "insert `line_explode`" in text
    assert "`purge --payload-dir` must never point at `shares/`" in text
    assert "Library entries published earlier must be republished before they can be forked" in text
    assert "the repository ruleset must be updated to match" in text
    assert "Package metadata is" not in text


def test_readme_keeps_the_0_8_13_context_budget_as_a_historical_pin() -> None:
    """The 0.8.13 entry keeps its two breaking settings changes and no current-release claim.

    The entry no longer repeats 0.8.13's known limits (advisor and review calls
    outside the budget): they were a statement about that release, not an
    upgrade action, and the current release bounds advisor requests. The
    changelog's 0.8.13 section still records them.
    """
    text = _upgrade_notes(_readme_text(), "0.8.13")

    assert "0.8.13 adds the Composer context budget" in text
    assert "`ELSPETH_WEB__COMPOSER_CONTEXT_MAX_PROMPT_TOKENS` is required and has no default" in text
    assert "the web server does not start without it" in text
    assert "The default of `ELSPETH_WEB__COMPOSER_PLANNER_MAX_REQUEST_BYTES` drops from 2 MiB to 960 KiB" in text
    assert "Advisor and review calls are not yet covered" not in text
    assert "Package metadata is" not in text


def test_readme_keeps_the_0_8_12_remediation_as_a_historical_pin() -> None:
    """The 0.8.12 entry keeps what an operator or client must act on and no current-release claim."""
    text = _upgrade_notes(_readme_text(), "0.8.12")

    assert "0.8.12 is the Composer tool remediation" in text
    assert "MCP file paths are confined to the configured data directory" in text
    assert "Langfuse tracing now needs Langfuse Cloud or a self-hosted server at 4.59.0 or later" in text
    assert "a response carrying more than one mutation is refused whole" in text
    assert "Package metadata is" not in text


def test_readme_keeps_the_0_8_10_cutover_as_a_historical_pin() -> None:
    """The 0.8.10 entry keeps its own epoch cutover statements."""
    text = _upgrade_notes(_readme_text(), "0.8.10")

    assert "Session epoch 78 replaces session epoch 77" in text
    assert "retains its committed graph-review authority" in text
    assert "Landscape epoch 50 replaces epoch 49" in text
    assert "Preserve epoch 75, both epoch 76 session-store contracts, epoch 77 session stores and epoch 49 Landscape stores" in text
    assert "use a separately provisioned epoch 78 session store" in text
    assert "epoch 50 Landscape store" in text
    assert "Keep old workers isolated from the new stores" in text
    assert "Compatible stores still require exact schema admission" in text
    assert "unsupported stores are preserved and refused before incompatible writes" in text
    assert "Quota API quantities now use decimal strings for exact signed-64-bit values" in text
    assert "ELSPETH_WEB__TRUSTED_PROXY_HOSTS" in text
    assert "`ELSPETH_WEB__COMPOSER_*_REASONING_EFFORT=none` is refused at startup" in text


def test_readme_has_no_per_release_history_sections() -> None:
    """Earlier releases live in the upgrading section and the changelog, not in a section each."""
    assert "\n## Previously in " not in _readme_text()


def test_readme_release_links_resolve() -> None:
    text = _readme_text()
    linked_paths = set(re.findall(r"\]\((docs/release/[^)#]+)", text))

    assert linked_paths, "README should reference at least one docs/release/ document"
    for relative_path in linked_paths:
        assert (REPO_ROOT / relative_path).exists(), relative_path
