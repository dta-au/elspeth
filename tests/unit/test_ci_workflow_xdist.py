"""CI workflow invariants for pytest parallel execution."""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from elspeth.testing.pytest_xdist_auto import pytest_cmdline_main

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yaml"
ACTIONLINT_CONFIG = REPO_ROOT / ".github" / "actionlint.yaml"
JUDGE_GATES_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "enforce-allowlist-judge-gates.yaml"
CODEQL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "codeql.yaml"
CODEQL_CONFIG = REPO_ROOT / ".github" / "codeql" / "codeql-config.yml"
PR_WORKFLOWS = (
    CI_WORKFLOW,
    CODEQL_WORKFLOW,
    REPO_ROOT / ".github" / "workflows" / "composer-redaction-gate.yml",
    JUDGE_GATES_WORKFLOW,
    REPO_ROOT / ".github" / "workflows" / "enforce-telemetry-backfill-trailer.yaml",
)
_SHELL_CONTROL_TOKENS = frozenset({"&&", "||", ";", "|"})


def _workflow(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), f"{path.name} workflow YAML root must be a mapping"
    return raw


def _ci_workflow() -> dict[str, Any]:
    return _workflow(CI_WORKFLOW)


def _step_run(job: dict[str, Any], step_name: str) -> str:
    step = _step(job, step_name)
    run = step.get("run")
    assert isinstance(run, str), f"{step_name!r} must have a shell run block"
    return run


def _step(job: dict[str, Any], step_name: str) -> dict[str, Any]:
    for step in job["steps"]:
        if step.get("name") == step_name:
            assert isinstance(step, dict), f"{step_name!r} must be a mapping"
            return step
    raise AssertionError(f"Missing CI step {step_name!r}")


def _step_index(job: dict[str, Any], step_name: str) -> int:
    for index, step in enumerate(job["steps"]):
        if step.get("name") == step_name:
            return index
    raise AssertionError(f"Missing CI step {step_name!r}")


def _embedded_python(run: str, marker: str) -> str:
    """Extract one marker-delimited Python heredoc from a workflow step."""
    lines = run.splitlines()
    start = lines.index(marker)
    end = lines.index("PY", start + 1)
    return "\n".join(lines[start + 1 : end]) + "\n"


class _ApiResponse(io.BytesIO):
    """Minimal context-managed HTTP response for embedded resolver tests."""

    def __init__(self, payload: object, *, link: str | None = None) -> None:
        super().__init__(json.dumps(payload).encode())
        self.headers = {"Link": link} if link is not None else {}


def _ratchet_resolver_script() -> str:
    workflow = _ci_workflow()
    run = _step_run(
        workflow["jobs"]["static-analysis"],
        "Reject touched or broadened permanent multi-rule per-file blankets",
    )
    return _embedded_python(run, "# resolve-nearest-successful-ratchet-baseline")


def _set_ratchet_resolver_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.invalid")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("CURRENT_RUN_ID", "999")
    monkeypatch.setenv("GITHUB_RUN_ID", "999")
    monkeypatch.setenv("GITHUB_SHA", "f" * 40)
    monkeypatch.setenv("GH_TOKEN", "read-only-token")


def _workflow_run(run_id: int, sha: str, *, conclusion: str) -> dict[str, object]:
    return {
        "id": run_id,
        "head_sha": sha,
        "event": "push",
        "status": "completed",
        "conclusion": conclusion,
    }


def _ratchet_job(run_id: int, sha: str, *, step_conclusion: str, attempt: int = 1) -> dict[str, object]:
    return {
        "id": run_id * 10 + attempt,
        "run_id": run_id,
        "run_attempt": attempt,
        "head_sha": sha,
        "name": "Static analysis",
        "steps": [
            {
                "name": "Reject touched or broadened permanent multi-rule per-file blankets",
                "status": "completed",
                "conclusion": step_conclusion,
            }
        ],
    }


