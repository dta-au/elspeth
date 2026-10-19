"""CI workflow invariants for pytest parallel execution."""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from elspeth.testing.pytest_xdist_auto import pytest_cmdline_main
from tests.unit.cicd.workflow_sources import EXECUTABLE_WORKFLOWS, all_workflow_paths

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = EXECUTABLE_WORKFLOWS / "ci.yaml"
JUDGE_GATES_WORKFLOW = EXECUTABLE_WORKFLOWS / "enforce-allowlist-judge-gates.yaml"
CODEQL_WORKFLOW = EXECUTABLE_WORKFLOWS / "codeql.yaml"
CODEQL_CONFIG = REPO_ROOT / ".github" / "codeql" / "codeql-config.yml"
PROOF_WORKFLOW = EXECUTABLE_WORKFLOWS / "landscape-long-run.yml"
PROOF_WORKFLOW_CALL = "./.github/workflows/landscape-long-run.yml"
LONG_RUN_CASES = {
    "no-escape": "test_no_mutation_alias_wrapper_dynamic_or_raw_write_escape_exists",
    "reserve-actual-sources": "test_required_work_reserve_admission_on_actual_sources",
    "protected-formal-recovery": "test_required_work_reserve_rejects_protected_instance_formal_recovery",
}
PR_WORKFLOWS = (
    CI_WORKFLOW,
    CODEQL_WORKFLOW,
    EXECUTABLE_WORKFLOWS / "composer-redaction-gate.yml",
    JUDGE_GATES_WORKFLOW,
    EXECUTABLE_WORKFLOWS / "enforce-telemetry-backfill-trailer.yaml",
)
_SHELL_CONTROL_TOKENS = frozenset({"&&", "||", ";", "|"})


def _workflow(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), f"{path.name} workflow YAML root must be a mapping"
    return raw


def _ci_workflow() -> dict[str, Any]:
    return _workflow(CI_WORKFLOW)


def _executable_jobs(workflow: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Only the explicitly reviewed local proof call is a non-executable job.

    Every called executable job is still scanned through PROOF_WORKFLOW in the
    all-workflow inventories and explicitly in the fork-admission inventory.
    """
    result: list[tuple[str, dict[str, Any]]] = []
    for name, job in workflow["jobs"].items():
        if "uses" not in job:
            result.append((name, job))
            continue
        assert job["uses"] == PROOF_WORKFLOW_CALL, "unreviewed reusable workflow call"
        assert set(job) <= {"uses", "name", "needs", "if", "permissions"}, "reusable call has execution or secret overrides"
        assert job.get("permissions", {"contents": "read"}) == {"contents": "read"}, "reusable call changes token permissions"
        target = _workflow(PROOF_WORKFLOW)
        assert set(target.get("on", target.get(True))) == {"workflow_call"}, "proof target must be workflow_call-only"
        assert set(target["jobs"]) == {"proof"} and "uses" not in target["jobs"]["proof"], "proof target must execute its job"
    return result


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


def test_integration_lane_caps_xdist_workers_explicitly() -> None:
    """An explicit cap prevents integration from inheriting the project worker default."""
    workflow = _ci_workflow()
    integration_job = workflow["jobs"]["integration"]

    run = _step_run(integration_job, "Run integration tests")

    assert _pytest_numprocesses_values(run) == ["2"]


def test_judge_gates_workflow_mirrors_ci_concurrency_policy() -> None:
    """Policy-gate workflow must not race push and PR runs for one ref."""
    ci_workflow = _ci_workflow()
    judge_workflow = _workflow(JUDGE_GATES_WORKFLOW)

    assert judge_workflow["concurrency"] == ci_workflow["concurrency"]


def test_judge_gates_context_is_documented_as_advisory() -> None:
    """The judge summary remains an additional signal outside required checks."""
    workflow_text = JUDGE_GATES_WORKFLOW.read_text(encoding="utf-8")
    judge_workflow = _workflow(JUDGE_GATES_WORKFLOW)
    aggregate_job = judge_workflow["jobs"]["judge-gates-success"]

    assert aggregate_job["name"] == "Judge gates success"
    assert "This workflow is advisory by design" in workflow_text
    assert "outside the standard enforcement" in workflow_text
    assert "absence from branch protection is not a" in workflow_text
    assert "Branch protection MUST require" not in workflow_text


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


def test_codeql_analysis_relativizes_results_to_the_isolated_checkout() -> None:
    """Uploaded findings must use repository paths rather than the run directory."""
    job = _workflow(CODEQL_WORKFLOW)["jobs"]["analyze"]
    checkout = _step(job, "Checkout code")["with"]["path"]
    init = _step(job, "Initialize CodeQL")["with"]
    analyze = _step(job, "Perform CodeQL analysis")["with"]

    assert init["source-root"] == checkout
    assert analyze["checkout_path"] == f"${{{{ github.workspace }}}}/{checkout}"


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

    assert condition == (
        "(github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository) && "
        "(github.event_name == 'push' && (github.ref == 'refs/heads/main' || github.ref == 'refs/heads/master' || "
        "startsWith(github.ref, 'refs/heads/RC') || startsWith(github.ref, 'refs/heads/release/')))"
    )


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


def test_every_workflow_job_uses_the_declared_self_hosted_pool() -> None:
    """Hosted runners are forbidden; live topology proofs retain their cloud pool."""
    workflow_paths = all_workflow_paths()
    assert CI_WORKFLOW in workflow_paths
    for workflow_path in workflow_paths:
        if workflow_path.suffix not in {".yml", ".yaml"}:
            continue
        workflow = _workflow(workflow_path)
        for job_name, job in _executable_jobs(workflow):
            expected = ["self-hosted", "Linux", "X64", "nyx-ci", "trusted"]
            if workflow_path.name == "state-engine-live-provider.yml":
                expected = ["self-hosted", "Linux", "X64", "ephemeral", "aws-ecs", "postgresql-16-rds", "state-engine-live-provider"]
            assert job["runs-on"] == expected, f"{workflow_path.name}:{job_name} must route to its declared self-hosted pool"


def test_fork_pull_requests_cannot_reach_checkout_jobs() -> None:
    """Active PR sources retain fork guards, independent of external admission."""
    for workflow_path in (*PR_WORKFLOWS, PROOF_WORKFLOW):
        assert workflow_path.parent == EXECUTABLE_WORKFLOWS
        assert workflow_path.is_file()
        workflow = _workflow(workflow_path)
        for job_name, job in _executable_jobs(workflow):
            if not any(str(step.get("uses", "")).startswith("actions/checkout@") for step in job["steps"]):
                continue
            condition = str(job.get("if", ""))
            assert (
                "github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository" in condition
            ), f"{workflow_path.name}:{job_name} lacks fork guard in active source"


@pytest.mark.parametrize("workflow_path,job_name", [(CI_WORKFLOW, "ci-success"), (JUDGE_GATES_WORKFLOW, "judge-gates-success")])
@pytest.mark.parametrize("is_fork,expected_status", [("true", 1), ("false", 0)])
def test_fork_pull_requests_fail_aggregate_statuses_without_checkout(
    workflow_path: Path, job_name: str, is_fork: str, expected_status: int
) -> None:
    """Skipped fork jobs cannot make either aggregate status green."""
    job = _workflow(workflow_path)["jobs"][job_name]
    assert job["if"] == "always()"
    assert not any(str(step.get("uses", "")).startswith("actions/checkout@") for step in job["steps"])
    gate = job["steps"][0]
    assert gate["env"]["IS_FORK_PR"] == (
        "${{ github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name != github.repository }}"
    )
    environment = os.environ.copy()
    environment["IS_FORK_PR"] = is_fork
    result = subprocess.run(["bash", "-e", "-c", gate["run"]], env=environment, capture_output=True, text=True, check=False, timeout=5)
    assert result.returncode == expected_status, result.stdout + result.stderr


def test_local_checkouts_are_isolated_between_jobs_and_attempts() -> None:
    """Host jobs cannot inherit root-owned container environments from another run."""
    paths: set[str] = set()
    for workflow_path in all_workflow_paths():
        if workflow_path.suffix not in {".yml", ".yaml"}:
            continue
        if workflow_path.name == "state-engine-live-provider.yml":
            continue  # This workflow retains disposable cloud runners for topology proofs.
        workflow = _workflow(workflow_path)
        for job_name, job in _executable_jobs(workflow):
            for step in job["steps"]:
                if not str(step.get("uses", "")).startswith("actions/checkout@"):
                    continue
                checkout_path = step["with"]["path"]
                assert checkout_path == "${{ env.CI_CHECKOUT_PATH }}", f"{workflow_path.name}:{job_name} has a shared checkout"
                path = job["env"]["CI_CHECKOUT_PATH"]
                assert "github.run_id" in path and "github.run_attempt" in path
                assert path not in paths, f"{workflow_path.name}:{job_name} shares a checkout with another job"
                paths.add(path)
                if "matrix" in job.get("strategy", {}):
                    assert "matrix." in path or "strategy.job-index" in path, f"{workflow_path.name}:{job_name} shares matrix checkouts"


def test_no_workflow_references_the_operator_hmac_key() -> None:
    """A key injected at job/workflow scope is as reachable as a step secret."""
    workflow_paths = all_workflow_paths()
    assert CI_WORKFLOW in workflow_paths
    for workflow_path in workflow_paths:
        if workflow_path.suffix in {".yml", ".yaml"}:
            assert "ELSPETH_JUDGE_METADATA_HMAC_KEY" not in json.dumps(_workflow(workflow_path)), workflow_path.name


def test_automatic_judge_workflow_has_no_paid_quality_job_or_credentials() -> None:
    """Both push and PR paths remain deterministic and credential-free."""
    workflow = _workflow(JUDGE_GATES_WORKFLOW)

    assert set(workflow["jobs"]) == {"check-override-rate", "judge-gates-success"}
    assert "OPENROUTER_API_KEY" not in json.dumps(workflow)
    assert "check-judge-quality" not in json.dumps(workflow)
    assert workflow["jobs"]["judge-gates-success"]["needs"] == ["check-override-rate"]


def _assert_no_automatic_judge_quality(workflow: dict[str, Any]) -> None:
    # PyYAML's YAML 1.1 loader reads the unquoted GitHub "on" key as True.
    events = workflow.get("on", workflow.get(True))
    assert isinstance(events, (str, list, dict)), "workflow must declare its triggers"
    event_names = {events} if isinstance(events, str) else set(events)
    assert event_names, "an empty trigger set cannot prove manual-only execution"
    if event_names != {"workflow_dispatch"}:
        assert "check-judge-quality" not in json.dumps(workflow), "automatic workflow can invoke paid judge-quality validation"


def test_all_automatic_workflows_exclude_paid_judge_quality() -> None:
    """Direct paid-quality commands cannot move to another automatic workflow.

    This configuration guard does not follow wrapper scripts or reusable actions;
    caller inspection remains part of workflow review.
    """
    paths = all_workflow_paths()
    assert CI_WORKFLOW in paths
    assert JUDGE_GATES_WORKFLOW in paths
    for path in paths:
        _assert_no_automatic_judge_quality(_workflow(path))


@pytest.mark.parametrize("event", ["push", "pull_request", "schedule", "workflow_run", "workflow_call"])
def test_automatic_paid_judge_guard_rejects_live_calls(event: str) -> None:
    """A paid invocation must be detected for every automatic entry point."""
    workflow = {"on": {event: {}}, "jobs": {"quality": {"steps": [{"run": "elspeth-lints check-judge-quality --corpus cases.jsonl"}]}}}

    with pytest.raises(AssertionError, match="automatic workflow"):
        _assert_no_automatic_judge_quality(workflow)


def test_paid_judge_guard_allows_only_exclusively_manual_validation() -> None:
    """An explicit manual path is allowed; adding any automatic trigger is caught."""
    workflow = yaml.safe_load(
        "on:\n  workflow_dispatch:\njobs:\n  quality:\n    steps:\n"
        "      - run: python -m elspeth_lints.core.cli check-judge-quality --corpus cases.jsonl\n"
    )
    _assert_no_automatic_judge_quality(workflow)
    workflow[True]["push"] = {}
    with pytest.raises(AssertionError, match="automatic workflow"):
        _assert_no_automatic_judge_quality(workflow)


@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped", "timed_out"])
def test_judge_aggregate_still_requires_successful_override_rate(result: str) -> None:
    """Removing live judging cannot turn a failed deterministic check green."""
    job = _workflow(JUDGE_GATES_WORKFLOW)["jobs"]["judge-gates-success"]
    run = _step_run(job, "Check all judge gates passed")
    run = run.replace("${{ needs.check-override-rate.result }}", result)

    completed = subprocess.run(["bash", "-e", "-c", run], capture_output=True, text=True, check=False, timeout=5)

    assert completed.returncode == (0 if result == "success" else 1), completed.stdout + completed.stderr


@pytest.mark.parametrize("job_name", ["test", "integration"])
def test_pytest_container_jobs_raise_the_open_file_limit(job_name: str) -> None:
    """Docker 29 starts the runners' containers with a soft nofile of 1024, which
    one xdist worker exhausted mid-suite (run 36526535297: `Too many open
    files`, then 108 failed + 1661 errors on that worker alone)."""
    job = _ci_workflow()["jobs"][job_name]

    assert "--ulimit nofile=65536:524288" in job["container"]["options"]


def test_test_job_reports_through_junit_not_verbose_log() -> None:
    """The retrievable job log truncates around 71k lines, so the verbose
    per-test listing hid the failure summary; the JUnit report is uploaded
    whatever the outcome."""
    test_job = _ci_workflow()["jobs"]["test"]

    for step_name in ("Run tests with coverage", "Run tests without coverage"):
        args = _pytest_args(_step_run(test_job, step_name))
        assert "-v" not in args
        assert "--junitxml=pytest-junit.xml" in args

    upload = _step(test_job, "Upload JUnit report")
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "${{ env.CI_CHECKOUT_PATH }}/pytest-junit.xml"


def test_reviewed_local_proof_call_preserves_executable_policy_inventory() -> None:
    executable = {"runs-on": ["self-hosted"], "steps": [{"run": "false"}]}
    assert _executable_jobs({"jobs": {"call": {"uses": PROOF_WORKFLOW_CALL}, "native": executable}}) == [("native", executable)]
    assert _executable_jobs(_workflow(PROOF_WORKFLOW)) == [("proof", _workflow(PROOF_WORKFLOW)["jobs"]["proof"])]


@pytest.mark.parametrize(
    "override",
    [
        {"uses": "untrusted/repo/.github/workflows/proof.yml@main"},
        {"uses": "./.github/workflows/other.yml"},
        {"uses": "./.github/workflows/landscape-no-escape.yml"},
        {"uses": "./.github/workflows/../workflows/landscape-no-escape.yml"},
        {"steps": [{"run": "false"}]},
        {"runs-on": "ubuntu-latest"},
        {"env": {"BYPASS": "1"}},
        {"secrets": "inherit"},
        {"with": {"command": "true"}},
        {"continue-on-error": True},
        {"permissions": {"contents": "write"}},
    ],
)
def test_reusable_call_cannot_bypass_executable_policy(override: dict[str, Any]) -> None:
    job = {"uses": PROOF_WORKFLOW_CALL, **override}
    with pytest.raises(AssertionError):
        _executable_jobs({"jobs": {"call": job}})


def _assert_long_run_workflow_contract(workflow: dict[str, Any]) -> None:
    assert set(workflow.get("on", workflow.get(True))) == {"workflow_call"}
    assert "concurrency" not in workflow and "defaults" not in workflow
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["env"] == {"UV_VERSION": "0.10.2"}
    assert set(workflow["jobs"]) == {"proof"}
    job = workflow["jobs"]["proof"]
    assert "uses" not in job and "defaults" not in job
    assert job.get("continue-on-error", False) is False
    assert job["if"] == "github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository"
    assert job["name"] == "Landscape ${{ matrix.proof-case }} (${{ matrix.python-version }})"
    assert job["timeout-minutes"] == "${{ matrix.job-timeout-minutes }}"
    assert job["strategy"]["fail-fast"] is False
    assert job["strategy"]["max-parallel"] == 2
    assert set(job["strategy"]["matrix"]) == {"include"}
    rows = job["strategy"]["matrix"]["include"]
    assert len(rows) == 6
    assert {(row["proof-case"], row["python-version"]) for row in rows} == {
        (case, version) for case in LONG_RUN_CASES for version in ("3.12", "3.13")
    }
    images = {
        "3.12": "python:3.12-bookworm@sha256:dbbe4ceb97851e2e5fa83798b239811f871cb743b259ba3563737349f6bcfaa0",
        "3.13": "python:3.13-bookworm@sha256:227b6570d6ee07061ae6ca2eb04dedfb6d2b34045835f343065b9869e4d427ea",
    }
    for row in rows:
        assert set(row) == {
            "proof-case",
            "proof-nodeid",
            "python-version",
            "coverage",
            "execution-timeout-seconds",
            "job-timeout-minutes",
            "ci-container-image",
        }
        version = row["python-version"]
        assert row["proof-nodeid"] == "tests/unit/architecture/test_web_landscape_mutation_fencing.py::" + LONG_RUN_CASES[row["proof-case"]]
        assert row["coverage"] is (version == "3.13")
        assert row["execution-timeout-seconds"] == (14400 if version == "3.13" else 7200)
        assert row["job-timeout-minutes"] == (270 if version == "3.13" else 150)
        assert row["ci-container-image"] == images[version]
    assert job["container"] == {"image": "${{ matrix.ci-container-image }}", "options": "--ulimit nofile=65536:524288"}
    assert job["env"] == {
        "CI_CHECKOUT_PATH": "ci-${{ github.run_id }}-${{ github.run_attempt }}-landscape-proof-${{ matrix.proof-case }}-${{ matrix.python-version }}",
        "UV_PYTHON": "${{ matrix.python-version }}",
        "COVERAGE_FILE": "coverage-data",
        "PROOF_CASE": "${{ matrix.proof-case }}",
        "PROOF_NODEID": "${{ matrix.proof-nodeid }}",
        "PROOF_TIMEOUT_SECONDS": "${{ matrix.execution-timeout-seconds }}",
    }
    assert _step(job, "Checkout code")["with"] == {"path": "${{ env.CI_CHECKOUT_PATH }}", "ref": "${{ github.sha }}", "fetch-depth": 0}
    assert _step(job, "Install dependencies")["run"] == "uv sync --frozen --all-extras"
    for covered, name in ((True, "Run long-run proof with coverage"), (False, "Run long-run proof without coverage")):
        step = _step(job, name)
        assert set(step) == {"name", "if", "working-directory", "run"}
        assert step["if"] == ("matrix.coverage" if covered else "${{ !matrix.coverage }}")
        assert step["working-directory"] == "${{ env.CI_CHECKOUT_PATH }}"
        expected = [
            "uv",
            "run",
            "python",
            "scripts/ci_proof_progress.py",
            "--nodeid",
            "$PROOF_NODEID",
            "--timeout-seconds",
            "$PROOF_TIMEOUT_SECONDS",
            "--heartbeat-seconds",
            "30",
            "--grace-seconds",
            "10",
            "--report",
            "proof-progress.json",
            "--",
            "uv",
            "run",
            "python",
            "-m",
            "pytest",
            "tests/",
            "-p",
            "scripts.ci_test_shards",
            "--ci-partition=proof",
            "--ci-proof-case=$PROOF_CASE",
            "--ci-shard-count=1",
            "--ci-shard-index=0",
            "--ci-shard-report=shard-manifest.json",
        ]
        if covered:
            expected.extend(["--cov=src/elspeth", "--cov-report=", "--cov-fail-under=0"])
        expected.extend(["-n", "0", "-q", "-rfE", "--junitxml=pytest-junit.xml"])
        # Compare the whole executable block, not only pytest args before a shell
        # operator: prefixes and later newline commands can also mask failures.
        assert shlex.split(step["run"].replace("\\\n", " ")) == expected
        assert not any(token in step["run"] for token in ("`", "$(", "||", "&&", ";", "|"))
    upload = _step(job, "Upload proof selection, coverage and execution evidence")
    assert upload["if"] == "always()"
    assert upload.get("continue-on-error", False) is False
    assert upload["with"] == {
        "name": "landscape-proof-${{ matrix.proof-case }}-${{ matrix.python-version }}-attempt-${{ github.run_attempt }}",
        "path": "".join(
            "${{ env.CI_CHECKOUT_PATH }}/" + name + "\n"
            for name in (
                "shard-manifest.json",
                "coverage-data",
                "pytest-junit.xml",
                "proof-progress.json",
            )
        ),
        "if-no-files-found": "error",
        "retention-days": 7,
    }


def test_three_case_workflow_is_called_complete_and_serial() -> None:
    _assert_long_run_workflow_contract(_workflow(PROOF_WORKFLOW))
    calls = [
        (path.name, name, job["uses"]) for path in all_workflow_paths() for name, job in _workflow(path)["jobs"].items() if "uses" in job
    ]
    assert calls == [("ci.yaml", "landscape-proof", PROOF_WORKFLOW_CALL)]
    _executable_jobs(_ci_workflow())
    assert not (REPO_ROOT / ".github/workflows/landscape-no-escape.yml").exists()


@pytest.mark.parametrize("row_index", range(6))
@pytest.mark.parametrize(
    "field,value",
    [
        ("proof-case", "unknown"),
        ("proof-nodeid", "tests/wrong.py::test_wrong"),
        ("python-version", "3.14"),
        ("coverage", "true"),
        ("execution-timeout-seconds", 1),
        ("job-timeout-minutes", 1),
        ("ci-container-image", "python:latest"),
    ],
)
def test_three_case_contract_rejects_every_misbound_row(row_index: int, field: str, value: object) -> None:
    workflow = copy.deepcopy(_workflow(PROOF_WORKFLOW))
    workflow["jobs"]["proof"]["strategy"]["matrix"]["include"][row_index][field] = value
    with pytest.raises(AssertionError):
        _assert_long_run_workflow_contract(workflow)


@pytest.mark.parametrize(
    "mutation",
    [
        "omit",
        "duplicate",
        "trigger",
        "permissions",
        "concurrency",
        "fork",
        "continue",
        "fail-fast",
        "capacity",
        "checkout",
        "artifact",
        "node-env",
        "native-identity",
        "upload-condition",
    ],
)
def test_three_case_contract_rejects_identity_and_admission_bypasses(mutation: str) -> None:
    workflow = copy.deepcopy(_workflow(PROOF_WORKFLOW))
    job = workflow["jobs"]["proof"]
    rows = job["strategy"]["matrix"]["include"]
    if mutation == "omit":
        rows.pop()
    elif mutation == "duplicate":
        rows[-1] = rows[0]
    elif mutation == "trigger":
        workflow[True]["push"] = {}
    elif mutation == "permissions":
        workflow["permissions"] = {"contents": "write"}
    elif mutation == "concurrency":
        workflow["concurrency"] = {"group": "${{ github.workflow }}-${{ github.ref }}"}
    elif mutation == "fork":
        job["if"] = "always()"
    elif mutation == "continue":
        job["continue-on-error"] = True
    elif mutation == "fail-fast":
        job["strategy"]["fail-fast"] = True
    elif mutation == "capacity":
        job["strategy"]["max-parallel"] = 6
    elif mutation == "checkout":
        job["env"]["CI_CHECKOUT_PATH"] = "ci-${{ github.run_id }}-${{ github.run_attempt }}-${{ matrix.python-version }}"
    elif mutation == "artifact":
        _step(job, "Upload proof selection, coverage and execution evidence")["with"]["name"] = "latest-success"
    elif mutation == "node-env":
        job["env"]["PROOF_NODEID"] = "$PROOF_CASE"
    elif mutation == "native-identity":
        job["env"]["GITHUB_SHA"] = "old-success"
    elif mutation == "upload-condition":
        _step(job, "Upload proof selection, coverage and execution evidence")["if"] = "success()"
    with pytest.raises(AssertionError):
        _assert_long_run_workflow_contract(workflow)


@pytest.mark.parametrize("step_name", ["Run long-run proof with coverage", "Run long-run proof without coverage"])
@pytest.mark.parametrize(
    "mutation", ["or-true", "newline-exit", "newline-true", "prefix-true", "marker", "partition", "case", "parallel", "step-continue"]
)
def test_three_case_commands_cannot_mask_supervisor_failure(step_name: str, mutation: str) -> None:
    workflow = copy.deepcopy(_workflow(PROOF_WORKFLOW))
    step = _step(workflow["jobs"]["proof"], step_name)
    if mutation == "or-true":
        step["run"] += " || true"
    elif mutation == "newline-exit":
        step["run"] += "\nexit 0\n"
    elif mutation == "newline-true":
        step["run"] += "\ntrue\n"
    elif mutation == "prefix-true":
        step["run"] = "true " + step["run"]
    elif mutation == "marker":
        step["run"] += " -m landscape_long_run"
    elif mutation == "partition":
        step["run"] = step["run"].replace("--ci-partition=proof", "--ci-partition=all")
    elif mutation == "case":
        step["run"] = step["run"].replace('--ci-proof-case="$PROOF_CASE"', "--ci-proof-case=no-escape")
    elif mutation == "parallel":
        step["run"] = step["run"].replace("-n 0", "-n 2")
    elif mutation == "step-continue":
        step["continue-on-error"] = True
    with pytest.raises(AssertionError):
        _assert_long_run_workflow_contract(workflow)


@pytest.mark.parametrize(
    "job_name,step_name",
    [
        ("coverage", "Require every test shard and exhaustive proof to pass"),
        ("ci-success", "Check all jobs passed"),
    ],
)
@pytest.mark.parametrize("proof_result", ["success", "failure", "cancelled", "skipped", "timed_out", ""])
def test_active_native_proof_result_remains_required(job_name: str, step_name: str, proof_result: str) -> None:
    job = _ci_workflow()["jobs"][job_name]
    assert "landscape-proof" in job["needs"]
    run = _step_run(job, step_name)
    assert "${{ needs.landscape-proof.result }}" in run
    run = re.sub(r"\$\{\{ needs\.([\w-]+)\.result }}", lambda match: proof_result if match[1] == "landscape-proof" else "success", run)
    result = subprocess.run(["bash", "-e", "-c", run], capture_output=True, text=True, check=False, timeout=5)
    assert result.returncode == (0 if proof_result == "success" else 1), result.stdout + result.stderr
