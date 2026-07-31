"""Regression coverage for ELSPETH's Wardline trust-vocabulary gate."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_MODULE = "scripts.wardline_pack"
GATE_MODULE = "scripts.wardline_gate"


def _load_gate_module():  # type: ignore[no-untyped-def]
    gate_path = REPO_ROOT / "scripts" / "wardline_gate.py"
    assert gate_path.is_file(), "the canonical Wardline gate script is missing"
    return importlib.import_module(GATE_MODULE)


def test_weft_config_declares_elspeth_pack() -> None:
    config = tomllib.loads((REPO_ROOT / "weft.toml").read_text(encoding="utf-8"))

    assert config["wardline"]["packs"] == [PACK_MODULE]


def test_canonical_gate_grants_only_the_declared_local_pack(tmp_path: Path) -> None:
    gate = _load_gate_module()

    command = gate._wardline_command("wardline", tmp_path / "summary.json")

    assert command.count("--trust-pack") == 1
    trust_index = command.index("--trust-pack")
    assert command[trust_index + 1] == PACK_MODULE
    assert command.count("--allow-custom-packs") == 1
    assert command.count("--local-only") == 1
    assert command.count("--fail-on") == 1
    assert command[command.index("--fail-on") + 1] == "ERROR"
    assert command[command.index("--format") + 1] == "agent-summary"


@pytest.mark.parametrize(
    ("resolution", "message"),
    [
        ({"inert": True, "recognized_boundaries": 7}, "inert"),
        ({"inert": False, "recognized_boundaries": 0}, "zero recognized"),
    ],
)
def test_canonical_gate_rejects_false_green_resolution(resolution: dict[str, object], message: str) -> None:
    gate = _load_gate_module()

    with pytest.raises(gate.GateContractError, match=message):
        gate._validated_recognized_boundaries(
            {
                "schema": "wardline-agent-summary-1",
                "resolution": resolution,
            }
        )


def test_canonical_gate_accepts_non_inert_resolution() -> None:
    gate = _load_gate_module()

    recognized = gate._validated_recognized_boundaries(
        {
            "schema": "wardline-agent-summary-1",
            "resolution": {"inert": False, "recognized_boundaries": 7},
        }
    )

    assert recognized == 7


@pytest.mark.skipif(shutil.which("wardline") is None, reason="Wardline CLI is an operator tool, not a pytest dependency")
def test_project_pack_bridges_elspeth_decorators_in_bounded_cli_fixture(tmp_path: Path) -> None:
    """Exercise the real pack/config/CLI seam without scanning the full repository."""
    fixture_root = tmp_path / "fixture"
    source_root = fixture_root / "src"
    source_root.mkdir(parents=True)
    (fixture_root / "weft.toml").write_text(
        f'[wardline]\nsource_roots = ["src"]\npacks = ["{PACK_MODULE}"]\n',
        encoding="utf-8",
    )
    (source_root / "target.py").write_text(
        "from elspeth.contracts.trust_boundary import trust_boundary\n\n"
        "def helper_one(): return 1\n"
        "def helper_two(): return 2\n"
        "def helper_three(): return 3\n"
        "def helper_four(): return 4\n\n"
        "@trust_boundary(tier=3, source='fixture', source_param='payload', suppresses=(), invariant='validates')\n"
        "def validates(payload):\n"
        "    return str(payload)\n\n"
        "@trust_boundary(tier=3, source='fixture', source_param='payload', suppresses=(), invariant='rejects')\n"
        "def leaks(payload):\n"
        "    return payload\n",
        encoding="utf-8",
    )
    output = tmp_path / "summary.json"
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")

    completed = subprocess.run(
        [
            "wardline",
            "scan",
            str(fixture_root),
            "--config",
            str(fixture_root / "weft.toml"),
            "--format",
            "agent-summary",
            "--output",
            str(output),
            "--fail-on",
            "ERROR",
            "--trust-pack",
            PACK_MODULE,
            "--allow-custom-packs",
            "--local-only",
        ],
        cwd=fixture_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1, completed.stderr
    summary = json.loads(output.read_text(encoding="utf-8"))
    assert summary["resolution"]["inert"] is False
    assert summary["resolution"]["recognized_boundaries"] == 2
    assert any(finding["rule_id"] == "PY-WL-119" for finding in summary["active_defects"])
