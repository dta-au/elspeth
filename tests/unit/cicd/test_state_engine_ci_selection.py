"""Maintained CI selection contract for the state-engine proof gates.

Task 12 requires the maintained gates to fail closed: the always-on CI run
must validate both catalogs, the plugin inventory, the evidence selector
manifest, and the documentation links, and the aggregate CI Success job must
refuse to pass when that validation job fails. These tests pin the exact
selection so silent drift in the workflows is a test failure, not a quiet
green.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yaml"
PYPROJECT = REPO_ROOT / "pyproject.toml"
V2_CATALOG = "docs/architecture/state_engine/proof-catalog/v2/catalog.json"
V3_CATALOG = "docs/architecture/state_engine/proof-catalog/v3/catalog.json"
SELECTOR_MANIFEST = "docs/architecture/state_engine/proof-catalog/v3/evidence_selectors.json"
PLUGIN_MATRIX = "tests/golden/state_engine/plugin_lifecycle_matrix.json"
ACTION_PINS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "astral-sh/setup-uv": "c771a70e6277c0a99b617c7a806ffedaca235ff9",
}


def _workflow() -> dict[str, Any]:
    value = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _job(name: str) -> dict[str, Any]:
    job = _workflow()["jobs"][name]
    assert isinstance(job, dict)
    return job


def _run_lines(job: dict[str, Any]) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


PUSH_WORKFLOWS = (
    CI_WORKFLOW,
    REPO_ROOT / ".github" / "workflows" / "codeql.yaml",
    REPO_ROOT / ".github" / "workflows" / "enforce-allowlist-judge-gates.yaml",
)
RELEASE_KEEPS_IN_PROGRESS_RUNS = "${{ !startsWith(github.ref, 'refs/heads/release/') }}"
BASH_ONLY_TOKENS = ("pipefail", "[[")


def test_container_job_steps_with_bash_syntax_declare_bash() -> None:
    """A container job's default shell is dash, so bash-only steps must say so.

    GitHub's shell auto-detection inside ``container:`` jobs falls back to
    ``sh -e`` (dash), which rejects ``set -o pipefail`` and has no ``[[``.
    The "Reject touched or broadened permanent multi-rule per-file blankets"
    step ran that way on every push run and died at its first line, so the
    ratchet resolver it guards never executed (run 33944365102,
    elspeth-d8749aeaa3). Every run step in a container job that uses a
    bash-only token must declare ``shell: bash``; host-runner jobs get bash
    by default and are not constrained here.
    """
    workflow = _workflow()
    offenders: list[str] = []
    checked = 0
    for job_name, job in workflow["jobs"].items():
        if "container" not in job:
            continue
        for step in job.get("steps", []):
            run = step.get("run")
            if not isinstance(run, str) or not any(token in run for token in BASH_ONLY_TOKENS):
                continue
            checked += 1
            if step.get("shell") != "bash":
                offenders.append(f"{job_name}: {step.get('name')!r}")
    assert checked >= 2, "expected the actionlint and blanket-ratchet steps to be checked"
    assert offenders == []


def test_release_refs_never_cancel_an_in_progress_required_run() -> None:
    """A required gate must be allowed to finish on a release branch.

    With ``cancel-in-progress: true`` every push cancels the running
    workflow, and a release branch is landed in bursts: on 2026-09-04/05
    release/0.8.0 took ~12 pushes in 11 h and not one CI run completed, so
    the required ``CI Success`` check never rendered a verdict on any sha
    (elspeth-d8749aeaa3). The expression below keeps the cancel for PRs and
    feature refs and disables it for ``release/**``; the pending run is still
    replaced by GitHub, so the tip is always tested eventually. All three
    push-triggered workflows share one group shape and one expression so a
    policy change is one edit, not three drifting ones.
    """
    seen: list[str] = []
    for path in PUSH_WORKFLOWS:
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        concurrency = workflow["concurrency"]
        assert concurrency["group"] == "${{ github.workflow }}-${{ github.ref }}", path.name
        assert concurrency["cancel-in-progress"] == RELEASE_KEEPS_IN_PROGRESS_RUNS, path.name
        seen.append(path.name)
    assert seen == ["ci.yaml", "codeql.yaml", "enforce-allowlist-judge-gates.yaml"]


def test_state_engine_validation_job_runs_every_maintained_validator() -> None:
    job = _job("state-engine-validation")
    commands = _run_lines(job)
    assert f"state_engine_assessment.py validate-catalog {V2_CATALOG}" in commands
    assert f"state_engine_assessment.py validate-catalog {V3_CATALOG}" in commands
    assert f"state_engine_plugin_matrix.py check {PLUGIN_MATRIX}" in commands
    assert "state_engine_assessment.py validate-selectors" in commands
    assert SELECTOR_MANIFEST in commands
    assert f"--catalog {V3_CATALOG}" in commands
    assert "state_engine_assessment.py check-links" in commands


def test_state_engine_validation_job_pins_actions_and_frozen_install() -> None:
    job = _job("state-engine-validation")
    uses = [step["uses"] for step in job["steps"] if "uses" in step]
    for action, sha in ACTION_PINS.items():
        assert f"{action}@{sha}" in uses
    assert "uv sync --frozen --all-extras" in _run_lines(job)


def test_suites_run_while_static_analysis_is_red_and_ci_success_still_requires_it() -> None:
    """The test suites report on every run; the merge still waits on static analysis.

    Operator ruling 2026-09-05 (elspeth-d8749aeaa3): the trust-tier step keeps
    ``static-analysis`` red by design until Phase 5 signs the allowlists, and
    while ``test`` and ``testcontainer`` carried ``needs: [static-analysis]``
    every Python job was ``skipped`` on every push, so no run could score a
    merge. Those two jobs (and ``integration``, which follows ``test``) now run
    regardless; ``ci-success`` still lists ``static-analysis`` in its ``needs``
    and still demands its ``result == success``, so the red blocks the merge
    without hiding the test verdict. Exactly these two jobs were authorised;
    the other static-analysis dependants keep theirs.
    """
    assert "needs" not in _job("test")
    assert "needs" not in _job("testcontainer")
    assert _job("integration")["needs"] == ["test"]
    for dependant in ("state-engine-validation", "azure-container-apps-bicep", "supply-chain-audit"):
        assert _job(dependant)["needs"] == ["static-analysis"], dependant
    ci_success = _job("ci-success")
    assert "static-analysis" in ci_success["needs"]
    assert ci_success["if"] == "always()"
    assert 'if [[ "${{ needs.static-analysis.result }}" != "success" ]]' in _run_lines(ci_success)
    trust_tier = next(step for step in _job("static-analysis")["steps"] if step["name"] == "Run trust-tier elspeth-lints rule")
    assert "continue-on-error" not in trust_tier


def test_ci_success_requires_state_engine_validation() -> None:
    job = _job("ci-success")
    assert "state-engine-validation" in job["needs"]
    check = _run_lines(job)
    assert "needs.state-engine-validation.result" in check


def test_always_on_test_job_runs_the_ci_equivalent_selection() -> None:
    commands = _run_lines(_job("test"))
    assert "pytest tests/" in commands


def test_testcontainer_job_selects_the_postgresql_matrix() -> None:
    """The testcontainer job is the ONLY run of the ``testcontainer`` ids.

    pyproject addopts deselect the marker, so the default selection (the
    ``test`` job) never sees them (elspeth-d8749aeaa3: a PostgreSQL-only
    schema defect landed on a fully green default suite). Three properties
    keep the job honest, and each is pinned here because dropping any one of
    them is a silent green:

    * the selection is the whole tree — four ``tests/integration/web/`` files
      carry per-test marks and ran nowhere while the job scanned only
      ``tests/testcontainer/``;
    * the run is serial — ``tests/testcontainer/web/conftest.py`` shares one
      PostgreSQL container across the deployment-acceptance files and raises
      ``UsageError`` under any xdist worker, while addopts default to
      ``-n 12`` (``CI=1`` disables the auto-xdist plugin, not addopts);
    * the junit report is emitted and uploaded, so a run URL can carry the
      P0 pin test as evidence.
    """
    job = _job("testcontainer")
    commands = _run_lines(job)
    assert re.search(r"pytest tests/\s", commands), "selection must be the whole tree"
    assert "pytest tests/testcontainer/" not in commands
    assert "-m testcontainer" in commands
    assert "-n 0" in commands
    assert "--junitxml=testcontainer-junit.xml" in commands
    sequential_fixture = (REPO_ROOT / "tests" / "testcontainer" / "web" / "conftest.py").read_text(encoding="utf-8")
    assert "-n 0" in sequential_fixture, "the job's serial flag mirrors the shared-container fixture's own rule"
    uploads = [step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/upload-artifact@")]
    assert any(step.get("if") == "always()" and "testcontainer-junit.xml" in str(step.get("with", {}).get("path")) for step in uploads)
    assert "testcontainer" in _job("ci-success")["needs"]


def test_default_selection_excludes_protected_live_lanes() -> None:
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    addopts = pyproject["tool"]["pytest"]["ini_options"]["addopts"]
    marker_expressions = [addopts[index + 1] for index, token in enumerate(addopts) if token == "-m"]
    assert any("not live_provider" in expression for expression in marker_expressions)
