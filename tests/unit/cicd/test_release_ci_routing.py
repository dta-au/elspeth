"""Release CI runs once through the PR merge tree, with no synthetic success."""

from __future__ import annotations

import copy
from fnmatch import fnmatchcase
from typing import Any

import pytest
import yaml
from tests.unit.cicd.test_workflow_runner_isolation import _admitted, _context, _events, _expression
from tests.unit.cicd.workflow_sources import EXECUTABLE_WORKFLOWS


def _workflow() -> dict[str, Any]:
    return yaml.safe_load((EXECUTABLE_WORKFLOWS / "ci.yaml").read_text())


def _triggers(workflow: dict[str, Any], event: str, branch: str) -> bool:
    """Evaluate the branch-only trigger subset used by CI; reject other filters."""
    events = _events(workflow)
    if event not in events:
        return False
    if event == "workflow_dispatch":
        assert events[event] is None
        return True
    config = events[event]
    assert set(config) == {"branches"}, "CI must not silently skip paths or event activity types"
    assert set(config["branches"]) <= {"main", "master", "RC*", "release/**"}
    return any(fnmatchcase(branch, pattern) for pattern in config["branches"])


def _assert_release_event_routing(workflow: dict[str, Any]) -> None:
    for release in ("release/0.8.10-assembly", "release/next/candidate"):
        assert not _triggers(workflow, "push", release), "release push duplicates the full PR matrix"
        assert _triggers(workflow, "pull_request", "main"), "release PR must receive a real CI run"
        assert _triggers(workflow, "pull_request", release), "PRs into release branches remain checked"
        assert _triggers(workflow, "workflow_dispatch", release), "release without a PR needs explicit CI"
    for branch in ("main", "master", "RC1"):
        assert _triggers(workflow, "push", branch), "post-merge integration CI must remain"
    assert not _triggers(workflow, "push", "feature/example")


def test_release_has_one_automatic_matrix_and_an_explicit_manual_path() -> None:
    _assert_release_event_routing(_workflow())


@pytest.mark.parametrize("mutation", ["duplicate-push", "missing-pr", "missing-manual", "missing-main"])
def test_release_routing_instrument_rejects_duplicate_or_missing_paths(mutation: str) -> None:
    workflow = _workflow()
    _assert_release_event_routing(workflow)
    changed = copy.deepcopy(workflow)
    events = _events(changed)
    if mutation == "duplicate-push":
        events["push"]["branches"].append("release/**")
    elif mutation == "missing-pr":
        del events["pull_request"]
    elif mutation == "missing-manual":
        del events["workflow_dispatch"]
    else:
        events["push"]["branches"].remove("main")
    with pytest.raises(AssertionError):
        _assert_release_event_routing(changed)


@pytest.mark.parametrize(
    ("event", "ref", "head_ref", "cancels"),
    [
        ("pull_request", "refs/pull/338/merge", "release/0.8.10-assembly", False),
        ("pull_request", "refs/pull/339/merge", "feature/example", True),
        ("workflow_dispatch", "refs/heads/release/0.8.10-assembly", "", False),
        ("push", "refs/heads/main", "", True),
    ],
)
def test_release_pr_and_manual_runs_finish_before_pending_replacement(event: str, ref: str, head_ref: str, cancels: bool) -> None:
    context = _context(event, ref=ref)
    context["github"]["head_ref"] = head_ref
    assert _expression(_workflow()["concurrency"]["cancel-in-progress"], context) is cancels


def test_release_pr_keeps_merge_checkout_and_every_required_producer() -> None:
    jobs = _workflow()["jobs"]
    context = _context("pull_request", ref="refs/pull/338/merge")
    context["github"]["head_ref"] = "release/0.8.10-assembly"
    assert jobs["ci-success"]["if"].startswith("(always()) && ")
    for name in jobs["ci-success"]["needs"]:
        assert _admitted(jobs, name, context), name
    checkouts = [step for job in jobs.values() for step in job.get("steps", []) if step.get("uses", "").startswith("actions/checkout@")]
    assert checkouts
    for checkout in checkouts:
        assert checkout.get("with", {}).get("ref") in (None, "${{ github.sha }}"), "PR checks must use their merge tree"


@pytest.mark.parametrize("job_name", _workflow()["jobs"])
def test_every_ci_job_refuses_unprotected_manual_dispatch_before_runner_allocation(job_name: str) -> None:
    jobs = _workflow()["jobs"]
    for ref in ("refs/heads/main", "refs/heads/master", "refs/heads/RC1", "refs/heads/release/0.8.10-assembly"):
        context = _context("workflow_dispatch", ref=ref)
        assert _admitted(jobs, job_name, context), job_name
        context["github"]["ref_protected"] = False
        assert not _admitted(jobs, job_name, context), job_name
        changed = copy.deepcopy(jobs)
        changed[job_name]["if"] = "always()"
        assert _admitted(changed, job_name, context), "negative control must expose a removed manual admission guard"


@pytest.mark.parametrize(
    ("event", "ref", "head_ref", "fork", "expected"),
    [
        ("pull_request", "refs/pull/338/merge", "release/0.8.10-assembly", False, True),
        ("pull_request", "refs/pull/338/merge", "release/0.8.10-assembly", True, False),
        ("pull_request", "refs/pull/339/merge", "feature/example", False, False),
        ("workflow_dispatch", "refs/heads/release/0.8.10-assembly", "", False, True),
        ("workflow_dispatch", "refs/heads/feature/example", "", False, False),
        ("push", "refs/heads/main", "", False, True),
    ],
)
def test_optional_integration_follows_canonical_release_path_without_admitting_forks(
    event: str, ref: str, head_ref: str, fork: bool, expected: bool
) -> None:
    context = _context(event, ref=ref, fork=fork)
    context["github"]["head_ref"] = head_ref
    assert _admitted(_workflow()["jobs"], "integration", context) is expected
