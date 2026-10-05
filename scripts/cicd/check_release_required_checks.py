"""Verify release image publication is backed by required GitHub checks.

The build-push workflow uses this gate before publishing images for a commit.
It reads the active repository ruleset, extracts the required status-check
contexts and optional GitHub App integration bindings, and then verifies that
the image SHA has successful trusted evidence for every required context.
Only missing known PR-only contexts may use the verified merged PR's exact
head, provided its tree equals the image tree. Direct failures stay failures;
CI/CodeQL and unknown future contexts still require exact-image evidence.

GitHub ruleset contexts are not always identical to the check-run names exposed
by the Checks API. In the current repository, the ruleset context ``CodeQL`` is
reported by the CodeQL workflow's ``Analyze Python`` job. Keep that mapping
explicit so a future ruleset or workflow rename fails closed instead of silently
weakening release proof.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any

GITHUB_API_ROOT = "https://api.github.com"
DEFAULT_CONTEXT_ALIASES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "CodeQL": ("Analyze Python",),
    }
)
PR_ONLY_WORKFLOWS: Mapping[str, str] = MappingProxyType(
    {
        "Check cohort-attribution trailers on PR commits": ".github/workflows/enforce-telemetry-backfill-trailer.yaml",
        "redaction-gate": ".github/workflows/composer-redaction-gate.yml",
    }
)
GITHUB_ACTIONS_APP_ID = 15368


@dataclass(frozen=True, slots=True)
class RequiredCheckSpec:
    """Ruleset-required status check plus optional GitHub App provenance."""

    context: str
    integration_id: int | None = None


@dataclass(frozen=True, slots=True)
class CheckRun:
    """Normalized GitHub check-run data used for release proof."""

    name: str
    status: str
    conclusion: str | None
    html_url: str | None
    app_id: int | None = None
    completed_at: str | None = None
    started_at: str | None = None
    check_suite_id: int | None = None
    head_sha: str | None = None


@dataclass(frozen=True, slots=True)
class CommitStatus:
    """Normalized legacy commit-status data used for release proof."""

    context: str
    state: str
    target_url: str | None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class RequiredCheckResult:
    """Evaluation result for one ruleset-required context."""

    context: str
    matched_name: str | None
    state: str
    url: str | None
    evidence_sha: str | None = None
    pull_request_number: int | None = None


@dataclass(frozen=True, slots=True)
class CommitIdentity:
    """Immutable commit identity admitted from GitHub."""

    sha: str
    tree_sha: str
    parents: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MergedPullRequest:
    """Trusted merged PR whose result is exactly the publication candidate."""

    number: int
    head_sha: str
    head_ref: str


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    """GitHub Actions provenance for SHA-scoped PR checks."""

    workflow_id: int
    run_number: int
    run_attempt: int
    check_suite_id: int
    head_sha: str
    head_branch: str
    path: str
    event: str
    repository: str
    head_repository: str
    status: str
    conclusion: str | None
    pull_request_numbers: tuple[int, ...]


def extract_required_contexts(ruleset: Mapping[str, Any]) -> tuple[str, ...]:
    """Extract required status-check contexts from a repository ruleset payload."""
    return tuple(spec.context for spec in extract_required_checks(ruleset))


def extract_required_checks(ruleset: Mapping[str, Any]) -> tuple[RequiredCheckSpec, ...]:
    """Extract required status-check contexts and app bindings from a ruleset payload."""
    required: list[RequiredCheckSpec] = []
    rules = ruleset.get("rules")
    if not isinstance(rules, list):
        raise ValueError("ruleset payload does not contain a rules list")

    for rule in rules:
        if not isinstance(rule, Mapping) or rule.get("type") != "required_status_checks":
            continue
        parameters = rule.get("parameters")
        if not isinstance(parameters, Mapping):
            raise ValueError("required_status_checks rule is missing parameters")
        required_checks = parameters.get("required_status_checks")
        if not isinstance(required_checks, list):
            raise ValueError("required_status_checks rule is missing required_status_checks list")
        for item in required_checks:
            if not isinstance(item, Mapping):
                raise ValueError("required status check entry is not an object")
            context = item.get("context")
            if not isinstance(context, str) or not context:
                raise ValueError("required status check entry has invalid context")
            integration_id = item.get("integration_id")
            if integration_id is not None and type(integration_id) is not int:
                raise ValueError("required status check entry has invalid integration_id")
            required.append(RequiredCheckSpec(context=context, integration_id=integration_id))

    if not required:
        raise ValueError("ruleset does not define any required status-check contexts")
    return tuple(dict.fromkeys(required))


def evaluate_required_checks(
    *,
    required_contexts: Sequence[str | RequiredCheckSpec],
    check_runs: Sequence[CheckRun],
    statuses: Sequence[CommitStatus],
    context_aliases: Mapping[str, Sequence[str]] = DEFAULT_CONTEXT_ALIASES,
) -> tuple[RequiredCheckResult, ...]:
    """Evaluate whether every required context has successful commit evidence."""
    results: list[RequiredCheckResult] = []
    for required in required_contexts:
        spec = _coerce_required_check(required)
        context = spec.context
        accepted_check_names = (context, *tuple(context_aliases.get(context, ())))
        check_run = _latest_check_run(check_runs, accepted_check_names, integration_id=spec.integration_id)
        if check_run is not None:
            results.append(
                RequiredCheckResult(
                    context=context,
                    matched_name=check_run.name,
                    state=_check_run_state(check_run),
                    url=check_run.html_url,
                )
            )
            continue

        if spec.integration_id is None:
            status = _latest_status(statuses, context)
            if status is not None:
                results.append(
                    RequiredCheckResult(
                        context=context,
                        matched_name=status.context,
                        state=status.state,
                        url=status.target_url,
                    )
                )
                continue

        results.append(RequiredCheckResult(context=context, matched_name=None, state="missing", url=None))
    return tuple(results)


def all_required_checks_succeeded(results: Iterable[RequiredCheckResult]) -> bool:
    """Return true only when every evaluated context succeeded."""
    return all(result.state == "success" for result in results)


def resolve_pr_only_checks(
    *,
    repo: str,
    image_sha: str,
    target_branch: str,
    token: str,
    required_checks: Sequence[RequiredCheckSpec],
    results: Sequence[RequiredCheckResult],
) -> tuple[RequiredCheckResult, ...]:
    """Recover only absent PR-only checks through verified result/head/tree identity.

    GitHub checks are SHA-scoped. Merged runs can have empty PR associations;
    the proof is a trusted expected PR workflow on the merged PR's exact head,
    not a claim that GitHub retained a unique per-PR execution attestation.
    """
    missing = {result.context for result in results if result.state == "missing" and result.context in PR_ONLY_WORKFLOWS}
    if not missing:
        return tuple(results)

    image = fetch_commit_identity(repo=repo, sha=image_sha, token=token)
    pull_request = fetch_merged_pull_request(repo=repo, image_sha=image_sha, target_branch=target_branch, token=token)
    if len(image.parents) not in {1, 2}:
        raise ValueError("publication result must have one or two parents")
    if len(image.parents) == 2 and image.parents[1] != pull_request.head_sha:
        raise ValueError("merged PR head does not match the result's second parent")
    head = fetch_commit_identity(repo=repo, sha=pull_request.head_sha, token=token)
    if head.tree_sha != image.tree_sha:
        raise ValueError("merged PR head and image commit have different trees; fresh exact-image proof is required")

    checks = fetch_check_runs(repo=repo, sha=head.sha, token=token)
    workflows = fetch_pr_workflow_runs(repo=repo, sha=head.sha, token=token)
    specs = {spec.context: spec for spec in required_checks}
    resolved: list[RequiredCheckResult] = []
    for result in results:
        if result.context not in missing:
            resolved.append(result)
            continue
        spec = specs[result.context]
        candidates = [
            run
            for run in workflows
            if run.path == PR_ONLY_WORKFLOWS[result.context]
            and run.event == "pull_request"
            and run.repository == repo
            and run.head_repository == repo
            and run.head_sha == head.sha
            and run.head_branch == pull_request.head_ref
            and (not run.pull_request_numbers or pull_request.number in run.pull_request_numbers)
        ]
        if not candidates:
            resolved.append(result)
            continue
        if len({run.workflow_id for run in candidates}) != 1:
            raise ValueError(f"ambiguous workflow identity for PR-only context {result.context!r}")
        # run_number increases for each new run of one workflow; run_attempt
        # advances on reruns. Completion time cannot make an older success win.
        latest_key = max((run.run_number, run.run_attempt) for run in candidates)
        latest_runs = {run for run in candidates if (run.run_number, run.run_attempt) == latest_key}
        if len(latest_runs) != 1:
            raise ValueError(f"ambiguous latest workflow attempt for {result.context!r}")
        latest = latest_runs.pop()
        if latest.status != "completed" or latest.conclusion != "success":
            resolved.append(
                RequiredCheckResult(
                    context=result.context,
                    matched_name=result.context,
                    state=_check_state(latest.status, latest.conclusion),
                    url=None,
                    evidence_sha=head.sha,
                    pull_request_number=pull_request.number,
                )
            )
            continue
        trusted_checks = [
            check
            for check in checks
            if check.check_suite_id == latest.check_suite_id and check.head_sha == head.sha and check.app_id == GITHUB_ACTIONS_APP_ID
        ]
        inherited = evaluate_required_checks(required_contexts=(spec,), check_runs=trusted_checks, statuses=())[0]
        resolved.append(
            RequiredCheckResult(
                context=inherited.context,
                matched_name=inherited.matched_name,
                state=inherited.state,
                url=inherited.url,
                evidence_sha=head.sha,
                pull_request_number=pull_request.number,
            )
        )
    return tuple(resolved)


def fetch_commit_identity(*, repo: str, sha: str, token: str) -> CommitIdentity:
    """Admit an exact immutable commit and tree, never a moving branch/tag ref."""
    if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ValueError("publication proof requires a full immutable commit SHA")
    payload = _object(_github_api_json(f"/repos/{repo}/git/commits/{sha}", token=token), "commit")
    actual_sha = _required_str(payload, "sha")
    if actual_sha != sha:
        raise ValueError("GitHub returned a different commit identity")
    tree = _object(payload.get("tree"), "commit tree")
    raw_parents = payload.get("parents")
    if not isinstance(raw_parents, list):
        raise ValueError("commit parents are not a list")
    return CommitIdentity(
        sha=actual_sha,
        tree_sha=_required_sha(tree, "sha"),
        parents=tuple(_required_sha(_object(parent, "commit parent"), "sha") for parent in raw_parents),
    )


def fetch_merged_pull_request(*, repo: str, image_sha: str, target_branch: str, token: str) -> MergedPullRequest:
    """Select exactly one trusted merged PR with this exact resulting commit."""
    pages = _github_api_json_pages(f"/repos/{repo}/commits/{image_sha}/pulls", token=token, params={"per_page": "100"})
    numbers: set[int] = set()
    for page in pages:
        if not isinstance(page, list):
            raise ValueError("associated pull-requests page was not a list")
        for raw in page:
            numbers.add(_required_int(_object(raw, "associated pull request"), "number"))
    candidates: list[MergedPullRequest] = []
    for number in sorted(numbers):
        payload = _object(_github_api_json(f"/repos/{repo}/pulls/{number}", token=token), "pull request")
        if _required_int(payload, "number") != number:
            raise ValueError("GitHub returned a different pull-request identity")
        if payload.get("merged") is not True or payload.get("state") != "closed":
            continue
        if payload.get("merge_commit_sha") != image_sha:
            continue
        if datetime.fromisoformat(_required_str(payload, "merged_at")).tzinfo is None:
            raise ValueError("merged PR timestamp lacks timezone")
        base = _object(payload.get("base"), "pull-request base")
        head = _object(payload.get("head"), "pull-request head")
        if (
            _required_str(base, "ref") != target_branch
            or _required_str(_object(base.get("repo"), "base repository"), "full_name") != repo
            or _required_str(_object(head.get("repo"), "head repository"), "full_name") != repo
        ):
            continue
        candidates.append(MergedPullRequest(number=number, head_sha=_required_str(head, "sha"), head_ref=_required_str(head, "ref")))
    if len(candidates) != 1:
        raise ValueError(f"expected one trusted merged PR producing {image_sha} on {target_branch!r}; found {len(candidates)}")
    return candidates[0]


def fetch_pr_workflow_runs(*, repo: str, sha: str, token: str) -> tuple[WorkflowRun, ...]:
    """Admit paginated workflow provenance; do not trust a check's details URL."""
    pages = _github_api_json_pages(
        f"/repos/{repo}/actions/runs", token=token, params={"head_sha": sha, "event": "pull_request", "per_page": "100"}
    )
    runs: list[WorkflowRun] = []
    for run in _records_from_pages(pages, "workflow_runs"):
        pull_requests = run.get("pull_requests")
        if not isinstance(pull_requests, list):
            raise ValueError("workflow run is missing pull_requests")
        runs.append(
            WorkflowRun(
                workflow_id=_required_int(run, "workflow_id"),
                run_number=_required_int(run, "run_number"),
                run_attempt=_required_int(run, "run_attempt"),
                check_suite_id=_required_int(run, "check_suite_id"),
                head_sha=_required_str(run, "head_sha"),
                head_branch=_required_str(run, "head_branch"),
                path=_required_str(run, "path"),
                event=_required_str(run, "event"),
                repository=_required_str(_object(run.get("repository"), "workflow repository"), "full_name"),
                head_repository=_required_str(_object(run.get("head_repository"), "workflow head repository"), "full_name"),
                status=_required_str(run, "status"),
                conclusion=_optional_str(run.get("conclusion")),
                pull_request_numbers=tuple(_required_int(_object(pr, "workflow pull request"), "number") for pr in pull_requests),
            )
        )
    return tuple(runs)


