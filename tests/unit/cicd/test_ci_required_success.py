"""Every required producer must succeed before the parked aggregate can succeed."""

from __future__ import annotations

import re
import subprocess
from typing import Any

import pytest
import yaml
from tests.unit.cicd.workflow_sources import PREPARED_WORKFLOWS


def _aggregate() -> dict[str, Any]:
    workflow = yaml.safe_load((PREPARED_WORKFLOWS / "ci.yaml").read_text())
    return workflow["jobs"]["ci-success"]


def _run_results(job: dict[str, Any], results: dict[str, str]) -> subprocess.CompletedProcess[str]:
    assert job["if"] == "always()", "aggregate must run after a failed or skipped dependency"
    step = next(step for step in job["steps"] if step["name"] == "Check all jobs passed")
    script = step["run"]
    expressions = re.findall(r"\$\{\{ needs\.([a-z0-9-]+)\.result }}", script)
    assert expressions and len(expressions) == len(set(expressions))
    assert set(expressions) == set(job["needs"]) == set(results), "aggregate must inspect every required dependency"
    for name, status in results.items():
        script = script.replace("${{ needs." + name + ".result }}", status)
    return subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=5, check=False)


def test_required_success_chain_accepts_all_success() -> None:
    job = _aggregate()
    result = _run_results(job, dict.fromkeys(job["needs"], "success"))
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("dependency", _aggregate()["needs"])
@pytest.mark.parametrize("status", ["failure", "skipped", "cancelled", "timed_out", ""])
def test_required_success_chain_refuses_each_failed_skipped_cancelled_or_absent_result(dependency: str, status: str) -> None:
    job = _aggregate()
    results = dict.fromkeys(job["needs"], "success")
    assert _run_results(job, results).returncode == 0
    results[dependency] = status
    assert _run_results(job, results).returncode == 1


@pytest.mark.parametrize("mutation", ["aggregate-skip", "omitted-inspection", "omitted-dependency"])
def test_success_chain_instrument_detects_disconnected_or_skipped_aggregate(mutation: str) -> None:
    job = _aggregate()
    results = dict.fromkeys(job["needs"], "success")
    assert _run_results(job, results).returncode == 0
    if mutation == "aggregate-skip":
        job["if"] = "success()"
    elif mutation == "omitted-inspection":
        step = next(step for step in job["steps"] if step["name"] == "Check all jobs passed")
        step["run"] = step["run"].replace("${{ needs.landscape-proof.result }}", "success")
    else:
        job["needs"].remove("landscape-proof")
        del results["landscape-proof"]
    with pytest.raises(AssertionError):
        _run_results(job, results)
