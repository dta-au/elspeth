"""Offline examples gate contracts, exercised with small local fixtures."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts import examples_gate


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    tree = tmp_path / "tree"
    example = tree / "examples" / "demo"
    example.mkdir(parents=True)
    (example / "settings.yaml").write_text("source: {plugin: csv, options: {path: examples/demo/input.csv}}\nsinks: []\n")
    (example / "input.csv").write_text("id,url\n1,http://127.0.0.1:8199/item\n")
    (example / "run.sh").write_text("#!/bin/sh\n")
    cases = tmp_path / "cases.yaml"
    cases.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "examples": {
                    "demo": {
                        "class": "NON-NETWORK",
                        "configs": {"settings.yaml": {"class": "NON-NETWORK", "case": "demo"}},
                        "launchers": {"run.sh": {"class": "NON-NETWORK", "case": "demo"}},
                    }
                },
                "cases": [
                    {
                        "id": "demo",
                        "example": "demo",
                        "command": "./examples/demo/run.sh",
                        "expect_exit": [0],
                        "configs": ["examples/demo/settings.yaml"],
                        "expectation_source": "fixture",
                        "log_markers": ["verified"],
                        "audit": [
                            {
                                "db": "examples/demo/audit.db",
                                "runs": 1,
                                "status": ["completed"],
                                "source_rows": 2,
                                "sinks": {"output": 2},
                                "conservation": True,
                            }
                        ],
                    }
                ],
            }
        )
    )
    return tree, cases


def _validate(tree: Path, cases: Path) -> int:
    return examples_gate.main(["validate", "--cases", str(cases), "--tree", str(tree)])


def test_validate_accepts_complete_tree_and_refuses_unclassified_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tree, cases = _fixture(tmp_path)
    assert _validate(tree, cases) == 0
    (tree / "examples" / "unknown").mkdir()
    assert _validate(tree, cases) == 2
    assert "unknown" in capsys.readouterr().out


@pytest.mark.parametrize(
    "name,content",
    [
        ("extra.yaml", "source: {plugin: csv}\nsinks: []\n"),
        ("extra.sh", "#!/bin/sh\n"),
    ],
)
def test_validate_refuses_unlisted_config_or_launcher(tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, content: str) -> None:
    tree, cases = _fixture(tmp_path)
    assert _validate(tree, cases) == 0
    (tree / "examples" / "demo" / name).write_text(content)
    assert _validate(tree, cases) == 2
    assert name in capsys.readouterr().out


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_guard_accepts_loopback_in_config_and_input(tmp_path: Path, host: str) -> None:
    tree, _ = _fixture(tmp_path)
    config = tree / "examples" / "demo" / "settings.yaml"
    config.write_text(
        config.read_text()
        + f"endpoint: http://{host}:8199/\n"
        + f"transforms: [{{plugin: llm, options: {{provider: openai, base_url: 'http://{host}:8199/v1'}}}}]\n"
    )
    (config.parent / "input.csv").write_text(f"url\nhttp://{host}:8199/item\n")
    assert examples_gate.network_findings(tree, config) == []


@pytest.mark.parametrize(
    "where,content,reason",
    [
        ("config", "endpoint: https://example.com/item\n", "non-loopback URL"),
        ("input", "url\nhttps://example.com/item\n", "source row URL"),
        ("config", "transforms: [{plugin: azure_ai_search}]\n", "remote-service plugin"),
        ("config", "transforms: [{plugin: llm, options: {provider: openai}}]\n", "no base_url"),
    ],
)
def test_guard_refuses_remote_targets(tmp_path: Path, where: str, content: str, reason: str) -> None:
    tree, _ = _fixture(tmp_path)
    config = tree / "examples" / "demo" / "settings.yaml"
    if where == "config":
        config.write_text(config.read_text() + content)
    else:
        (config.parent / "input.csv").write_text(content)
    assert any(reason in finding for finding in examples_gate.network_findings(tree, config))


def _audit(tree: Path, outcomes: list[tuple[int, str]] | None = None) -> None:
    db = tree / "examples" / "demo" / "audit.db"
    with sqlite3.connect(db) as connection:
        connection.executescript(
            "CREATE TABLE runs (run_id TEXT, status TEXT, run_mode TEXT, started_at TEXT);"
            "CREATE TABLE rows (run_id TEXT, row_id INTEGER);"
            "CREATE TABLE tokens (run_id TEXT, token_id INTEGER, row_id INTEGER);"
            "CREATE TABLE token_outcomes (run_id TEXT, token_id INTEGER, completed INTEGER, sink_name TEXT, outcome TEXT, path TEXT);"
            "CREATE TABLE transform_errors (run_id TEXT);"
        )
        connection.execute("INSERT INTO runs VALUES ('r', 'completed', 'live', '2026-01-01')")
        connection.executemany("INSERT INTO rows VALUES ('r', ?)", [(1,), (2,)])
        connection.executemany("INSERT INTO tokens VALUES ('r', ?, ?)", [(1, 1), (2, 2)])
        for token, sink in outcomes if outcomes is not None else [(1, "output"), (2, "output")]:
            connection.execute("INSERT INTO token_outcomes VALUES ('r', ?, 1, ?, 'success', 'sink')", (token, sink))


def _check(tmp_path: Path, tree: Path, cases: Path, *, exit_code: int = 0, log: str = "verified") -> tuple[int, dict[str, Any]]:
    log_path = tmp_path / "run.log"
    evidence_path = tmp_path / "evidence.json"
    log_path.write_text(log)
    code = examples_gate.main(
        [
            "check",
            "--cases",
            str(cases),
            "--root",
            str(tree),
            "--case",
            "demo",
            "--exit",
            str(exit_code),
            "--log",
            str(log_path),
            "--evidence",
            str(evidence_path),
        ]
    )
    return code, json.loads(evidence_path.read_text())


def test_check_accepts_matching_audit_and_conservation(tmp_path: Path) -> None:
    tree, cases = _fixture(tmp_path)
    _audit(tree)
    code, evidence = _check(tmp_path, tree, cases)
    assert code == 0
    assert evidence["verdict"] == "PASS"
    assert any("rows_without_exactly_one_terminal=0" in item for item in evidence["evidence"])


@pytest.mark.parametrize(
    "change,needle",
    [
        ("rows", "source rows"),
        ("status", "status failed"),
        ("exit", "exit 4"),
        ("marker", "log marker"),
        ("sink", "sink output"),
    ],
)
def test_check_refuses_wrong_evidence(tmp_path: Path, change: str, needle: str) -> None:
    tree, cases = _fixture(tmp_path)
    _audit(tree)
    if change in {"rows", "status"}:
        with sqlite3.connect(tree / "examples" / "demo" / "audit.db") as connection:
            if change == "rows":
                connection.execute("DELETE FROM rows WHERE row_id=2")
            else:
                connection.execute("UPDATE runs SET status='failed'")
    if change == "sink":
        with sqlite3.connect(tree / "examples" / "demo" / "audit.db") as connection:
            connection.execute("UPDATE token_outcomes SET sink_name='other' WHERE token_id=2")
    code, evidence = _check(tmp_path, tree, cases, exit_code=4 if change == "exit" else 0, log="" if change == "marker" else "verified")
    assert code == 1
    assert any(needle in failure for failure in evidence["failures"])


@pytest.mark.parametrize("outcomes", [[(1, "output")], [(1, "output"), (1, "output"), (2, "output")]])
def test_conservation_refuses_zero_or_two_terminal_outcomes(tmp_path: Path, outcomes: list[tuple[int, str]]) -> None:
    tree, cases = _fixture(tmp_path)
    _audit(tree, outcomes)
    code, evidence = _check(tmp_path, tree, cases)
    assert code == 1
    assert any("lack exactly one terminal outcome" in failure for failure in evidence["failures"])


GATE = Path(__file__).resolve().parents[3] / "scripts" / "examples-gate.sh"
# Never created: the dry run writes nothing, so the tmp-root may not exist.
_ABSENT = Path("/nonexistent-elspeth-examples-gate-test")


def _dry_run(tmp_path: Path, *options: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    tree, cases = (tmp_path / "tree", tmp_path / "cases.yaml") if (tmp_path / "cases.yaml").exists() else _fixture(tmp_path)
    root = tmp_path / "checkout"
    root.mkdir(exist_ok=True)
    run_env = {**os.environ, "HOME": str(_ABSENT / "home")}
    run_env.pop("XDG_CACHE_HOME", None)
    run_env.update(env or {})
    venv = Path(sys.executable).parent.parent
    return subprocess.run(
        ["bash", str(GATE), "--root", str(root), "--tree", str(tree), "--cases", str(cases), "--venv", str(venv), *options],
        capture_output=True,
        text=True,
        env=run_env,
        check=False,
    )


def _planned_tmp_root(result: subprocess.CompletedProcess[str]) -> str:
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip().startswith("tmp-root")]
    assert len(lines) == 1, result.stdout
    return lines[0].split(":", 1)[1].strip()


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, _ABSENT / "home" / ".cache" / "elspeth-examples-gate"),
        ({"XDG_CACHE_HOME": str(_ABSENT / "xdg")}, _ABSENT / "xdg" / "elspeth-examples-gate"),
    ],
)
def test_tmp_root_defaults_to_user_cache_and_dry_run_creates_nothing(tmp_path: Path, env: dict[str, str], expected: Path) -> None:
    assert _planned_tmp_root(_dry_run(tmp_path, env=env)) == str(expected)
    assert not _ABSENT.exists()


@pytest.mark.parametrize("tmp_root", ["/tmp", "/tmp/gate", "/var/../tmp/gate"])
def test_tmp_root_under_tmp_is_refused(tmp_path: Path, tmp_root: str) -> None:
    result = _dry_run(tmp_path, "--tmp-root", tmp_root)
    assert result.returncode == 2
    assert "must not be under /tmp" in result.stderr


def test_tmp_root_inside_checkout_is_refused_but_a_sibling_prefix_is_not(tmp_path: Path) -> None:
    inside = _dry_run(tmp_path, "--tmp-root", str(tmp_path / "checkout" / "gate"))
    assert inside.returncode == 2
    assert "must not be inside the checkout" in inside.stderr
    sibling = _dry_run(tmp_path, "--tmp-root", str(tmp_path / "checkout-gate"))
    assert "inside the checkout" not in sibling.stderr
    # tmp_path may itself sit under /tmp (default basetemp); then the sibling meets the /tmp refusal instead.
    assert sibling.returncode == 0 or "must not be under /tmp" in sibling.stderr


def test_tmp_root_prefix_of_tmp_is_not_refused(tmp_path: Path) -> None:
    assert _planned_tmp_root(_dry_run(tmp_path, "--tmp-root", "/tmpgate-elspeth-test")) == "/tmpgate-elspeth-test"