def fetch_ruleset_by_name(*, repo: str, ruleset_name: str, token: str) -> Mapping[str, Any]:
    """Fetch one active repository ruleset by name."""
    rulesets_payload = _github_api_json(f"/repos/{repo}/rulesets", token=token)
    if not isinstance(rulesets_payload, list):
        raise ValueError("GitHub rulesets response was not a list")

    matches = [
        item
        for item in rulesets_payload
        if isinstance(item, Mapping)
        and item.get("name") == ruleset_name
        and item.get("target") == "branch"
        and item.get("enforcement") == "active"
    ]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one active branch ruleset named {ruleset_name!r}, found {len(matches)}")

    ruleset_id = matches[0].get("id")
    if not isinstance(ruleset_id, int):
        raise ValueError(f"ruleset {ruleset_name!r} has invalid id {ruleset_id!r}")

    ruleset_payload = _github_api_json(f"/repos/{repo}/rulesets/{ruleset_id}", token=token)
    if not isinstance(ruleset_payload, Mapping):
        raise ValueError(f"GitHub ruleset {ruleset_id} response was not an object")
    return ruleset_payload


def fetch_check_runs(*, repo: str, sha: str, token: str) -> tuple[CheckRun, ...]:
    """Fetch all check runs for a commit SHA."""
    payloads = _github_api_json_pages(f"/repos/{repo}/commits/{sha}/check-runs", token=token, params={"per_page": "100"})
    runs: list[CheckRun] = []
    for item in _records_from_pages(payloads, "check_runs"):
        if _required_str(item, "head_sha") != sha:
            raise ValueError("check run head differs from requested commit SHA")
        runs.append(
            CheckRun(
                name=_required_str(item, "name"),
                status=_required_str(item, "status"),
                conclusion=_optional_str(item.get("conclusion")),
                html_url=_optional_str(item.get("html_url")),
                app_id=_optional_app_id(item.get("app")),
                completed_at=_optional_str(item.get("completed_at")),
                started_at=_optional_str(item.get("started_at")),
                check_suite_id=_optional_suite_id(item.get("check_suite")),
                head_sha=_required_str(item, "head_sha"),
            )
        )
    return tuple(runs)