def _pytest_args(run: str) -> list[str]:
    lexer = shlex.shlex(run.replace("\\\n", " "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    tokens = list(lexer)
    for index in range(len(tokens)):
        if tokens[index : index + 3] == ["uv", "run", "pytest"]:
            start = index + 3
        elif tokens[index : index + 5] == ["uv", "run", "python", "-m", "pytest"]:
            start = index + 5
        else:
            continue

        args: list[str] = []
        for token in tokens[start:]:
            if token in _SHELL_CONTROL_TOKENS:
                break
            args.append(token)
        return args
    raise AssertionError("Missing pytest invocation")


def _pytest_numprocesses_values(run: str) -> list[str]:
    values: list[str] = []
    args = _pytest_args(run)
    for index, arg in enumerate(args):
        if arg == "-n" and index + 1 < len(args):
            values.append(args[index + 1])
        elif arg.startswith("-n") and arg != "-n":
            values.append(arg[2:])
        elif arg == "--numprocesses" and index + 1 < len(args):
            values.append(args[index + 1])
        elif arg.startswith("--numprocesses="):
            values.append(arg.split("=", 1)[1])
    return values


@pytest.mark.parametrize(
    ("flag_args", "expected"),
    (
        ("-n0", "0"),
        ("-n 0", "0"),
        ("--numprocesses 0", "0"),
        ("--numprocesses=0", "0"),
        ("-nauto", "auto"),
        ("-n auto", "auto"),
        ("--numprocesses auto", "auto"),
        ("--numprocesses=auto", "auto"),
    ),
)
def test_pytest_numprocesses_values_tokenizes_supported_cli_forms(flag_args: str, expected: str) -> None:
    run = f"uv run pytest tests/ {flag_args}"

    assert _pytest_numprocesses_values(run) == [expected]


@pytest.mark.parametrize(
    ("run", "expected_args"),
    (
        ("uv run pytest tests/ -n 0|| status=$?", ["tests/", "-n", "0"]),
        ("uv run pytest tests/ -n auto&& echo done", ["tests/", "-n", "auto"]),
        ("uv run pytest tests/ --numprocesses=auto; echo done", ["tests/", "--numprocesses=auto"]),
    ),
)
def test_pytest_args_stop_at_attached_shell_control_operators(run: str, expected_args: list[str]) -> None:
    assert _pytest_args(run) == expected_args


def test_python_matrix_ci_does_not_hard_disable_xdist() -> None:
    """Remote Python test lanes must leave xdist available instead of forcing ``-n 0``."""
    workflow = _ci_workflow()
    test_job = workflow["jobs"]["test"]

    coverage_run = _step_run(test_job, "Run tests with coverage")
    no_coverage_run = _step_run(test_job, "Run tests without coverage")

    assert "0" not in _pytest_numprocesses_values(coverage_run)
    assert "0" not in _pytest_numprocesses_values(no_coverage_run)


def test_integration_lane_does_not_force_parallel_xdist() -> None:
    """Integration lane stays sequential by omitting explicit xdist process flags."""
    workflow = _ci_workflow()
    integration_job = workflow["jobs"]["integration"]

    run = _step_run(integration_job, "Run integration tests")

    assert _pytest_numprocesses_values(run) == []


def test_python_matrix_documents_coverage_lane_choice() -> None:
    """The 3.12/3.13 coverage split must carry its rationale in the workflow."""
    workflow_text = CI_WORKFLOW.read_text(encoding="utf-8")
    normalized = " ".join(workflow_text.split())

    assert "Coverage runs on Python 3.13 only" in normalized
    assert "Python 3.12 lane remains" in normalized
    assert "dependency-compatibility signal" in normalized


def test_judge_gates_workflow_mirrors_ci_concurrency_policy() -> None:
    """Policy-gate workflow must not race push and PR runs for one ref."""
    ci_workflow = _ci_workflow()
    judge_workflow = _workflow(JUDGE_GATES_WORKFLOW)

    assert judge_workflow["concurrency"] == ci_workflow["concurrency"]


def test_judge_gates_required_context_matches_emitted_check_name() -> None:
    """Branch-protection docs must name the check context GitHub emits."""
    workflow_text = JUDGE_GATES_WORKFLOW.read_text(encoding="utf-8")
    required_context = re.search(
        r"Branch protection MUST require ``(?P<context>[^`]+)``",
        workflow_text,
    )
    assert required_context is not None, "workflow header must document the required context"

    judge_workflow = _workflow(JUDGE_GATES_WORKFLOW)
    aggregate_job = judge_workflow["jobs"]["judge-gates-success"]

    assert required_context.group("context") == aggregate_job["name"]


def test_judge_gates_workflow_has_bounded_job_timeouts() -> None:
    """Judge-gate jobs must not inherit GitHub's six-hour default timeout."""
    judge_workflow = _workflow(JUDGE_GATES_WORKFLOW)

    for job_name in ("check-override-rate",):
        job = judge_workflow["jobs"][job_name]
        assert job["timeout-minutes"] == 15


def test_codeql_security_suites_do_not_filter_by_problem_severity() -> None:
    """Security suites must not drop security queries whose problem severity is warning.

    security-extended carries the complete security query set, including
    warning-severity queries; security-and-quality only adds non-security
    quality queries on top of it, so dropping that suite (1a524d260) does
    not filter security coverage by problem severity.
    """
    workflow = _workflow(CODEQL_WORKFLOW)
    init_step = _step(workflow["jobs"]["analyze"], "Initialize CodeQL")
    queries = init_step["with"]["queries"]
    assert "security-extended" in queries

    config = _workflow(CODEQL_CONFIG)
    for query_filter in config.get("query-filters", ()):
        exclude = query_filter.get("exclude", {})
        assert "problem.severity" not in exclude


def test_override_rate_workflow_pins_threshold_policy() -> None:
    """C3 threshold is CI policy and must be explicit in workflow YAML."""
    judge_workflow = _workflow(JUDGE_GATES_WORKFLOW)
    job = judge_workflow["jobs"]["check-override-rate"]

    run = _step_run(job, "Run check-override-rate")

    assert "--max-rate 0.10" in run


def test_override_rate_workflow_surfaces_pass_notice_in_step_summary() -> None:
    """C3 PASS/insufficient-data notices must be visible outside raw job logs."""
    judge_workflow = _workflow(JUDGE_GATES_WORKFLOW)
    job = judge_workflow["jobs"]["check-override-rate"]

    run = _step_run(job, "Run check-override-rate")

    assert "GITHUB_STEP_SUMMARY" in run
    assert "Override-rate drift gate" in run


def test_integration_job_runs_on_rc_and_release_branch_pushes() -> None:
    """RC and maintained release pushes must not skip the integration lane."""
    workflow = _ci_workflow()
    integration_job = workflow["jobs"]["integration"]

    condition = integration_job["if"]

    assert "github.event_name == 'push'" in condition
    assert "refs/heads/main" in condition
    assert "startsWith(github.ref, 'refs/heads/RC')" in condition
    assert "startsWith(github.ref, 'refs/heads/release/')" in condition


def test_integration_lane_fails_closed_on_real_test_failures() -> None:
    """A real integration failure must fail the lane.

    The historical ``... || echo "Integration tests skipped (no API keys)"``
    swallowed *every* non-zero pytest exit — assertion regressions, collection
    errors, import failures, and infra faults all left the job green, and
    ``build-push.yaml`` would then build an image off a broken CI run. The lane
    must propagate real failures and tolerate only pytest's exit code 5 ("no
    tests collected").
    """
    workflow = _ci_workflow()
    integration_job = workflow["jobs"]["integration"]
    run = _step_run(integration_job, "Run integration tests")

    # The blanket failure-swallow must be gone.
    assert "|| echo" not in run
    # Real failures propagate via the captured status.
    assert 'exit "$status"' in run
    # Only "no tests collected" (pytest exit 5) is tolerated as a skip.
    assert "-eq 5" in run


def test_xdist_auto_defaults_to_parallel_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local pytest runs default to xdist when no process count is explicit."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    config = SimpleNamespace(option=SimpleNamespace(numprocesses=None))

    pytest_cmdline_main(config)  # type: ignore[arg-type]

    assert config.option.numprocesses == "auto"


def test_xdist_auto_noops_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI controllers stay sequential for clearer failure output."""
    monkeypatch.setenv("CI", "1")
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    config = SimpleNamespace(option=SimpleNamespace(numprocesses=None))

    pytest_cmdline_main(config)  # type: ignore[arg-type]

    assert config.option.numprocesses is None


def test_xdist_auto_stays_sequential_for_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Coverage jobs stay sequential because pytest-cov owns worker coordination."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    config = SimpleNamespace(option=SimpleNamespace(cov_source=["src/elspeth"], numprocesses=None))

    pytest_cmdline_main(config)  # type: ignore[arg-type]

    assert config.option.numprocesses is None


def test_xdist_auto_noops_inside_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Workers must not recursively auto-enable xdist."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    config = SimpleNamespace(option=SimpleNamespace(numprocesses=None))

    pytest_cmdline_main(config)  # type: ignore[arg-type]

    assert config.option.numprocesses is None


def test_xdist_auto_preserves_explicit_process_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit pytest ``-n`` choices remain authoritative."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    config = SimpleNamespace(option=SimpleNamespace(numprocesses=4))

    pytest_cmdline_main(config)  # type: ignore[arg-type]

    assert config.option.numprocesses == 4


def test_static_analysis_runs_composer_skill_inventory_drift_gate() -> None:
    """Generated composer skill inventory must be checked in CI, not only pre-commit."""
    workflow = _ci_workflow()
    static_analysis = workflow["jobs"]["static-analysis"]

    run = _step_run(static_analysis, "Check composer skill tool inventory")

    assert "scripts/cicd/generate_skill_inventory.py --check" in run


def test_static_analysis_runs_actionlint_with_repo_policy_config() -> None:
    """Workflow syntax and self-hosted runner labels must be checked in CI."""
    workflow = _ci_workflow()
    static_analysis = workflow["jobs"]["static-analysis"]

    step = _step(static_analysis, "Check GitHub workflows (actionlint)")
    assert step["shell"] == "bash"
    env = step.get("env")
    assert isinstance(env, dict), "actionlint step must pin version and checksum"
    assert env["ACTIONLINT_VERSION"] == "1.7.12"
    assert env["ACTIONLINT_SHA256"] == "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8"

    run = _step_run(static_analysis, "Check GitHub workflows (actionlint)")
    assert "sha256sum -c -" in run
    assert "-config-file .github/actionlint.yaml" in run
    assert ".github/workflows/*.yml" in run
    assert ".github/workflows/*.yaml" in run


def test_actionlint_policy_declares_self_hosted_runner_labels() -> None:
    policy = _workflow(ACTIONLINT_CONFIG)

    labels = policy["self-hosted-runner"]["labels"]

    assert {"nyx-ci", "trusted"} <= set(labels)


@pytest.mark.parametrize("workflow_path", PR_WORKFLOWS, ids=lambda path: path.name)
def test_pull_request_workflows_include_release_branches(workflow_path: Path) -> None:
    """PR checks must run when a change targets a maintained release branch."""
    workflow_text = workflow_path.read_text(encoding="utf-8")
    pull_request = re.search(r"(?m)^  pull_request:\n    branches: \[(?P<branches>[^]]+)]$", workflow_text)

    assert pull_request is not None, f"{workflow_path.name} must declare pull_request branch filters"
    assert '"release/**"' in pull_request.group("branches")


@pytest.mark.parametrize("workflow_path", (CI_WORKFLOW, CODEQL_WORKFLOW, JUDGE_GATES_WORKFLOW), ids=lambda path: path.name)
def test_push_workflows_include_release_branches(workflow_path: Path) -> None:
    """Merged release-branch changes must receive post-merge CI signal."""
    workflow_text = workflow_path.read_text(encoding="utf-8")
    push = re.search(r"(?m)^  push:\n    branches: \[(?P<branches>[^]]+)]$", workflow_text)

    assert push is not None, f"{workflow_path.name} must declare push branch filters"
    assert '"release/**"' in push.group("branches")


def test_pull_request_jobs_never_use_the_trusted_runner() -> None:
    """No PR-controlled workflow code may execute on the persistent nyx runner."""
    for workflow_path in PR_WORKFLOWS:
        workflow = _workflow(workflow_path)
        for job_name, job in workflow["jobs"].items():
            selector = str(job["runs-on"])
            if "nyx-ci" not in selector:
                continue
            condition = str(job.get("if", ""))
            assert "github.event_name != 'pull_request'" in selector or "github.event_name != 'pull_request'" in condition, (
                f"{workflow_path.name}:{job_name} can route PR code to nyx"
            )
            assert "head.repo.full_name == github.repository" not in selector


def test_static_analysis_signed_allowlist_steps_are_keyless_for_every_pr() -> None:
    """PR code gets shape/binding checks but never receives the operator HMAC key."""
    workflow = _ci_workflow()
    static_analysis = workflow["jobs"]["static-analysis"]
    expected_secret = "${{ github.event_name != 'pull_request' && secrets.ELSPETH_JUDGE_METADATA_HMAC_KEY || '' }}"

    for step_name in (
        "Run trust-tier elspeth-lints rule",
        "Run trust-boundary honesty-gate elspeth-lints rules",
        "Emit elspeth-lints trust-tier SARIF artifact",
    ):
        step = _step(static_analysis, step_name)
        env = step.get("env")
        assert isinstance(env, dict), f"{step_name!r} must define step env"
        assert env.get("ELSPETH_JUDGE_METADATA_HMAC_KEY") == expected_secret
        verify_mode = env.get("ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE")
        assert isinstance(verify_mode, str), f"{step_name!r} must define signature verification mode"
        assert verify_mode == "${{ github.event_name == 'pull_request' && 'shape-only-when-key-missing' || 'required' }}"


def test_static_analysis_all_prs_reject_unverified_signed_allowlist_edits() -> None:
    """No keyless PR may add or mutate signed metadata and claim authority."""
    workflow = _ci_workflow()
    static_analysis = workflow["jobs"]["static-analysis"]

    step_name = "Reject unverified PR signed allowlist edits"
    step = _step(static_analysis, step_name)
    assert step.get("if") == "${{ github.event_name == 'pull_request' }}"

    run = _step_run(static_analysis, step_name)
    assert "git fetch --no-tags --depth=1 origin ${{ github.event.pull_request.base.sha }}" in run
    assert "check-judge-coverage" in run
    assert "--forbid-unverified-judge-metadata" in run
    assert "--allowlist-root config/cicd/enforce_tier_model" in run
    assert "--allowlist-root config/cicd/enforce_trust_boundary_honesty" in run
    assert "--baseline-ref ${{ github.event.pull_request.base.sha }}" in run

    gate_index = _step_index(static_analysis, step_name)
    assert gate_index < _step_index(static_analysis, "Run trust-tier elspeth-lints rule")
    assert gate_index < _step_index(static_analysis, "Run trust-boundary honesty-gate elspeth-lints rules")


def test_static_analysis_ratchets_permanent_multi_rule_blankets_repo_wide_on_prs_and_pushes() -> None:
    """The transitional blanket debt may shrink after merge but never grow."""
    workflow = _ci_workflow()
    static_analysis = workflow["jobs"]["static-analysis"]
    assert workflow["permissions"] == {"actions": "read", "contents": "read"}

    step_name = "Reject touched or broadened permanent multi-rule per-file blankets"
    step = _step(static_analysis, step_name)
    assert "if" not in step, "blanket ratchet must run for protected pushes as well as PRs"
    env = step.get("env")
    assert isinstance(env, dict)
    assert env.get("PR_BASELINE_REF") == "${{ github.event_name == 'pull_request' && github.event.pull_request.base.sha || '' }}"
    assert env.get("GH_TOKEN") == "${{ github.event_name != 'pull_request' && github.token || '' }}"
    assert env.get("CURRENT_RUN_ID") == "${{ github.run_id }}"

    run = _step_run(static_analysis, step_name)
    assert 'BASELINE_REF="$PR_BASELINE_REF"' in run, "PRs must preserve the event's exact base SHA"
    assert "github.event.before" not in str(step), "a cancelled push predecessor is not policy authority"
    assert "default_branch" not in str(step), "a divergent default branch is not release debt authority"
    assert "actions/workflows/ci.yaml/runs" in run
    assert "actions/runs/{run_id}/jobs" in run
    assert '"event": "push"' in run
    assert '{"filter": "all"' in run, "rerun attempts must be visible"
    assert 'rel="next"' in run, "workflow-run and job result sets must be paginated"
    assert '["git", "rev-list", "--parents"' in run
    assert "first-parent" in run, "merge authority must stay on the protected branch lineage"
    assert "Reject touched or broadened permanent multi-rule per-file blankets" in run
    assert "No successful ancestor baseline was found" in run
    assert "check-per-file-blanket-ratchet" in run
    assert '--baseline-ref "$BASELINE_REF"' in run
    assert "--allowlist-root config/cicd" in run
    assert "--repo-root ." in run

    gate_index = _step_index(static_analysis, step_name)
    assert gate_index < _step_index(static_analysis, "Reject unverified PR signed allowlist edits")
    assert gate_index < _step_index(static_analysis, "Run trust-tier elspeth-lints rule")


def test_push_ratchet_resolver_accepts_successful_step_from_failed_workflow(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Later job failures must not erase this exact ratchet step's successful authority."""
    _set_ratchet_resolver_env(monkeypatch)
    baseline_sha = "e" * 40

    def urlopen(request: urllib.request.Request, **_kwargs: object) -> _ApiResponse:
        if "/actions/workflows/ci.yaml/runs?" in request.full_url:
            return _ApiResponse({"workflow_runs": [_workflow_run(200, baseline_sha, conclusion="failure")]})
        if "/actions/runs/200/jobs?" in request.full_url:
            return _ApiResponse({"jobs": [_ratchet_job(200, baseline_sha, step_conclusion="success")]})
        raise AssertionError(request.full_url)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=f"{'f' * 40} {baseline_sha}\n{baseline_sha}\n"),
    )

    exec(compile(_ratchet_resolver_script(), "<ratchet-baseline-resolver>", "exec"), {"__name__": "__main__"})

    assert capsys.readouterr().out.splitlines() == [baseline_sha]


