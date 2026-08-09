"""Tests for the skill RGR CLI entrypoint."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from scripts.skill_rgr import run

REPO_ROOT = Path(__file__).resolve().parents[3]

_SCENARIOS = (
    ("batch1", "batch1_mechanical_fact_fixes"),
    ("batch1_pressured", "batch1_pressured"),
    ("batch1_refactor_incomplete", "batch1_refactor_incomplete"),
    ("batch1_refactor_insist", "batch1_refactor_insist"),
    ("batch1_refactor_override", "batch1_refactor_override"),
    ("batch3_bootstrap", "batch3_bootstrap"),
    ("batch3_contract_loop", "batch3_contract_loop"),
    ("batch3_contract_loop_insist", "batch3_contract_loop_insist"),
    ("calibration", "calibration"),
)


@pytest.mark.parametrize(("name", "expected_scenario_name"), _SCENARIOS)
def test_closed_scenario_registry_loads_every_supported_scenario(name: str, expected_scenario_name: str) -> None:
    scenario = run._load_scenario(name)

    assert scenario.name == expected_scenario_name


def test_closed_scenario_registry_rejects_unknown_before_import() -> None:
    with pytest.raises(ValueError, match="Unknown scenario"):
        run._load_scenario("unknown")


def test_help_does_not_import_harness_before_argument_parsing(tmp_path: Path) -> None:
    sitecustomize = tmp_path / "sitecustomize.py"
    sitecustomize.write_text(
        "\n".join(
            [
                "import importlib.abc",
                "import sys",
                "",
                "class BlockHarness(importlib.abc.MetaPathFinder):",
                "    def find_spec(self, fullname, path, target=None):",
                "        if fullname == 'harness':",
                "            raise RuntimeError('harness imported before args parsed')",
                "        return None",
                "",
                "sys.meta_path.insert(0, BlockHarness())",
            ]
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{tmp_path}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else str(tmp_path)

    result = subprocess.run(
        [sys.executable, "-m", "scripts.skill_rgr.run", "--help"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "usage: run.py" in result.stdout
    assert "harness imported before args parsed" not in result.stderr