def fetch_commit_statuses(*, repo: str, sha: str, token: str) -> tuple[CommitStatus, ...]:
    """Fetch legacy commit statuses for a commit SHA."""
    payload = _github_api_json(f"/repos/{repo}/commits/{sha}/status", token=token)
    if not isinstance(payload, Mapping):
        raise ValueError("GitHub commit status response was not an object")
    statuses = payload.get("statuses")
    if not isinstance(statuses, list):
        raise ValueError("GitHub commit status response is missing statuses list")

    result: list[CommitStatus] = []
    for item in statuses:
        if not isinstance(item, Mapping):
            raise ValueError("GitHub commit status entry was not an object")
        result.append(
            CommitStatus(
                context=_required_str(item, "context"),
                state=_required_str(item, "state"),
                target_url=_optional_str(item.get("target_url")),
                updated_at=_optional_str(item.get("updated_at")),
            )
        )
    return tuple(result)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="Repository in owner/name form.")
    parser.add_argument("--sha", required=True, help="Commit SHA represented by the release image.")
    parser.add_argument("--ruleset-name", default="main", help="Active branch ruleset name to mirror.")
    parser.add_argument("--target-branch", default="main", help="Trusted merge target for PR-only publication evidence.")
    args = parser.parse_args(argv)
    if re.fullmatch(r"[0-9a-f]{40}", args.sha) is None:
        print("A full immutable commit SHA is required for publication proof.", file=sys.stderr)
        return 2

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GH_TOKEN or GITHUB_TOKEN is required to verify release checks.", file=sys.stderr)
        return 2

    ruleset = fetch_ruleset_by_name(repo=args.repo, ruleset_name=args.ruleset_name, token=token)
    required_checks = extract_required_checks(ruleset)
    check_runs = fetch_check_runs(repo=args.repo, sha=args.sha, token=token)
    statuses = fetch_commit_statuses(repo=args.repo, sha=args.sha, token=token)
    results = evaluate_required_checks(required_contexts=required_checks, check_runs=check_runs, statuses=statuses)
    try:
        results = resolve_pr_only_checks(
            repo=args.repo,
            image_sha=args.sha,
            target_branch=args.target_branch,
            token=token,
            required_checks=required_checks,
            results=results,
        )
    except ValueError as exc:
        print(f"Cannot verify PR-only publication evidence: {exc}; refusing to publish image.", file=sys.stderr)
        return 1

    print(f"Required contexts from active ruleset {args.ruleset_name!r}:")
    for result in results:
        matched = result.matched_name or "<none>"
        url = result.url or "no URL"
        source = f" on {result.evidence_sha} through merged PR #{result.pull_request_number}" if result.evidence_sha else ""
        print(f"- {result.context}: {result.state} via {matched}{source} ({url})")

    if all_required_checks_succeeded(results):
        print(f"All ruleset-required checks succeeded for {args.sha}.")
        return 0

    sys.stdout.flush()
    print(f"Required checks are missing, pending, or failed for {args.sha}; refusing to publish image.", file=sys.stderr)
    return 1