def test_push_ratchet_resolver_ignores_failed_and_skipped_steps(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only a completed successful execution of the exact ratchet step is authority."""
    _set_ratchet_resolver_env(monkeypatch)
    failed_sha = "e" * 40
    skipped_sha = "d" * 40
    successful_sha = "c" * 40
    runs = [
        _workflow_run(300, failed_sha, conclusion="failure"),
        _workflow_run(200, skipped_sha, conclusion="cancelled"),
        _workflow_run(100, successful_sha, conclusion="failure"),
    ]

    def urlopen(request: urllib.request.Request, **_kwargs: object) -> _ApiResponse:
        if "/actions/workflows/ci.yaml/runs?" in request.full_url:
            return _ApiResponse({"workflow_runs": runs})
        run_id = int(re.search(r"/actions/runs/(\d+)/jobs", request.full_url).group(1))  # type: ignore[union-attr]
        sha, conclusion = {
            300: (failed_sha, "failure"),
            200: (skipped_sha, "skipped"),
            100: (successful_sha, "success"),
        }[run_id]
        return _ApiResponse({"jobs": [_ratchet_job(run_id, sha, step_conclusion=conclusion)]})

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    graph = f"{'f' * 40} {failed_sha}\n{failed_sha} {skipped_sha}\n{skipped_sha} {successful_sha}\n{successful_sha}\n"
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout=graph))

    exec(compile(_ratchet_resolver_script(), "<ratchet-baseline-resolver>", "exec"), {"__name__": "__main__"})

    assert capsys.readouterr().out.splitlines() == [successful_sha]


def test_push_ratchet_resolver_paginates_runs_and_rerun_jobs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Authorities beyond page one and in a successful rerun attempt remain discoverable."""
    _set_ratchet_resolver_env(monkeypatch)
    baseline_sha = "e" * 40
    # GitHub canonicalizes Link URLs to the numeric /repositories/{id} route.
    runs_page_two = "https://api.github.invalid/repositories/123/actions/workflows/ci.yaml/runs?event=push&per_page=100&page=2"
    jobs_page_two = "https://api.github.invalid/repositories/123/actions/runs/200/jobs?filter=all&per_page=100&page=2"

    def urlopen(request: urllib.request.Request, **_kwargs: object) -> _ApiResponse:
        if request.full_url == runs_page_two:
            return _ApiResponse({"workflow_runs": [_workflow_run(200, baseline_sha, conclusion="failure")]})
        if "/actions/workflows/ci.yaml/runs?" in request.full_url:
            return _ApiResponse({"workflow_runs": []}, link=f'<{runs_page_two}>; rel="next"')
        if request.full_url == jobs_page_two:
            return _ApiResponse({"jobs": [_ratchet_job(200, baseline_sha, step_conclusion="success", attempt=2)]})
        if "/actions/runs/200/jobs?" in request.full_url:
            return _ApiResponse(
                {"jobs": [_ratchet_job(200, baseline_sha, step_conclusion="failure", attempt=1)]},
                link=f'<{jobs_page_two}>; rel="next"',
            )
        raise AssertionError(request.full_url)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=f"{'f' * 40} {baseline_sha}\n{baseline_sha}\n"),
    )

    exec(compile(_ratchet_resolver_script(), "<ratchet-baseline-resolver>", "exec"), {"__name__": "__main__"})

    assert capsys.readouterr().out.splitlines() == [baseline_sha]


