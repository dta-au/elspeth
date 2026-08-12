"""Maintained CI selection contract for the state-engine proof gates.

Task 12 requires the maintained gates to fail closed: the always-on CI run
must validate both catalogs, the plugin inventory, the evidence selector
manifest, and the documentation links, and the aggregate CI Success job must
refuse to pass when that validation job fails. These tests pin the exact
selection so silent drift in the workflows is a test failure, not a quiet
green.
"""

from __future__ import annotations

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


def test_ci_success_requires_state_engine_validation() -> None:
    job = _job("ci-success")
    assert "state-engine-validation" in job["needs"]
    check = _run_lines(job)
    assert "needs.state-engine-validation.result" in check


def test_always_on_test_job_runs_the_ci_equivalent_selection() -> None:
    commands = _run_lines(_job("test"))
    assert "pytest tests/" in commands


def test_testcontainer_job_selects_the_postgresql_matrix() -> None:
    commands = _run_lines(_job("testcontainer"))
    assert "tests/testcontainer/" in commands
    assert "-m testcontainer" in commands


def test_default_selection_excludes_protected_live_lanes() -> None:
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    addopts = pyproject["tool"]["pytest"]["ini_options"]["addopts"]
    marker_expressions = [addopts[index + 1] for index, token in enumerate(addopts) if token == "-m"]
    assert any("not live_provider" in expression for expression in marker_expressions)
