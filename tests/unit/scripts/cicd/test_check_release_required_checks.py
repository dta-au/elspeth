"""Release image publication required-check verifier tests."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from scripts.cicd import check_release_required_checks as verifier
from scripts.cicd.check_release_required_checks import (
    CheckRun,
    CommitStatus,
    RequiredCheckResult,
    RequiredCheckSpec,
    evaluate_required_checks,
    extract_required_checks,
    extract_required_contexts,
    resolve_pr_only_checks,
)


def test_extract_required_contexts_from_ruleset() -> None:
    """The verifier must use the live ruleset's required-status context list."""
    ruleset = {
        "rules": [
            {"type": "deletion", "parameters": None},
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [
                        {"context": "CI Success"},
                        {"context": "CodeQL"},
                    ],
                },
            },
        ],
    }

    assert extract_required_contexts(ruleset) == ("CI Success", "CodeQL")


def test_extract_required_checks_preserves_ruleset_integration_ids() -> None:
    """Ruleset app bindings are part of the release-gate security invariant."""
    ruleset = {
        "rules": [
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [
                        {"context": "CI Success", "integration_id": 15368},
                        {"context": "redaction-gate"},
                    ],
                },
            },
        ],
    }

    assert extract_required_checks(ruleset) == (
        RequiredCheckSpec(context="CI Success", integration_id=15368),
        RequiredCheckSpec(context="redaction-gate", integration_id=None),
    )


def test_evaluate_required_checks_rejects_spoofed_evidence_for_app_bound_context() -> None:
    """A same-name status or check from another app must not satisfy an app-bound rule."""
    results = evaluate_required_checks(
        required_contexts=(RequiredCheckSpec(context="CI Success", integration_id=15368),),
        check_runs=[
            CheckRun(
                name="CI Success",
                status="completed",
                conclusion="success",
                html_url="https://example.invalid/spoofed-check",
                app_id=99999,
            ),
        ],
        statuses=[
            CommitStatus(
                context="CI Success",
                state="success",
                target_url="https://example.invalid/spoofed-status",
            )
        ],
    )

    assert results == (
        RequiredCheckResult(
            context="CI Success",
            matched_name=None,
            state="missing",
            url=None,
        ),
    )


def test_evaluate_required_checks_accepts_check_run_from_ruleset_integration() -> None:
    """An app-bound rule is satisfied by a successful check run from the required app."""
    results = evaluate_required_checks(
        required_contexts=(RequiredCheckSpec(context="CI Success", integration_id=15368),),
        check_runs=[
            CheckRun(
                name="CI Success",
                status="completed",
                conclusion="success",
                html_url="https://example.invalid/trusted-check",
                app_id=15368,
            ),
        ],
        statuses=[],
    )

    assert results == (
        RequiredCheckResult(
            context="CI Success",
            matched_name="CI Success",
            state="success",
            url="https://example.invalid/trusted-check",
        ),
    )


def test_evaluate_required_checks_fails_when_required_context_is_missing() -> None:
    """CI Success alone must not authorize image publication."""
    results = evaluate_required_checks(
        required_contexts=(
            "CI Success",
            "CodeQL",
            "Check cohort-attribution trailers on PR commits",
            "redaction-gate",
        ),
        check_runs=[
            CheckRun(name="CI Success", status="completed", conclusion="success", html_url="https://example.invalid/ci"),
            CheckRun(name="Analyze Python", status="completed", conclusion="success", html_url="https://example.invalid/codeql"),
        ],
        statuses=[],
    )

    assert results == (
        RequiredCheckResult(
            context="CI Success",
            matched_name="CI Success",
            state="success",
            url="https://example.invalid/ci",
        ),
        RequiredCheckResult(
            context="CodeQL",
            matched_name="Analyze Python",
            state="success",
            url="https://example.invalid/codeql",
        ),
        RequiredCheckResult(
            context="Check cohort-attribution trailers on PR commits",
            matched_name=None,
            state="missing",
            url=None,
        ),
        RequiredCheckResult(
            context="redaction-gate",
            matched_name=None,
            state="missing",
            url=None,
        ),
    )