def test_push_ratchet_resolver_uses_graph_nearest_authority_not_api_order(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Older debt cannot be restored merely because its successful run is listed first."""
    _set_ratchet_resolver_env(monkeypatch)
    older_sha = "c" * 40
    newer_sha = "e" * 40
    runs = [
        _workflow_run(100, older_sha, conclusion="success"),
        _workflow_run(200, newer_sha, conclusion="failure"),
    ]

    def urlopen(request: urllib.request.Request, **_kwargs: object) -> _ApiResponse:
        if "/actions/workflows/ci.yaml/runs?" in request.full_url:
            return _ApiResponse({"workflow_runs": runs})
        match = re.search(r"/actions/runs/(\d+)/jobs", request.full_url)
        assert match is not None
        run_id = int(match.group(1))
        sha = {100: older_sha, 200: newer_sha}[run_id]
        return _ApiResponse({"jobs": [_ratchet_job(run_id, sha, step_conclusion="success")]})

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    graph = f"{'f' * 40} {newer_sha}\n{newer_sha} {older_sha}\n{older_sha}\n"
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout=graph))

    exec(compile(_ratchet_resolver_script(), "<ratchet-baseline-resolver>", "exec"), {"__name__": "__main__"})

    assert capsys.readouterr().out.splitlines() == [newer_sha]


def test_push_ratchet_resolver_excludes_every_run_at_current_sha(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A duplicate successful run at GITHUB_SHA must not make the current tree its own baseline."""
    _set_ratchet_resolver_env(monkeypatch)
    current_sha = "f" * 40
    parent_sha = "e" * 40
    runs = [
        _workflow_run(300, current_sha, conclusion="failure"),
        _workflow_run(200, parent_sha, conclusion="failure"),
    ]

    def urlopen(request: urllib.request.Request, **_kwargs: object) -> _ApiResponse:
        if "/actions/workflows/ci.yaml/runs?" in request.full_url:
            return _ApiResponse({"workflow_runs": runs})
        match = re.search(r"/actions/runs/(\d+)/jobs", request.full_url)
        assert match is not None
        run_id = int(match.group(1))
        sha = {200: parent_sha, 300: current_sha}[run_id]
        return _ApiResponse({"jobs": [_ratchet_job(run_id, sha, step_conclusion="success")]})

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=f"{current_sha} {parent_sha}\n{parent_sha}\n"),
    )

    exec(compile(_ratchet_resolver_script(), "<ratchet-baseline-resolver>", "exec"), {"__name__": "__main__"})

    assert capsys.readouterr().out.splitlines() == [parent_sha]