def _check_run_state(check_run: CheckRun) -> str:
    return _check_state(check_run.status, check_run.conclusion)


def _check_state(status: str, conclusion: str | None) -> str:
    if status == "completed":
        return conclusion or "invalid"
    if status in {"queued", "in_progress", "requested", "waiting", "pending"}:
        return status
    return "invalid"


def _coerce_required_check(required: str | RequiredCheckSpec) -> RequiredCheckSpec:
    if isinstance(required, RequiredCheckSpec):
        return required
    return RequiredCheckSpec(context=required)


def _latest_check_run(
    check_runs: Sequence[CheckRun],
    accepted_names: Sequence[str],
    *,
    integration_id: int | None,
) -> CheckRun | None:
    matches = [run for run in check_runs if run.name in accepted_names and (integration_id is None or run.app_id == integration_id)]
    if not matches:
        return None
    # Queued runs can have neither timestamp. Their order cannot be proven;
    # a successful older run must not conceal unresolved evidence.
    unordered_refusals = [run for run in matches if not (run.started_at or run.completed_at) and _check_run_state(run) != "success"]
    if unordered_refusals:
        return unordered_refusals[0]
    return max(matches, key=lambda run: run.started_at or run.completed_at or "")


def _latest_status(statuses: Sequence[CommitStatus], context: str) -> CommitStatus | None:
    matches = [status for status in statuses if status.context == context]
    if not matches:
        return None
    return max(matches, key=lambda status: status.updated_at or "")