def test_evaluate_required_checks_accepts_all_successful_required_contexts() -> None:
    """Publication can proceed only when every required context has succeeded."""
    results = evaluate_required_checks(
        required_contexts=(
            "CI Success",
            "CodeQL",
            "Check cohort-attribution trailers on PR commits",
            "redaction-gate",
        ),
        check_runs=[
            CheckRun(name="CI Success", status="completed", conclusion="success", html_url=None),
            CheckRun(name="Analyze Python", status="completed", conclusion="success", html_url=None),
            CheckRun(name="Check cohort-attribution trailers on PR commits", status="completed", conclusion="success", html_url=None),
            CheckRun(name="redaction-gate", status="completed", conclusion="success", html_url=None),
        ],
        statuses=[],
    )

    assert all(result.state == "success" for result in results)


def test_evaluate_required_checks_uses_latest_matching_check_run() -> None:
    """An older success must not mask a newer failed run for the same context."""
    results = evaluate_required_checks(
        required_contexts=("CI Success",),
        check_runs=[
            CheckRun(
                name="CI Success",
                status="completed",
                conclusion="success",
                html_url="https://example.invalid/old",
                completed_at="2026-06-01T00:00:00Z",
            ),
            CheckRun(
                name="CI Success",
                status="completed",
                conclusion="failure",
                html_url="https://example.invalid/new",
                completed_at="2026-06-02T00:00:00Z",
            ),
        ],
        statuses=[],
    )

    assert results == (
        RequiredCheckResult(
            context="CI Success",
            matched_name="CI Success",
            state="failure",
            url="https://example.invalid/new",
        ),
    )


REPO = "dta-au/elspeth"
IMAGE_SHA = "a" * 40
HEAD_SHA = "b" * 40
BASE_SHA = "c" * 40
TREE_SHA = "d" * 40
COHORT = "Check cohort-attribution trailers on PR commits"
REQUIRED = tuple(RequiredCheckSpec(context=context) for context in ("CI Success", "CodeQL", COHORT, "redaction-gate"))