def test_push_ratchet_resolver_uses_first_parent_authority_at_merge(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A debt-bearing second parent cannot grandfather reintroduction at a merge."""
    _set_ratchet_resolver_env(monkeypatch)
    current_sha = "f" * 40
    stricter_first_parent_sha = "e" * 40
    debt_bearing_second_parent_sha = "a" * 40
    shared_ancestor_sha = "9" * 40
    runs = [
        _workflow_run(100, debt_bearing_second_parent_sha, conclusion="failure"),
        _workflow_run(200, stricter_first_parent_sha, conclusion="failure"),
    ]

    def urlopen(request: urllib.request.Request, **_kwargs: object) -> _ApiResponse:
        if "/actions/workflows/ci.yaml/runs?" in request.full_url:
            return _ApiResponse({"workflow_runs": runs})
        match = re.search(r"/actions/runs/(\d+)/jobs", request.full_url)
        assert match is not None
        run_id = int(match.group(1))
        sha = {100: debt_bearing_second_parent_sha, 200: stricter_first_parent_sha}[run_id]
        return _ApiResponse({"jobs": [_ratchet_job(run_id, sha, step_conclusion="success")]})

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    graph = (
        f"{current_sha} {stricter_first_parent_sha} {debt_bearing_second_parent_sha}\n"
        f"{stricter_first_parent_sha} {shared_ancestor_sha}\n"
        f"{debt_bearing_second_parent_sha} {shared_ancestor_sha}\n"
        f"{shared_ancestor_sha}\n"
    )
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout=graph))

    exec(compile(_ratchet_resolver_script(), "<ratchet-baseline-resolver>", "exec"), {"__name__": "__main__"})

    assert capsys.readouterr().out.splitlines() == [stricter_first_parent_sha]


def test_push_ratchet_resolver_rejects_successful_non_ancestor_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A successful step on another branch cannot become authority without ancestry."""
    _set_ratchet_resolver_env(monkeypatch)
    unrelated_sha = "a" * 40

    def urlopen(request: urllib.request.Request, **_kwargs: object) -> _ApiResponse:
        if "/actions/workflows/ci.yaml/runs?" in request.full_url:
            return _ApiResponse({"workflow_runs": [_workflow_run(100, unrelated_sha, conclusion="success")]})
        raise AssertionError("non-ancestor workflow jobs must not be queried")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout=f"{'f' * 40}\n"))

    with pytest.raises(SystemExit) as exc_info:
        exec(compile(_ratchet_resolver_script(), "<ratchet-baseline-resolver>", "exec"), {"__name__": "__main__"})

    assert exc_info.value.code == 2
    assert "No successful ancestor baseline was found" in capsys.readouterr().err


