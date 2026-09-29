"""CI workflow invariants for pytest parallel execution."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from elspeth.testing.pytest_xdist_auto import pytest_cmdline_main

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yaml"
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


def test_no_workflow_references_the_operator_hmac_key() -> None:
    """A key injected at job/workflow scope is as reachable as a step secret."""
    workflow_paths = sorted((REPO_ROOT / ".github" / "workflows").iterdir())
    assert CI_WORKFLOW in workflow_paths
    for workflow_path in workflow_paths:
        if workflow_path.suffix in {".yml", ".yaml"}:
            assert "ELSPETH_JUDGE_METADATA_HMAC_KEY" not in json.dumps(_workflow(workflow_path)), workflow_path.name


def test_judge_quality_never_receives_openrouter_credentials_on_prs() -> None:
    """The live-judge secret remains push-only; PRs skip the trusted job."""
    workflow = _workflow(JUDGE_GATES_WORKFLOW)
    job = workflow["jobs"]["check-judge-quality"]

    assert job["if"] == "github.event_name != 'pull_request'"
    assert job["env"]["OPENROUTER_API_KEY"] == "${{ secrets.OPENROUTER_API_KEY }}"


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
