"""Repository runner routing, separate from externally enforced runner access."""

from __future__ import annotations

import ast
import copy
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from tests.unit.cicd.workflow_sources import PREPARED_NAMES, PREPARED_WORKFLOWS, all_workflow_paths, workflow_paths

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
LOCAL = ["self-hosted", "Linux", "X64", "nyx-ci", "trusted"]
LIVE = ["self-hosted", "Linux", "X64", "ephemeral", "aws-ecs", "postgresql-16-rds", "state-engine-live-provider"]
HOSTED = ["ubuntu-24.04"]


def _workflows() -> dict[str, dict[str, Any]]:
    return {path.name: yaml.safe_load(path.read_text()) for path in all_workflow_paths()}


def _active_workflows() -> dict[str, dict[str, Any]]:
    return {path.name: yaml.safe_load(path.read_text()) for path in workflow_paths(WORKFLOWS)}


def _prepared_workflows() -> dict[str, dict[str, Any]]:
    return {path.name: yaml.safe_load(path.read_text()) for path in workflow_paths(PREPARED_WORKFLOWS)}


def _events(workflow: dict[str, Any]) -> dict[str, Any]:
    # YAML 1.1 parses the unquoted GitHub "on" key as True.
    events = workflow.get("on", workflow.get(True))
    assert isinstance(events, dict) and events, "workflow events must be resolved"
    return events


def _context(event: str, *, ref: str = "refs/heads/main", protected: bool = True, fork: bool = False) -> dict[str, Any]:
    return {
        "github": {
            "event_name": event,
            "repository": "dta-au/elspeth",
            "ref": ref,
            "ref_protected": protected,
            "event": {
                "repository": {"default_branch": "main"},
                "pull_request": {"head": {"repo": {"full_name": "fork/elspeth" if fork else "dta-au/elspeth"}}},
                "workflow_run": {
                    "conclusion": "success",
                    "event": "push",
                    "head_repository": {"full_name": "dta-au/elspeth"},
                    "head_branch": "main",
                },
            },
        },
        "needs": {"build-push": {"outputs": {"push_ghcr": "true", "push_acr": "false"}}},
    }