def test_push_ratchet_resolver_fails_closed_on_malformed_ratchet_job(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_ratchet_resolver_env(monkeypatch)
    baseline_sha = "e" * 40

    def urlopen(request: urllib.request.Request, **_kwargs: object) -> _ApiResponse:
        if "/actions/workflows/ci.yaml/runs?" in request.full_url:
            return _ApiResponse({"workflow_runs": [_workflow_run(200, baseline_sha, conclusion="failure")]})
        if "/actions/runs/200/jobs?" in request.full_url:
            malformed_job = _ratchet_job(200, baseline_sha, step_conclusion="success")
            malformed_job["steps"] = "not-a-list"
            return _ApiResponse({"jobs": [malformed_job]})
        raise AssertionError(request.full_url)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=f"{'f' * 40} {baseline_sha}\n{baseline_sha}\n"),
    )

    with pytest.raises(SystemExit) as exc_info:
        exec(compile(_ratchet_resolver_script(), "<ratchet-baseline-resolver>", "exec"), {"__name__": "__main__"})

    assert exc_info.value.code == 2
    assert "malformed ratchet job" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("response", "expected_error"),
    (
        (b"{", "could not read workflow runs"),
        (b'{"workflow_runs": "not-a-list"}', "malformed Actions API response"),
        (
            b'{"workflow_runs": [{"id": 1, "head_sha": "not-a-sha", "event": "push", "status": "completed", "conclusion": "failure"}]}',
            "malformed workflow run",
        ),
    ),
)
def test_push_ratchet_baseline_resolver_fails_closed_on_malformed_api_results(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    response: bytes,
    expected_error: str,
) -> None:
    _set_ratchet_resolver_env(monkeypatch)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: io.BytesIO(response))
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout=f"{'f' * 40}\n"))

    with pytest.raises(SystemExit) as exc_info:
        exec(compile(_ratchet_resolver_script(), "<ratchet-baseline-resolver>", "exec"), {"__name__": "__main__"})

    assert exc_info.value.code == 2
    assert expected_error in capsys.readouterr().err