class FakeGitHub:
    """API-shaped data at the network seam; all production proof code runs."""

    def __init__(self) -> None:
        self.resources: dict[str, Any] = {
            f"/repos/{REPO}/git/commits/{IMAGE_SHA}": {
                "sha": IMAGE_SHA,
                "tree": {"sha": TREE_SHA},
                "parents": [{"sha": BASE_SHA}, {"sha": HEAD_SHA}],
            },
            f"/repos/{REPO}/git/commits/{HEAD_SHA}": {"sha": HEAD_SHA, "tree": {"sha": TREE_SHA}, "parents": [{"sha": BASE_SHA}]},
            f"/repos/{REPO}/pulls/269": {
                "number": 269,
                "state": "closed",
                "merged": True,
                "merged_at": "2026-10-04T19:26:00Z",
                "merge_commit_sha": IMAGE_SHA,
                "base": {"ref": "main", "repo": {"full_name": REPO}},
                "head": {"sha": HEAD_SHA, "ref": "release/0.8.2", "repo": {"full_name": REPO}},
            },
            f"/repos/{REPO}/rulesets": [{"id": 7, "name": "main", "target": "branch", "enforcement": "active"}],
            f"/repos/{REPO}/rulesets/7": {
                "rules": [
                    {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": s.context} for s in REQUIRED]}}
                ]
            },
            f"/repos/{REPO}/commits/{IMAGE_SHA}/status": {"statuses": []},
        }
        self.checks = [self.check(COHORT, 11), self.check("redaction-gate", 12)]
        self.workflows = [self.workflow(COHORT, 11), self.workflow("redaction-gate", 12)]
        self.associated: list[dict[str, Any]] = [{"number": 269}]
        self.pages: dict[str, Any] = {
            f"/repos/{REPO}/commits/{IMAGE_SHA}/pulls": [self.associated],
            f"/repos/{REPO}/commits/{HEAD_SHA}/check-runs": [{"check_runs": self.checks}],
            f"/repos/{REPO}/actions/runs": [{"workflow_runs": self.workflows}],
            f"/repos/{REPO}/commits/{IMAGE_SHA}/check-runs": [
                {"check_runs": [self.check("CI Success", 1, IMAGE_SHA), self.check("Analyze Python", 2, IMAGE_SHA)]}
            ],
        }
        self.calls: list[str] = []

    @staticmethod
    def check(name: str, suite: int, sha: str = HEAD_SHA) -> dict[str, Any]:
        return {
            "name": name,
            "status": "completed",
            "conclusion": "success",
            "head_sha": sha,
            "app": {"id": 15368},
            "check_suite": {"id": suite},
            "html_url": "https://example.invalid/check",
            "started_at": "2026-10-04T17:37:00Z",
            "completed_at": "2026-10-04T17:38:00Z",
        }

    @staticmethod
    def workflow(context: str, suite: int) -> dict[str, Any]:
        return {
            "workflow_id": suite,
            "run_number": 50,
            "run_attempt": 1,
            "check_suite_id": suite,
            "head_sha": HEAD_SHA,
            "head_branch": "release/0.8.2",
            "path": verifier.PR_ONLY_WORKFLOWS[context],
            "event": "pull_request",
            "repository": {"full_name": REPO},
            "head_repository": {"full_name": REPO},
            "status": "completed",
            "conclusion": "success",
            # Actual GitHub merged-run payloads have empty associations.
            "pull_requests": [],
        }

    def json(self, path: str, *, token: str) -> Any:
        self.calls.append(path)
        return deepcopy(self.resources[path])

    def json_pages(self, path: str, *, token: str, params: Any) -> tuple[Any, ...]:
        self.calls.append(path)
        assert params["per_page"] == "100"
        if path.endswith("/actions/runs"):
            assert params["head_sha"] == HEAD_SHA
            assert params["event"] == "pull_request"
        pages = deepcopy(self.pages[path])
        if path.endswith("/check-runs") or path.endswith("/actions/runs"):
            collection = "check_runs" if path.endswith("/check-runs") else "workflow_runs"
            total = sum(len(page[collection]) for page in pages)
            for page in pages:
                page.setdefault("total_count", total)
        return tuple(pages)


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    api = FakeGitHub()
    monkeypatch.setattr(verifier, "_github_api_json", api.json)
    monkeypatch.setattr(verifier, "_github_api_json_pages", api.json_pages)
    return api


def _image_results() -> tuple[RequiredCheckResult, ...]:
    return evaluate_required_checks(
        required_contexts=REQUIRED,
        check_runs=[CheckRun("CI Success", "completed", "success", None), CheckRun("Analyze Python", "completed", "success", None)],
        statuses=(),
    )


def _resolve(results: tuple[RequiredCheckResult, ...] | None = None) -> tuple[RequiredCheckResult, ...]:
    return resolve_pr_only_checks(
        repo=REPO,
        image_sha=IMAGE_SHA,
        target_branch="main",
        token="fixture",
        required_checks=REQUIRED,
        results=_image_results() if results is None else results,
    )


@pytest.mark.parametrize("parents", [[BASE_SHA, HEAD_SHA], [BASE_SHA]], ids=["merge", "squash-or-rebase-result"])
def test_publication_proof_accepts_verified_source_identical_result(github: FakeGitHub, parents: list[str]) -> None:
    github.resources[f"/repos/{REPO}/git/commits/{IMAGE_SHA}"]["parents"] = [{"sha": sha} for sha in parents]
    results = _resolve()
    assert verifier.all_required_checks_succeeded(results)
    assert results[:2] == _image_results()[:2]
    assert {result.evidence_sha for result in results[2:]} == {HEAD_SHA}
    assert {result.pull_request_number for result in results[2:]} == {269}


@pytest.mark.parametrize("state", ["failure", "queued", "in_progress", "skipped", "cancelled"])
def test_existing_image_refusal_is_never_replaced(github: FakeGitHub, state: str) -> None:
    original = tuple(RequiredCheckResult(s.context, s.context, state, None) for s in REQUIRED)
    assert _resolve(original) == original
    assert github.calls == []


def test_all_direct_evidence_keeps_tag_and_manual_route_without_pr_lookup(github: FakeGitHub) -> None:
    original = tuple(RequiredCheckResult(s.context, s.context, "success", None) for s in REQUIRED)
    assert _resolve(original) == original
    assert github.calls == []


def test_missing_ci_codeql_or_unknown_context_is_not_inherited(github: FakeGitHub) -> None:
    required = (*REQUIRED, RequiredCheckSpec("Future required gate"))
    results = tuple(RequiredCheckResult(s.context, None, "missing", None) for s in required)
    resolved = resolve_pr_only_checks(
        repo=REPO, image_sha=IMAGE_SHA, target_branch="main", token="fixture", required_checks=required, results=results
    )
    assert [(r.context, r.state) for r in resolved] == [
        ("CI Success", "missing"),
        ("CodeQL", "missing"),
        (COHORT, "success"),
        ("redaction-gate", "success"),
        ("Future required gate", "missing"),
    ]
    assert not verifier.all_required_checks_succeeded(resolved)


@pytest.mark.parametrize(
    ("field", "value"),
    [("merged", False), ("merged", "true"), ("state", "open"), ("merge_commit_sha", "e" * 40)],
)
def test_association_is_not_merged_result_proof(github: FakeGitHub, field: str, value: Any) -> None:
    github.resources[f"/repos/{REPO}/pulls/269"][field] = value
    with pytest.raises(ValueError, match="expected one trusted merged PR"):
        _resolve()


@pytest.mark.parametrize("boundary", ["target", "base-repo", "head-repo"])
def test_pr_repository_and_target_boundaries_are_required(github: FakeGitHub, boundary: str) -> None:
    pr = github.resources[f"/repos/{REPO}/pulls/269"]
    if boundary == "target":
        pr["base"]["ref"] = "release/0.8.2"
    elif boundary == "base-repo":
        pr["base"]["repo"]["full_name"] = "elsewhere/elspeth"
    else:
        pr["head"]["repo"]["full_name"] = "fork/elspeth"
    with pytest.raises(ValueError, match="expected one trusted merged PR"):
        _resolve()


@pytest.mark.parametrize("field", ["merged_at", "head", "base"])
def test_malformed_pr_evidence_fails_closed(github: FakeGitHub, field: str) -> None:
    github.resources[f"/repos/{REPO}/pulls/269"].pop(field)
    with pytest.raises(ValueError):
        _resolve()


def test_ambiguous_merged_prs_fail_closed(github: FakeGitHub) -> None:
    other = deepcopy(github.resources[f"/repos/{REPO}/pulls/269"])
    other["number"] = 270
    github.resources[f"/repos/{REPO}/pulls/270"] = other
    github.associated.append({"number": 270})
    with pytest.raises(ValueError, match="found 2"):
        _resolve()


def test_no_merged_pr_does_not_use_ancestor_success(github: FakeGitHub) -> None:
    github.associated.clear()
    with pytest.raises(ValueError, match="found 0"):
        _resolve()


@pytest.mark.parametrize("parents", [[], [BASE_SHA, "e" * 40], [BASE_SHA, HEAD_SHA, "e" * 40]])
def test_unrelated_or_unexpected_parent_topology_fails_closed(github: FakeGitHub, parents: list[str]) -> None:
    github.resources[f"/repos/{REPO}/git/commits/{IMAGE_SHA}"]["parents"] = [{"sha": sha} for sha in parents]
    with pytest.raises(ValueError, match="parent"):
        _resolve()


def test_different_integrated_tree_requires_fresh_proof(github: FakeGitHub) -> None:
    github.resources[f"/repos/{REPO}/git/commits/{IMAGE_SHA}"]["tree"]["sha"] = "e" * 40
    with pytest.raises(ValueError, match="different trees"):
        _resolve()


def test_returned_commit_identity_must_equal_requested_sha(github: FakeGitHub) -> None:
    github.resources[f"/repos/{REPO}/git/commits/{IMAGE_SHA}"]["sha"] = "e" * 40
    with pytest.raises(ValueError, match="different commit identity"):
        _resolve()


@pytest.mark.parametrize("timestamp", ["not-a-date", "2026-10-04T19:26:00"])
def test_merged_timestamp_must_be_real_and_timezone_aware(github: FakeGitHub, timestamp: str) -> None:
    github.resources[f"/repos/{REPO}/pulls/269"]["merged_at"] = timestamp
    with pytest.raises(ValueError):
        _resolve()


def test_matching_malformed_tree_identifiers_are_not_proof(github: FakeGitHub) -> None:
    for sha in (IMAGE_SHA, HEAD_SHA):
        github.resources[f"/repos/{REPO}/git/commits/{sha}"]["tree"]["sha"] = "invalid-tree"
    with pytest.raises(ValueError, match="full immutable SHA"):
        _resolve()


@pytest.mark.parametrize(
    ("field", "value"),
    [("path", ".github/workflows/other.yml"), ("event", "push"), ("head_sha", "e" * 40), ("head_branch", "other-branch")],
)
def test_wrong_workflow_provenance_cannot_supply_context(github: FakeGitHub, field: str, value: str) -> None:
    github.workflows[1][field] = value
    assert _resolve()[-1].state == "missing"


@pytest.mark.parametrize("field", ["repository", "head_repository"])
def test_wrong_workflow_repository_cannot_supply_context(github: FakeGitHub, field: str) -> None:
    github.workflows[1][field]["full_name"] = "fork/elspeth"
    assert _resolve()[-1].state == "missing"


def test_nonempty_pr_association_must_include_selected_pr(github: FakeGitHub) -> None:
    github.workflows[1]["pull_requests"] = [{"number": 999}]
    assert _resolve()[-1].state == "missing"
    github.workflows[1]["pull_requests"] = [{"number": 269}]
    assert _resolve()[-1].state == "success"


@pytest.mark.parametrize("boundary", ["app", "suite", "head"])
def test_same_named_check_requires_trusted_workflow_suite(github: FakeGitHub, boundary: str) -> None:
    check = github.checks[1]
    if boundary == "app":
        check["app"]["id"] = 999
    elif boundary == "suite":
        check["check_suite"]["id"] = 999
    else:
        check["head_sha"] = "e" * 40
    if boundary == "head":
        with pytest.raises(ValueError, match="check run head"):
            _resolve()
    else:
        assert _resolve()[-1].state == "missing"


@pytest.mark.parametrize("state", ["failure", "queued", "in_progress", "skipped", "cancelled"])
def test_newer_workflow_refusal_beats_older_success(github: FakeGitHub, state: str) -> None:
    newer = deepcopy(github.workflows[1])
    newer["run_number"] += 1
    newer["check_suite_id"] = 22
    newer["status"] = state if state in {"queued", "in_progress"} else "completed"
    newer["conclusion"] = None if state in {"queued", "in_progress"} else state
    github.workflows.append(newer)
    assert _resolve()[-1].state == state


def test_latest_rerun_attempt_must_succeed(github: FakeGitHub) -> None:
    github.workflows[1]["run_attempt"] = 2
    github.workflows[1]["status"] = "in_progress"
    github.workflows[1]["conclusion"] = None
    assert _resolve()[-1].state == "in_progress"


@pytest.mark.parametrize("status", ["queued", "in_progress", "success", "invalid"])
def test_contradictory_workflow_success_cannot_pass(github: FakeGitHub, status: str) -> None:
    github.workflows[1]["status"] = status
    github.workflows[1]["conclusion"] = "success"
    assert not verifier.all_required_checks_succeeded(_resolve())


@pytest.mark.parametrize("status", ["queued", "in_progress", "success", "invalid"])
def test_contradictory_check_success_cannot_pass(github: FakeGitHub, status: str) -> None:
    github.checks[1]["status"] = status
    github.checks[1]["conclusion"] = "success"
    assert not verifier.all_required_checks_succeeded(_resolve())


def test_timestamp_free_queued_source_check_cannot_hide_behind_success(github: FakeGitHub) -> None:
    newer = deepcopy(github.checks[1])
    newer["status"] = "queued"
    newer["conclusion"] = None
    newer["started_at"] = None
    newer["completed_at"] = None
    github.checks.append(newer)
    assert _resolve()[-1].state == "queued"


@pytest.mark.parametrize("state", ["failure", "in_progress", "skipped", "cancelled"])
def test_successful_workflow_does_not_hide_non_successful_check(github: FakeGitHub, state: str) -> None:
    github.checks[1]["status"] = "in_progress" if state == "in_progress" else "completed"
    github.checks[1]["conclusion"] = None if state == "in_progress" else state
    assert _resolve()[-1].state == state


def test_ambiguous_workflow_identity_refuses_source_recovery(github: FakeGitHub) -> None:
    duplicate = deepcopy(github.workflows[1])
    duplicate["workflow_id"] = 999
    github.workflows.append(duplicate)
    with pytest.raises(ValueError, match="ambiguous workflow identity"):
        _resolve()


def test_conflicting_same_latest_attempt_is_not_arbitrarily_selected(github: FakeGitHub) -> None:
    duplicate = deepcopy(github.workflows[1])
    duplicate["conclusion"] = "failure"
    github.workflows.append(duplicate)
    with pytest.raises(ValueError, match="ambiguous latest workflow attempt"):
        _resolve()


def test_ruleset_app_binding_is_preserved_in_inherited_evidence(github: FakeGitHub) -> None:
    required = (*REQUIRED[:-1], RequiredCheckSpec("redaction-gate", integration_id=999))
    resolved = resolve_pr_only_checks(
        repo=REPO, image_sha=IMAGE_SHA, target_branch="main", token="fixture", required_checks=required, results=_image_results()
    )
    assert resolved[-1].state == "missing"


@pytest.mark.parametrize("field", ["workflow_id", "run_number", "run_attempt", "check_suite_id", "head_repository", "pull_requests"])
def test_malformed_workflow_evidence_fails_closed(github: FakeGitHub, field: str) -> None:
    github.workflows[1].pop(field)
    with pytest.raises(ValueError):
        _resolve()


def test_proof_uses_all_association_and_workflow_pages(github: FakeGitHub) -> None:
    github.pages[f"/repos/{REPO}/commits/{IMAGE_SHA}/pulls"] = [[], github.associated]
    github.pages[f"/repos/{REPO}/actions/runs"] = [{"workflow_runs": []}, {"workflow_runs": github.workflows}]
    github.pages[f"/repos/{REPO}/commits/{HEAD_SHA}/check-runs"] = [{"check_runs": []}, {"check_runs": github.checks}]
    assert verifier.all_required_checks_succeeded(_resolve())


@pytest.mark.parametrize("collection", ["check-runs", "actions/runs"])
def test_missing_pages_cannot_hide_newer_refusal(github: FakeGitHub, collection: str) -> None:
    path = f"/repos/{REPO}/commits/{HEAD_SHA}/check-runs" if collection == "check-runs" else f"/repos/{REPO}/actions/runs"
    github.pages[path][0]["total_count"] = 3
    with pytest.raises(ValueError, match="pagination is incomplete"):
        _resolve()


def test_changed_page_count_invalidates_proof(github: FakeGitHub) -> None:
    github.pages[f"/repos/{REPO}/actions/runs"] = [
        {"total_count": 2, "workflow_runs": [github.workflows[0]]},
        {"total_count": 3, "workflow_runs": [github.workflows[1]]},
    ]
    with pytest.raises(ValueError, match="count changed during pagination"):
        _resolve()


def test_old_late_completion_cannot_mask_a_newer_pending_check() -> None:
    results = evaluate_required_checks(
        required_contexts=("CI Success",),
        check_runs=[
            CheckRun("CI Success", "completed", "success", None, started_at="2026-10-04T17:00:00Z", completed_at="2026-10-04T17:20:00Z"),
            CheckRun("CI Success", "in_progress", None, None, started_at="2026-10-04T17:10:00Z"),
        ],
        statuses=(),
    )
    assert results[0].state == "in_progress"


def test_timestamp_free_queued_direct_check_cannot_hide_behind_success() -> None:
    results = evaluate_required_checks(
        required_contexts=("CI Success",),
        check_runs=[
            CheckRun("CI Success", "completed", "success", None, started_at="2026-10-04T17:00:00Z", completed_at="2026-10-04T17:20:00Z"),
            CheckRun("CI Success", "queued", None, None),
        ],
        statuses=(),
    )
    assert results[0].state == "queued"


def test_cli_accepts_incident_shape_and_prints_inherited_sha(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GH_TOKEN", "fixture")
    assert verifier.main(["--repo", REPO, "--sha", IMAGE_SHA]) == 0
    output = capsys.readouterr().out
    assert f"on {HEAD_SHA} through merged PR #269" in output
    assert f"All ruleset-required checks succeeded for {IMAGE_SHA}" in output


def test_cli_refuses_unproven_source_without_publication(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GH_TOKEN", "fixture")
    github.associated.clear()
    assert verifier.main(["--repo", REPO, "--sha", IMAGE_SHA]) == 1
    assert "refusing to publish image" in capsys.readouterr().err


def test_cli_direct_proof_needs_no_merged_pr_even_for_release_or_manual_sha(github: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "fixture")
    github.associated.clear()
    github.pages[f"/repos/{REPO}/commits/{IMAGE_SHA}/check-runs"][0]["check_runs"].extend(
        [github.check(COHORT, 3, IMAGE_SHA), github.check("redaction-gate", 4, IMAGE_SHA)]
    )
    assert verifier.main(["--repo", REPO, "--sha", IMAGE_SHA]) == 0
    assert not any(path.endswith("/pulls") or "/pulls/" in path for path in github.calls)


@pytest.mark.parametrize("ref", ["main", "v0.8.2", IMAGE_SHA[:7]])
def test_cli_rejects_moving_or_abbreviated_commit_refs(github: FakeGitHub, ref: str) -> None:
    assert verifier.main(["--repo", REPO, "--sha", ref]) == 2
    assert github.calls == []