def _expression(expression: str, context: dict[str, Any]) -> Any:
    """Evaluate the reviewed GitHub expression subset; unknown syntax fails closed.

    This evaluates actual condition/selector operands rather than searching for
    label strings. It is a test instrument, not a production authorization gate.
    """
    expression = expression.strip().removeprefix("${{").removesuffix("}}").strip()
    expression = re.sub(
        r"'[^']*'|&&|\|\||!(?!=)",
        lambda match: {"&&": " and ", "||": " or ", "!": " not "}.get(match.group(), match.group()),
        expression,
    )
    expression = expression.replace("needs.build-push", "needs['build-push']")

    def visit(node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in {"false", "true"}:
                return node.id == "true"
            assert node.id in context, f"unresolved context: {node.id}"
            return context[node.id]
        if isinstance(node, ast.Attribute):
            return visit(node.value)[node.attr]
        if isinstance(node, ast.Subscript):
            return visit(node.value)[visit(node.slice)]
        if isinstance(node, ast.BoolOp):
            value = visit(node.values[0])
            for operand in node.values[1:]:
                if isinstance(node.op, ast.And):
                    if not value:
                        return value
                else:
                    assert isinstance(node.op, ast.Or)
                    if value:
                        return value
                value = visit(operand)
            return value
        if isinstance(node, ast.Compare):
            assert len(node.ops) == len(node.comparators) == 1
            left, right = visit(node.left), visit(node.comparators[0])
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            assert isinstance(node.ops[0], ast.NotEq)
            return left != right
        if isinstance(node, ast.UnaryOp):
            assert isinstance(node.op, ast.Not)
            return not visit(node.operand)
        if isinstance(node, ast.Call):
            assert isinstance(node.func, ast.Name) and not node.keywords
            arguments = [visit(argument) for argument in node.args]
            if node.func.id in {"always", "success"}:
                assert not arguments
                return True
            if node.func.id == "fromJSON":
                return json.loads(*arguments)
            if node.func.id == "startsWith":
                return arguments[0].startswith(arguments[1])
            assert node.func.id == "format", f"unresolved function: {node.func.id}"
            return arguments[0].format(*arguments[1:])
        raise AssertionError(f"unresolved syntax: {ast.dump(node)}")

    return visit(ast.parse(f"({expression})", mode="eval").body)


def _runner(job: dict[str, Any], context: dict[str, Any]) -> list[str]:
    assert "uses" not in job, "reusable workflow runner policy must be resolved before admission"
    requested = job["runs-on"]
    value = _expression(requested, context) if isinstance(requested, str) and requested.startswith("${{") else requested
    return [value] if isinstance(value, str) else value


def _admitted(jobs: dict[str, Any], name: str, context: dict[str, Any]) -> bool:
    job = jobs[name]
    condition = job.get("if", "success()")
    admitted = bool(_expression(condition, context))
    dependencies = job.get("needs", [])
    if isinstance(dependencies, str):
        dependencies = [dependencies]
    # The manual workflows have ordinary success-dependent jobs. An always()
    # change deliberately removes that propagation and must be detected.
    if "always()" not in condition:
        admitted = admitted and all(_admitted(jobs, dependency, context) for dependency in dependencies)
    return admitted


def _assert_routing(workflows: dict[str, dict[str, Any]]) -> None:
    assert workflows, "runner inventory is empty"
    for filename, workflow in workflows.items():
        events = _events(workflow)
        assert set(events) <= {"push", "pull_request", "schedule", "workflow_dispatch", "workflow_run", "workflow_call"}, (
            f"{filename}: unresolved trigger path"
        )
        if "workflow_call" in events:
            assert filename == "landscape-long-run.yml" and set(events) == {"workflow_call"}, "unreviewed reusable workflow"
        for event in events:
            context = _context(event)
            for name, job in workflow["jobs"].items():
                if "uses" in job:
                    assert filename == "ci.yaml" and name == "landscape-proof", "unreviewed reusable workflow"
                    assert job["uses"] == "./.github/workflows/landscape-long-run.yml", "unreviewed reusable workflow"
                    assert set(job) == {"name", "if", "uses"}, "unreviewed reusable workflow overrides"
                    target = workflows["landscape-long-run.yml"]
                    assert set(target["jobs"]) == {"proof"} and "uses" not in target["jobs"]["proof"]
                    assert _runner(target["jobs"]["proof"], context) == LOCAL, "reusable workflow runner isolation failed"
                    continue
                expected = LIVE if filename == "state-engine-live-provider.yml" else LOCAL
                assert _runner(job, context) == expected, f"{filename}:{name}:{event}: runner isolation failed"


def _assert_isolated_inventory(active: dict[str, dict[str, Any]], prepared: dict[str, dict[str, Any]]) -> None:
    assert active, "executable workflow inventory is empty"
    assert set(prepared) == PREPARED_NAMES, "prepared inventory differs from reviewed package"
    assert set(active).isdisjoint(prepared), "duplicate workflow source identity"
    required_contexts = {"CI Success", "CodeQL", "Check cohort-attribution trailers on PR commits", "redaction-gate"}
    for filename, workflow in active.items():
        assert not {"pull_request", "pull_request_target", "pull_request_review"} & set(_events(workflow)), (
            f"{filename}: PR producer discovered before enforcement"
        )
        assert workflow.get("name") not in required_contexts, f"{filename}: required producer discovered before enforcement"
        for name, job in workflow["jobs"].items():
            assert "uses" not in job, f"{filename}:{name}: reusable caller discovered before enforcement"
            assert job.get("name", name) not in required_contexts, f"{filename}:{name}: required producer discovered before enforcement"


def _assert_manual_admission(workflows: dict[str, dict[str, Any]]) -> None:
    for filename, workflow in workflows.items():
        if "workflow_dispatch" not in _events(workflow):
            continue
        jobs = workflow["jobs"]
        for name in jobs:
            expected = not (filename == "build-push.yaml" and name == "release")
            assert _admitted(jobs, name, _context("workflow_dispatch")) == expected, f"{filename}:{name}: trusted manual path changed"
            for context in (
                _context("workflow_dispatch", ref="refs/heads/contributor"),
                _context("workflow_dispatch", ref="refs/heads/release/0.8.4"),
                _context("workflow_dispatch", ref="refs/tags/v0.8.4"),
                _context("workflow_dispatch", protected=False),
            ):
                assert not _admitted(jobs, name, context), f"{filename}:{name}: untrusted manual ref reached runner"


def test_all_declared_event_paths_resolve_to_their_isolated_runner_pool() -> None:
    _assert_routing(_workflows())


def test_all_manual_paths_reject_nondefault_or_unprotected_refs_before_runner_allocation() -> None:
    _assert_manual_admission(_active_workflows())


def test_prepared_ci_and_every_other_pr_path_are_inactive() -> None:
    _assert_isolated_inventory(_active_workflows(), _prepared_workflows())


@pytest.mark.parametrize("filename", sorted(PREPARED_NAMES))
def test_every_prepared_source_refuses_accidental_discovery(filename: str) -> None:
    active, prepared = _active_workflows(), _prepared_workflows()
    _assert_isolated_inventory(active, prepared)
    active[filename] = prepared.pop(filename)
    with pytest.raises(AssertionError, match="prepared inventory"):
        _assert_isolated_inventory(active, prepared)


@pytest.mark.parametrize("mutation", ["renamed-pr", "caller", "required-success", "duplicate", "empty-active", "empty-prepared"])
def test_inactive_inventory_refuses_new_producers_duplicates_and_empty_scans(mutation: str) -> None:
    active, prepared = _active_workflows(), _prepared_workflows()
    _assert_isolated_inventory(active, prepared)
    if mutation == "renamed-pr":
        active["other.yml"] = {"on": {"pull_request": {}}, "jobs": {"test": {"if": "${{ false }}"}}}
    elif mutation == "caller":
        active["other.yml"] = {"on": {"workflow_dispatch": {}}, "jobs": {"proof": {"uses": "./.github/workflows/landscape-long-run.yml"}}}
    elif mutation == "required-success":
        active["other.yml"] = {"on": {"workflow_dispatch": {}}, "jobs": {"fake": {"name": "CI Success", "if": "${{ false }}"}}}
    elif mutation == "duplicate":
        active["ci.yaml"] = prepared["ci.yaml"]
    elif mutation == "empty-active":
        active.clear()
    else:
        prepared.clear()
    with pytest.raises(AssertionError, match=r"inventory|discovered|duplicate"):
        _assert_isolated_inventory(active, prepared)


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "extra", "empty-active"])
def test_source_path_union_refuses_hidden_duplicates_or_incomplete_inventories(tmp_path: Path, mutation: str) -> None:
    active, prepared = tmp_path / "active", tmp_path / "prepared"
    active.mkdir()
    prepared.mkdir()
    (active / "manual.yml").touch()
    for name in PREPARED_NAMES:
        (prepared / name).touch()
    assert set(all_workflow_paths(active, prepared)) == {active / "manual.yml", *(prepared / name for name in PREPARED_NAMES)}
    if mutation == "duplicate":
        (active / "ci.yaml").touch()
    elif mutation == "missing":
        (prepared / "ci.yaml").unlink()
    elif mutation == "extra":
        (prepared / "unreviewed.yml").touch()
    else:
        (active / "manual.yml").unlink()
    with pytest.raises(AssertionError, match=r"inventory|duplicate"):
        all_workflow_paths(active, prepared)


@pytest.mark.parametrize("mutation", ["checkout", "aggregate", "reversed", "new-trigger", "reusable"])
def test_runner_policy_detects_privileged_pr_routes_and_unresolved_reusable_paths(mutation: str) -> None:
    workflows = copy.deepcopy(_workflows())
    _assert_routing(workflows)
    if mutation == "new-trigger":
        workflows["pages.yaml"][True]["pull_request_target"] = {}
    elif mutation == "reusable":
        workflows["ci.yaml"]["jobs"]["delegated"] = {"uses": "./.github/workflows/pages.yaml"}
    else:
        name = "ci-success" if mutation == "aggregate" else "test"
        job = workflows["ci.yaml"]["jobs"][name]
        if mutation == "reversed":
            job["runs-on"] = (
                '${{ fromJSON(github.event_name != \'pull_request\' && \'["self-hosted","Linux","X64","nyx-ci","trusted"]\' || \'["ubuntu-24.04"]\') }}'
            )
        else:
            job["runs-on"] = HOSTED
    with pytest.raises(AssertionError, match=r"runner isolation|reusable workflow|unresolved trigger"):
        _assert_routing(workflows)


@pytest.mark.parametrize("filename", ["build-push.yaml", "pages.yaml", "mutation-testing.yaml", "state-engine-live-provider.yml"])
def test_manual_admission_detects_removed_guard_in_every_workflow(filename: str) -> None:
    workflows = copy.deepcopy(_workflows())
    _assert_manual_admission(workflows)
    jobs = workflows[filename]["jobs"]
    first = next(iter(jobs))
    jobs[first]["if"] = "always()"
    with pytest.raises(AssertionError, match="untrusted manual ref"):
        _assert_manual_admission(workflows)