def test_push_ratchet_baseline_resolver_fails_closed_when_actions_api_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_ratchet_resolver_env(monkeypatch)

    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", unavailable)
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout=f"{'f' * 40}\n"))

    with pytest.raises(SystemExit) as exc_info:
        exec(compile(_ratchet_resolver_script(), "<ratchet-baseline-resolver>", "exec"), {"__name__": "__main__"})

    assert exc_info.value.code == 2
    assert "could not read workflow runs" in capsys.readouterr().err


def test_push_ratchet_baseline_resolver_fails_closed_without_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_ratchet_resolver_env(monkeypatch)
    monkeypatch.delenv("GH_TOKEN")

    with pytest.raises(SystemExit) as exc_info:
        exec(compile(_ratchet_resolver_script(), "<ratchet-baseline-resolver>", "exec"), {"__name__": "__main__"})

    assert exc_info.value.code == 2
    assert "GH_TOKEN is unavailable" in capsys.readouterr().err


def test_new_branch_full_shell_uses_nearest_cross_branch_ratchet_authority(tmp_path: Path) -> None:
    """A release-derived branch bootstraps from its nearest vetted ancestor, not stale main."""
    remote = tmp_path / "remote.git"
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "ci@example.invalid"], cwd=checkout, check=True)
    subprocess.run(["git", "config", "user.name", "CI Test"], cwd=checkout, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=checkout, check=True)

    fixture = checkout / "fixture.txt"
    fixture.write_text("old debt\n", encoding="utf-8")
    subprocess.run(["git", "add", "fixture.txt"], cwd=checkout, check=True)
    subprocess.run(["git", "commit", "-m", "older authority"], cwd=checkout, check=True, capture_output=True)
    older_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout, check=True, capture_output=True, text=True).stdout.strip()
    fixture.write_text("reduced debt\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "newer authority"], cwd=checkout, check=True, capture_output=True)
    newer_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout, check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "branch", "-M", "release/0.7.2"], cwd=checkout, check=True)
    subprocess.run(["git", "push", "-u", "origin", "release/0.7.2"], cwd=checkout, check=True, capture_output=True)
    subprocess.run(["git", "switch", "-c", "release/demo"], cwd=checkout, check=True, capture_output=True)
    fixture.write_text("attempted reintroduction\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "new branch push"], cwd=checkout, check=True, capture_output=True)
    current_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout, check=True, capture_output=True, text=True).stdout.strip()

    routes = {
        "/repos/owner/repo/actions/workflows/ci.yaml/runs?event=push&per_page=100": {
            "workflow_runs": [
                _workflow_run(100, older_sha, conclusion="success"),
                _workflow_run(200, newer_sha, conclusion="failure"),
            ]
        },
        "/repos/owner/repo/actions/runs/200/jobs?filter=all&per_page=100": {
            "jobs": [_ratchet_job(200, newer_sha, step_conclusion="success")]
        },
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            payload = routes.get(self.path)
            if payload is None:
                self.send_error(404)
                return
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        capture = tmp_path / "uv-args.txt"
        fake_uv = fake_bin / "uv"
        fake_uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$RATCHET_CAPTURE"\n', encoding="utf-8")
        fake_uv.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "CURRENT_RUN_ID": "999",
                "GH_TOKEN": "read-only-token",
                "GITHUB_API_URL": f"http://127.0.0.1:{server.server_port}",
                "GITHUB_REPOSITORY": "owner/repo",
                "GITHUB_RUN_ID": "999",
                "GITHUB_SHA": current_sha,
                "PATH": f"{fake_bin}:{env['PATH']}",
                "PR_BASELINE_REF": "",
                "RATCHET_CAPTURE": str(capture),
            }
        )

        completed = subprocess.run(
            [
                "bash",
                "-c",
                _step_run(
                    _ci_workflow()["jobs"]["static-analysis"],
                    "Reject touched or broadened permanent multi-rule per-file blankets",
                ),
            ],
            cwd=checkout,
            env=env,
            capture_output=True,
            text=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    assert completed.returncode == 0, completed.stderr
    captured_args = capture.read_text(encoding="utf-8").splitlines()
    assert captured_args[captured_args.index("--baseline-ref") + 1] == newer_sha


def test_judge_quality_never_receives_openrouter_credentials_on_prs() -> None:
    """The live-judge secret remains push-only; PRs skip the trusted job."""
    workflow = _workflow(JUDGE_GATES_WORKFLOW)
    job = workflow["jobs"]["check-judge-quality"]

    assert job["if"] == "github.event_name != 'pull_request'"
    assert job["env"]["OPENROUTER_API_KEY"] == "${{ secrets.OPENROUTER_API_KEY }}"


def test_trust_tier_ci_failure_points_to_signature_diagnosis_command() -> None:
    """Signed allowlist failures should point operators at the repair triage command."""
    workflow = _ci_workflow()
    static_analysis = workflow["jobs"]["static-analysis"]

    trust_tier_run = _step_run(static_analysis, "Run trust-tier elspeth-lints rule")
    sarif_run = _step_run(static_analysis, "Emit elspeth-lints trust-tier SARIF artifact")

    for run in (trust_tier_run, sarif_run):
        assert "diagnose-judge-signatures --root src/elspeth --allowlist-dir config/cicd/enforce_tier_model" in run
        assert "sign-judge-signatures --root src/elspeth --allowlist-dir config/cicd/enforce_tier_model" in run
        assert "--env-file /path/to/operator.env --owner" in run
        assert "judge_metadata_signature" in run
        assert "scope_fingerprint" in run
