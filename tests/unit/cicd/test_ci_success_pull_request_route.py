"""A pull request's CI Success may accept a skipped Integration Tests only for a push-pattern head.

Integration Tests runs on push events only. CI Success needs it, so on a pull
request run it is skipped. These tests pin that the aggregate accepts that skip
only after the head branch was shown to be one whose own push run carries
Integration Tests, and refuses every other pull request head.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any

import pytest
import yaml
from tests.unit.cicd.workflow_sources import EXECUTABLE_WORKFLOWS

ROUTE_STEP = "Require a push-pattern head for pull requests"
CHECK_STEP = "Check all jobs passed"


def _aggregate() -> dict[str, Any]:
    workflow = yaml.safe_load((EXECUTABLE_WORKFLOWS / "ci.yaml").read_text())
    return workflow["jobs"]["ci-success"]


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    return next(step for step in job["steps"] if step["name"] == name)


def _bash(script: str, env: dict[str, str]) -> int:
    process = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        env={"PATH": os.environ["PATH"], **env},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return process.returncode


def _route(job: dict[str, Any], event: str, head: str) -> int:
    return _bash(_step(job, ROUTE_STEP)["run"], {"EVENT_NAME": event, "HEAD_REF": head})


def _check(job: dict[str, Any], event: str, integration: str) -> int:
    script = _step(job, CHECK_STEP)["run"]
    results = dict.fromkeys(job["needs"], "success")
    results["integration"] = integration
    for name, status in results.items():
        script = script.replace("${{ needs." + name + ".result }}", status)
    assert "${{" not in script
    return _bash(script, {"EVENT_NAME": event})


def test_route_step_precedes_the_check_and_reads_the_event_and_head_from_env() -> None:
    job = _aggregate()
    names = [step["name"] for step in job["steps"]]
    assert "integration" in job["needs"]
    assert names.index(ROUTE_STEP) < names.index(CHECK_STEP)
    route = _step(job, ROUTE_STEP)
    assert route["env"] == {"EVENT_NAME": "${{ github.event_name }}", "HEAD_REF": "${{ github.head_ref }}"}
    assert "${{" not in route["run"], "head names reach the script only through env"
    assert "if" not in route, "the route step must run whenever the fork check passed"
    assert _step(job, CHECK_STEP)["env"] == {"EVENT_NAME": "${{ github.event_name }}"}


@pytest.mark.parametrize("head", ["release/0.8.18", "release/x/y", "main", "master", "RC3"])
def test_route_accepts_push_pattern_heads(head: str) -> None:
    assert _route(_aggregate(), "pull_request", head) == 0


@pytest.mark.parametrize(
    "head",
    ["claude/some-branch", "fix/something", "dependabot/uv/x", "release/", "releases/x", "RC/x", "mainline", "xrelease/1", ""],
)
def test_route_refuses_every_other_pull_request_head(head: str) -> None:
    assert _route(_aggregate(), "pull_request", head) == 1


def test_route_does_not_apply_to_push_events() -> None:
    assert _route(_aggregate(), "push", "") == 0


@pytest.mark.parametrize(
    ("event", "integration", "expected"),
    [
        ("push", "success", 0),
        ("push", "skipped", 1),
        ("push", "cancelled", 1),
        ("pull_request", "skipped", 0),
        ("pull_request", "success", 0),
        ("pull_request", "failure", 1),
        ("pull_request", "cancelled", 1),
        ("pull_request", "", 1),
        ("", "skipped", 1),
    ],
)
def test_integration_skip_is_accepted_only_on_pull_request_runs(event: str, integration: str, expected: int) -> None:
    assert _check(_aggregate(), event, integration) == expected


def test_route_instrument_detects_a_removed_catch_all_refusal() -> None:
    job = _aggregate()
    assert _route(job, "pull_request", "claude/some-branch") == 1
    mutated, count = re.subn(r"\n\s*\*\)\n\s*echo [^\n]*\n\s*exit 1\n\s*;;", "", _step(job, ROUTE_STEP)["run"])
    assert count == 1
    assert _bash(mutated, {"EVENT_NAME": "pull_request", "HEAD_REF": "claude/some-branch"}) == 0