def test_build_publication_workflow_run_retains_trusted_push_provenance() -> None:
    jobs = _workflows()["build-push.yaml"]["jobs"]
    context = _context("workflow_run")
    assert not _admitted(jobs, "build-push", context), "automatic publication stays inactive with CI"
    for field, value in (
        ("event", "pull_request"),
        ("conclusion", "failure"),
        ("head_repository", {"full_name": "fork/elspeth"}),
        ("head_branch", "contributor"),
    ):
        changed = copy.deepcopy(context)
        changed["github"]["event"]["workflow_run"][field] = value
        assert not _admitted(jobs, "build-push", changed)
    assert _admitted(jobs, "build-push", _context("push", ref="refs/tags/v0.8.4"))


def test_nyx_prerequisites_are_explicit_without_a_hosted_fallback() -> None:
    jobs = _workflows()["ci.yaml"]["jobs"]
    for name in ("testcontainer", "host-runner-unit", "state-engine-validation", "azure-container-apps-bicep", "e2e-frontend"):
        step = next(step for step in jobs[name]["steps"] if step.get("name") == "Install system dependencies")
        assert "github-hosted" not in step["run"]
        assert "dpkg-query -W libsqlcipher-dev" in step["run"]
    browser = next(step for step in jobs["e2e-frontend"]["steps"] if step.get("name") == "Install Playwright browser")
    assert browser["run"] == "npx playwright install chromium"
    package = json.loads((ROOT / "src/elspeth/web/frontend/package.json").read_text())
    assert package["scripts"]["test:e2e:install"] == "playwright install --with-deps chromium"


@pytest.mark.parametrize(
    "environment,name,probe_status,expected_status,port",
    [
        ("github-hosted", "Hosted Agent", 0, 1, None),
        ("self-hosted", "nyx-elspeth-1", 0, 0, 20002),
        ("self-hosted", "nyx-elspeth-4", 0, 0, 20008),
        ("self-hosted", "unexpected", 0, 1, None),
        ("self-hosted", "nyx-elspeth-22768", 0, 1, None),
        ("github-hosted", "Hosted Agent", 1, 1, None),
    ],
)
def test_playwright_port_allocation_uses_nyx_service_identity_and_refuses_probe_failure(
    tmp_path: Path, environment: str, name: str, probe_status: int, expected_status: int, port: int | None
) -> None:
    job = _workflows()["ci.yaml"]["jobs"]["e2e-frontend"]
    run = next(step for step in job["steps"] if step.get("name") == "Assign isolated Playwright ports")["run"]
    # Execute the actual shell branch, substituting only the socket-probe
    # subprocess so the test never reserves a shared host's fixed CI ports.
    probe = tmp_path / "python"
    probe.write_text('#!/bin/sh\nprintf "%s" "$2" > "$PROBE_PORT"\nexit "$PROBE_STATUS"\n')
    probe.chmod(0o755)
    output = tmp_path / "environment"
    probe_port = tmp_path / "probe-port"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "RUNNER_ENVIRONMENT": environment,
        "RUNNER_NAME": name,
        "GITHUB_ENV": str(output),
        "PROBE_STATUS": str(probe_status),
        "PROBE_PORT": str(probe_port),
    }
    completed = subprocess.run(["bash", "-euo", "pipefail", "-c", run], env=env, capture_output=True, text=True, check=False, timeout=5)
    assert completed.returncode == expected_status, completed.stdout + completed.stderr
    if port is None:
        assert not output.exists(), "failed allocation must not publish ports"
    else:
        assert probe_port.read_text() == str(port)
        assert output.read_text().splitlines() == [f"PLAYWRIGHT_BACKEND_PORT={port}", f"PLAYWRIGHT_FRONTEND_PORT={port + 1}"]