def _github_api_json(path: str, *, token: str) -> Any:
    with urllib.request.urlopen(_github_request(path, token=token), timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _github_api_json_pages(path: str, *, token: str, params: Mapping[str, str]) -> tuple[Any, ...]:
    next_url: str | None = _github_url(path, params=params)
    pages: list[Any] = []
    while next_url is not None:
        request = urllib.request.Request(
            next_url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            pages.append(json.loads(response.read().decode("utf-8")))
            next_url = _next_link(response.headers.get("Link"))
    return tuple(pages)


def _github_request(path: str, *, token: str) -> urllib.request.Request:
    return urllib.request.Request(
        _github_url(path, params={}),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def _github_url(path: str, *, params: Mapping[str, str]) -> str:
    query = urllib.parse.urlencode(params)
    url = f"{GITHUB_API_ROOT}{path}"
    if query:
        return f"{url}?{query}"
    return url


def _next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        url_part, _, rel_part = part.strip().partition(";")
        if 'rel="next"' not in rel_part:
            continue
        return url_part.strip()[1:-1]
    return None


def _required_str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"GitHub payload field {key!r} is missing or not a string")
    return value


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"GitHub {name} is not an object")
    return value


def _records_from_pages(pages: Sequence[Any], collection: str) -> tuple[Mapping[str, Any], ...]:
    """Reject incomplete or moving paginated proof rather than trust a subset."""
    if not pages:
        raise ValueError(f"GitHub {collection} proof has no response pages")
    expected: int | None = None
    records: list[Mapping[str, Any]] = []
    for raw_page in pages:
        page = _object(raw_page, f"{collection} page")
        total = page.get("total_count")
        if type(total) is not int or total < 0:
            raise ValueError(f"GitHub {collection} page has invalid total_count")
        if expected is not None and total != expected:
            raise ValueError(f"GitHub {collection} count changed during pagination")
        expected = total
        items = page.get(collection)
        if not isinstance(items, list):
            raise ValueError(f"GitHub page is missing {collection}")
        records.extend(_object(item, collection) for item in items)
    if len(records) != expected:
        raise ValueError(f"GitHub {collection} pagination is incomplete: expected {expected}, received {len(records)}")
    return tuple(records)


def _required_int(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    if type(value) is not int or value <= 0:
        raise ValueError(f"GitHub payload field {key!r} is missing or not a positive integer")
    return value


def _required_sha(data: Mapping[str, Any], key: str) -> str:
    value = _required_str(data, key)
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError(f"GitHub field {key!r} is not a full immutable SHA")
    return value


def _optional_suite_id(value: Any) -> int | None:
    if value is None:
        return None
    return _required_int(_object(value, "check suite"), "id")


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"GitHub payload optional string field had invalid value {value!r}")
    return value


def _optional_app_id(value: Any) -> int | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"GitHub check run app field had invalid value {value!r}")
    app_id = value.get("id")
    if app_id is None:
        return None
    if type(app_id) is not int:
        raise ValueError(f"GitHub check run app.id had invalid value {app_id!r}")
    return app_id


if __name__ == "__main__":
    sys.exit(main())
