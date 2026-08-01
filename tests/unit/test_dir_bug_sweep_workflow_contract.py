"""Contract tests for the local dir-bug-sweep workflow and companion skill."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".claude" / "workflows" / "dir-bug-sweep.js"
SKILL = REPO_ROOT / ".claude" / "skills" / "bug-sweep" / "SKILL.md"

_NODE_HARNESS = r"""
const fs = require('fs')
const input = JSON.parse(fs.readFileSync(0, 'utf8'))
const source = fs
  .readFileSync(input.workflow, 'utf8')
  .replace(/^export const meta =/m, 'const meta =')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const runWorkflow = new AsyncFunction('args', 'agent', 'parallel', 'phase', 'log', source)

const prompts = []
const logs = []
const phases = []
let agentCalls = 0
let parallelCalls = 0

const agent = async (prompt, options) => {
  prompts.push({ prompt, options })
  agentCalls += 1
  if (input.failOnAgent) throw new Error('agent must not be called')
  return input.agentResults[agentCalls - 1] ?? null
}

const parallel = async factories => {
  parallelCalls += 1
  if (input.failOnParallel) throw new Error('parallel must not be called')
  return Promise.all(factories.map(factory => factory()))
}

;(async () => {
  try {
    const result = await runWorkflow(
      input.args,
      agent,
      parallel,
      title => phases.push(title),
      message => logs.push(message),
    )
    process.stdout.write(JSON.stringify({
      ok: true,
      result,
      prompts,
      logs,
      phases,
      agentCalls,
      parallelCalls,
    }))
  } catch (error) {
    process.stdout.write(JSON.stringify({
      ok: false,
      error: String(error && error.message ? error.message : error),
      prompts,
      logs,
      phases,
      agentCalls,
      parallelCalls,
    }))
  }
})()
"""


def _run_workflow(
    arguments: dict[str, Any],
    *,
    agent_results: list[dict[str, Any] | None] | None = None,
    fail_on_agent: bool = False,
    fail_on_parallel: bool = False,
) -> dict[str, Any]:
    completed = subprocess.run(
        ["node", "-e", _NODE_HARNESS],
        input=json.dumps(
            {
                "workflow": str(WORKFLOW),
                "args": arguments,
                "agentResults": agent_results or [],
                "failOnAgent": fail_on_agent,
                "failOnParallel": fail_on_parallel,
            }
        ),
        text=True,
        capture_output=True,
        check=True,
        cwd=REPO_ROOT,
    )
    return json.loads(completed.stdout)


def _assert_workflow_succeeded(run: dict[str, Any]) -> None:
    assert run["ok"], run.get("error")


def _scout_command(prompt: str) -> str:
    return next(line.strip() for line in prompt.splitlines() if line.strip().startswith("find "))


def test_scout_command_treats_path_and_glob_as_single_shell_arguments() -> None:
    path = "scope with spaces; __CODEX_SENTINEL__ 'quoted'"
    glob = "*.py'; __CODEX_SECOND_SENTINEL__ #"

    run = _run_workflow({"path": path, "glob": glob}, agent_results=[{"files": []}])

    _assert_workflow_succeeded(run)
    command = _scout_command(run["prompts"][0]["prompt"])
    assert command == (f"find {shlex.quote(path)} -type f -name {shlex.quote(glob)} -not -path '*/__pycache__/*' -print0 | xargs -0 wc -l")


@pytest.mark.parametrize("path", [".", "./"])
def test_empty_explicit_whole_repo_inventory_never_launches_a_scout(path: str) -> None:
    run = _run_workflow({"path": path, "files": []}, fail_on_agent=True)

    _assert_workflow_succeeded(run)
    assert run["result"]["error"] == "no_files_found"
    assert run["agentCalls"] == 0


def test_empty_explicit_scoped_inventory_never_launches_a_scout() -> None:
    run = _run_workflow({"path": "src/elspeth/contracts", "files": []}, fail_on_agent=True)

    _assert_workflow_succeeded(run)
    assert run["result"]["error"] == "no_files_found"
    assert run["agentCalls"] == 0


@pytest.mark.parametrize("path", [".", "./", "src/..", "./src/..", "src/nested/../.."])
def test_whole_repo_alias_without_inventory_is_refused(path: str) -> None:
    run = _run_workflow({"path": path}, fail_on_agent=True)

    _assert_workflow_succeeded(run)
    assert run["result"]["error"] == "unscoped_sweep_refused"
    assert run["agentCalls"] == 0


@pytest.mark.parametrize("path", ["..", "../src", "src/../..", "src/../../outside"])
def test_parent_escape_without_inventory_is_refused(path: str) -> None:
    run = _run_workflow({"path": path}, fail_on_agent=True)

    _assert_workflow_succeeded(run)
    assert run["result"]["error"] == "out_of_repo_sweep_refused"
    assert run["agentCalls"] == 0


@pytest.mark.parametrize(
    "path",
    [
        str(REPO_ROOT),
        str(REPO_ROOT / "src" / ".."),
        str(REPO_ROOT / "src"),
    ],
)
def test_absolute_path_without_inventory_is_refused(path: str) -> None:
    run = _run_workflow({"path": path}, fail_on_agent=True)

    _assert_workflow_succeeded(run)
    assert run["result"]["error"] == "absolute_scout_path_refused"
    assert run["agentCalls"] == 0


def test_absolute_path_with_explicit_inventory_preserves_targeted_run_semantics() -> None:
    run = _run_workflow({"path": str(REPO_ROOT), "files": []}, fail_on_agent=True)

    _assert_workflow_succeeded(run)
    assert run["result"]["error"] == "no_files_found"
    assert run["agentCalls"] == 0


@pytest.mark.parametrize("path", ["scope..", "..scope", "src/..scope"])
def test_scout_allows_legitimate_path_names_containing_dot_dot(path: str) -> None:
    run = _run_workflow({"path": path}, agent_results=[{"files": []}])

    _assert_workflow_succeeded(run)
    assert run["result"]["error"] == "no_files_found"
    assert run["agentCalls"] == 1


@pytest.mark.parametrize("path", ["-H", "-delete", "-name"])
def test_scout_command_prevents_leading_dash_scope_from_becoming_find_syntax(path: str) -> None:
    run = _run_workflow({"path": path}, agent_results=[{"files": []}])

    _assert_workflow_succeeded(run)
    command = _scout_command(run["prompts"][0]["prompt"])
    assert shlex.split(command)[:4] == ["find", f"./{path}", "-type", "f"]


def test_scoped_inventory_omission_preserves_scout_fallback() -> None:
    run = _run_workflow(
        {"path": "src/elspeth/contracts", "glob": "*.py"},
        agent_results=[{"files": []}],
    )

    _assert_workflow_succeeded(run)
    assert run["result"]["error"] == "no_files_found"
    assert run["agentCalls"] == 1
    assert run["prompts"][0]["options"]["label"] == "scout"


@pytest.mark.parametrize("max_parallel", [-1, 0, 0.5, "2"])
def test_max_parallel_must_be_a_positive_integer(max_parallel: object) -> None:
    run = _run_workflow(
        {"path": "src", "files": [{"path": "src/one.py", "lines": 1}], "maxParallel": max_parallel},
        fail_on_agent=True,
        fail_on_parallel=True,
    )

    _assert_workflow_succeeded(run)
    assert run["result"]["error"] == "invalid_max_parallel"
    assert run["agentCalls"] == 0
    assert run["parallelCalls"] == 0


def test_review_prompt_creates_issue_with_sweep_label_atomically() -> None:
    tag = "contract-sweep"
    run = _run_workflow(
        {"path": "src", "tag": tag, "files": [{"path": "src/one.py", "lines": 1}]},
        agent_results=[{"files_reviewed": ["src/one.py"], "issues": [], "notes": "clean"}],
    )

    _assert_workflow_succeeded(run)
    prompt = run["prompts"][0]["prompt"]
    assert 'labels: ["contract-sweep"]' in prompt
    assert "mcp__filigree__label_add" not in prompt


def test_reviewed_files_are_bound_to_the_reporting_agents_assigned_bin() -> None:
    run = _run_workflow(
        {
            "path": "src",
            "lineCap": 1,
            "maxParallel": 2,
            "files": [
                {"path": "src/a.py", "lines": 1},
                {"path": "src/b.py", "lines": 1},
            ],
        },
        agent_results=[
            {
                "files_reviewed": ["src/a.py", "src/b.py", "outside.py"],
                "issues": [],
                "notes": "claimed files outside this bin",
            },
            None,
        ],
    )

    _assert_workflow_succeeded(run)
    assert run["result"]["binsReturned"] == 1
    assert run["result"]["filesReviewed"] == 1
    assert run["result"]["unreviewedFiles"] == ["src/b.py"]
    assert run["result"]["perAgent"][0]["files"] == ["src/a.py"]


def test_every_dir_bug_sweep_invocation_example_uses_script_path() -> None:
    sources = (WORKFLOW.read_text(encoding="utf-8"), SKILL.read_text(encoding="utf-8"))
    invocation_lines = [line for source in sources for line in source.splitlines() if "Workflow({" in line]

    assert invocation_lines
    assert all("scriptPath:" in line for line in invocation_lines), invocation_lines
    assert "re-invoke by name" not in sources[1]


def test_empty_precomputed_inventory_emits_empty_json(tmp_path: Path) -> None:
    skill = SKILL.read_text(encoding="utf-8")
    match = re.search(r"```bash\n(?P<command>find <path>.*?\n)```", skill, flags=re.DOTALL)
    assert match is not None
    command = match.group("command").replace("<path>", shlex.quote(str(tmp_path))).replace("<glob>", "*.py")

    completed = subprocess.run(
        ["bash", "-o", "pipefail", "-c", command],
        text=True,
        capture_output=True,
        check=False,
        cwd=REPO_ROOT,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == []
