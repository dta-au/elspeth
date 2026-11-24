"""Offline examples gate contracts, exercised with small local fixtures."""

from __future__ import annotations

import json
import os
import shutil
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
    # Stub packages so the gate's provenance check resolves elspeth and elspeth_lints inside each case copy.
    for package in (tree / "src" / "elspeth", tree / "elspeth-lints" / "src" / "elspeth_lints"):
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
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
        cwd=tmp_path,
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


@pytest.mark.parametrize("url", ["HTTPS://example.com/x", "postgresql://db.example.com/data", "s3://bucket.example.com/key"])
def test_guard_refuses_remote_url_schemes(tmp_path: Path, url: str) -> None:
    tree, _ = _fixture(tmp_path)
    config = tree / "examples/demo/settings.yaml"
    config.write_text(config.read_text() + f"endpoint: {url}\n")
    assert any("non-loopback URL" in finding for finding in examples_gate.network_findings(tree, config))


def test_guard_uses_url_host_after_userinfo(tmp_path: Path) -> None:
    tree, _ = _fixture(tmp_path)
    config = tree / "examples/demo/settings.yaml"
    # The userinfo names loopback; the host does not. A guard that reads the first name after :// is fooled.
    config.write_text(config.read_text() + "endpoint: postgresql://localhost@db.example.com/data\n")
    assert any("db.example.com" in finding for finding in examples_gate.network_findings(tree, config))
    config.write_text(config.read_text().replace("db.example.com", "127.0.0.1"))
    assert examples_gate.network_findings(tree, config) == []


@pytest.mark.parametrize("plugin", ["aws_s3", "dataverse", "azure_document_intelligence"])
def test_guard_refuses_remote_plugins(tmp_path: Path, plugin: str) -> None:
    tree, _ = _fixture(tmp_path)
    config = tree / "examples/demo/settings.yaml"
    config.write_text(config.read_text() + f"transforms: [{{plugin: {plugin}}}]\n")
    assert any("remote-service plugin" in finding for finding in examples_gate.network_findings(tree, config))


@pytest.mark.parametrize("state", ["missing", "oversize", "unreadable"])
def test_guard_refuses_uninspected_fixture(tmp_path: Path, state: str) -> None:
    tree, _ = _fixture(tmp_path)
    config = tree / "examples/demo/settings.yaml"
    fixture = tree / "examples/demo/input.csv"
    if state == "missing":
        fixture.unlink()
    elif state == "oversize":
        with fixture.open("wb") as output:
            output.truncate(50_000_000)
    else:
        fixture.unlink()
        fixture.mkdir()
    assert any("input fixture" in finding for finding in examples_gate.network_findings(tree, config))


@pytest.mark.parametrize("mutation", ["empty", "mismatch", "case_network", "skip", "control"])
def test_validate_refuses_uncovered_case_or_command(tmp_path: Path, mutation: str) -> None:
    tree, cases = _fixture(tmp_path)
    data = yaml.safe_load(cases.read_text())
    case = data["cases"][0]
    if mutation == "empty":
        case["configs"] = []
    elif mutation == "mismatch":
        second = dict(case)
        second["id"] = "second"
        second["configs"] = ["examples/demo/second.yaml"]
        data["cases"].append(second)
        (tree / "examples/demo/second.yaml").write_text((tree / "examples/demo/settings.yaml").read_text())
        data["examples"]["demo"]["configs"]["second.yaml"] = {"class": "NON-NETWORK", "case": "second"}
        data["examples"]["demo"]["configs"]["settings.yaml"]["case"] = "second"
    elif mutation == "skip":
        case["skip"] = "later"
    elif mutation == "case_network":
        data["examples"]["demo"]["configs"]["settings.yaml"]["class"] = "NETWORK"
    else:
        case["command"] = "echo first\necho second"
    cases.write_text(yaml.safe_dump(data))
    assert _validate(tree, cases) == 2


def test_execute_refuses_partial_case_list_before_sandbox(tmp_path: Path) -> None:
    tree, cases = _fixture(tmp_path)
    data = yaml.safe_load(cases.read_text())
    second = dict(data["cases"][0])
    second["id"] = "second"
    second["configs"] = ["examples/demo/second.yaml"]
    second["env"] = {"BAD": "unit\x1fseparator"}
    data["cases"].append(second)
    (tree / "examples/demo/second.yaml").write_text((tree / "examples/demo/settings.yaml").read_text())
    data["examples"]["demo"]["configs"]["second.yaml"] = {"class": "NON-NETWORK", "case": "second"}
    cases.write_text(yaml.safe_dump(data))
    log_dir = tmp_path / "logs"
    result = _dry_run(tmp_path, "--execute", "--tmp-root", str(tmp_path / "work"), "--log-dir", str(log_dir), "--min-mem-gb", "0")
    assert result.returncode != 0
    assert (log_dir / "overall.exit").read_text().strip() != "0"
    assert not (log_dir / "current-case.txt").exists()
    assert "case list" in result.stderr


@pytest.mark.parametrize("option", ["--log-dir", "--stop-file"])
def test_execute_refuses_control_output_inside_checkout(tmp_path: Path, option: str) -> None:
    result = _dry_run(tmp_path, "--execute", "--tmp-root", str(tmp_path / "work"), option, str(tmp_path / "checkout" / "out"))
    assert result.returncode == 2
    assert "inside the checkout" in result.stderr


def test_tmp_root_inside_exported_tree_is_refused(tmp_path: Path) -> None:
    result = _dry_run(tmp_path, "--tmp-root", str(tmp_path / "tree" / "work"))
    assert result.returncode == 2
    assert "inside --tree" in result.stderr


def test_exported_tree_symlink_outside_is_refused(tmp_path: Path) -> None:
    tree, _ = _fixture(tmp_path)
    (tree / "external").symlink_to(tmp_path / "outside")
    result = _dry_run(tmp_path)
    assert result.returncode == 2
    assert "symlink" in result.stderr


def test_relative_control_paths_are_absolute_in_plan(tmp_path: Path) -> None:
    _fixture(tmp_path)
    result = _dry_run(tmp_path, "--tree", "tree", "--cases", "cases.yaml", "--log-dir", "logs", "--stop-file", "STOP")
    assert result.returncode == 0, result.stderr
    assert f"cases      : {tmp_path / 'cases.yaml'}" in result.stdout
    assert f"source     : tree {tmp_path / 'tree'}" in result.stdout
    assert f"log-dir    : {tmp_path / 'logs'}" in result.stdout
    assert f"stop-file  : {tmp_path / 'STOP'}" in result.stdout


def _bwrap_unusable_reason() -> str | None:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return "bubblewrap is unavailable"
    probe = subprocess.run(
        [bwrap, "--dev-bind", "/", "/", "--unshare-net", "--unshare-pid", "--proc", "/proc", "--die-with-parent", "--", "true"],
        capture_output=True,
        text=True,
        check=False,
    )
    return None if probe.returncode == 0 else f"bubblewrap namespace self-test unavailable: {probe.stderr.strip()}"


def test_execute_refuses_a_sandbox_that_does_not_isolate(tmp_path: Path) -> None:
    # A bwrap that runs its command without namespaces must fail the self-test: the host's interfaces show.
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake = fake_bin / "bwrap"
    fake.write_text('#!/bin/sh\nwhile [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n')
    fake.chmod(0o755)
    _fixture(tmp_path)
    log_dir = tmp_path / "logs"
    result = _dry_run(
        tmp_path,
        "--execute",
        "--tmp-root",
        str(tmp_path / "work"),
        "--log-dir",
        str(log_dir),
        "--min-mem-gb",
        "0",
        env={"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "bwrap private-network self-test failed" in result.stderr
    assert (log_dir / "overall.exit").read_text().strip() == "2"
    assert not (log_dir / "current-case.txt").exists()


def _processes_with(token: str) -> list[int]:
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if token.encode() in cmdline:
            found.append(int(entry.name))
    return found


def test_case_processes_end_with_the_case_when_bwrap_works(tmp_path: Path) -> None:
    reason = _bwrap_unusable_reason()
    if reason is not None:
        pytest.skip(reason)
    token = f"elspeth-gate-leftover-{os.getpid()}-{tmp_path.name}"
    # Control: the instrument sees a live process carrying the token.
    control = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", token])
    try:
        assert control.pid in _processes_with(token)
    finally:
        control.terminate()
        control.wait()
    assert _processes_with(token) == []
    _, cases = _fixture(tmp_path)
    data = yaml.safe_load(cases.read_text())
    case = data["cases"][0]
    case["audit"] = []
    case["command"] = f"python -c 'import time; time.sleep(300)' {token} & echo verified"
    cases.write_text(yaml.safe_dump(data))
    log_dir = tmp_path / "logs"
    result = _dry_run(tmp_path, "--execute", "--tmp-root", str(tmp_path / "work"), "--log-dir", str(log_dir), "--min-mem-gb", "0")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "PASS demo" in (log_dir / "summary.txt").read_text()
    # The backgrounded sleeper lived in the case's PID namespace, which ended with the case.
    assert _processes_with(token) == []


def test_tree_digest_changes_for_mode_symlink_and_cache_bytes(tmp_path: Path) -> None:
    tree, _ = _fixture(tmp_path)
    manifest = tmp_path / "manifest"
    initial = examples_gate.digest_tree(tree, manifest)
    launcher = tree / "examples/demo/run.sh"
    launcher.chmod(0o755)
    mode_changed = examples_gate.digest_tree(tree, manifest)
    assert mode_changed != initial
    link = tree / "local-link"
    link.symlink_to("examples/demo/run.sh")
    first_link = examples_gate.digest_tree(tree, manifest)
    link.unlink()
    link.symlink_to("examples/demo/input.csv")
    assert examples_gate.digest_tree(tree, manifest) != first_link
    cache = tree / "__pycache__"
    cache.mkdir()
    pyc = cache / "example.pyc"
    pyc.write_bytes(b"one")
    first_cache = examples_gate.digest_tree(tree, manifest)
    pyc.write_bytes(b"two")
    assert examples_gate.digest_tree(tree, manifest) != first_cache


def test_execute_snapshots_controls_before_bwrap(tmp_path: Path) -> None:
    _, cases = _fixture(tmp_path)
    log_dir = tmp_path / "logs"
    result = _dry_run(
        tmp_path, "--execute", "--only", "nonexistent", "--tmp-root", str(tmp_path / "work"), "--log-dir", str(log_dir), "--min-mem-gb", "0"
    )
    # An unknown --only id stops the run in preflight, before bwrap and before any case.
    assert result.returncode == 2
    assert "case list failed" in result.stderr
    assert "--only names no case: nonexistent" in (log_dir / "preflight.txt").read_text()
    assert (log_dir / "cases.yaml").read_bytes() == cases.read_bytes()
    assert (log_dir / "examples_gate.py").read_bytes() == (GATE.parent / "examples_gate.py").read_bytes()
    preflight = (log_dir / "preflight.txt").read_text()
    assert "cases_sha256=" in preflight
    assert "helper_sha256=" in preflight


def test_execute_refuses_env_in_exported_tree(tmp_path: Path) -> None:
    tree, _ = _fixture(tmp_path)
    (tree / "examples/demo/.env").write_text("SECRET=sample\n")
    log_dir = tmp_path / "logs"
    result = _dry_run(tmp_path, "--execute", "--tmp-root", str(tmp_path / "work"), "--log-dir", str(log_dir), "--min-mem-gb", "0")
    assert result.returncode == 2
    assert "exported tree contains .env" in result.stderr
    assert (log_dir / "overall.exit").read_text().strip() == "2"


def test_execute_refuses_when_bwrap_namespace_cannot_start(tmp_path: Path) -> None:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        pytest.skip("bubblewrap is unavailable; missing-binary refusal is environment dependent")
    probe = subprocess.run(
        [bwrap, "--dev-bind", "/", "/", "--unshare-net", "--unshare-pid", "--proc", "/proc", "--die-with-parent", "--", "true"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode == 0:
        pytest.skip("bubblewrap works here; namespace failure cannot be exercised")
    _fixture(tmp_path)
    log_dir = tmp_path / "logs"
    result = _dry_run(tmp_path, "--execute", "--tmp-root", str(tmp_path / "work"), "--log-dir", str(log_dir), "--min-mem-gb", "0")
    assert result.returncode == 2
    assert "bwrap private-network self-test failed" in result.stderr
    assert (log_dir / "overall.exit").read_text().strip() == "2"


def test_execute_private_loopback_and_empty_home_when_bwrap_works(tmp_path: Path) -> None:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        pytest.skip("bubblewrap is unavailable")
    probe = subprocess.run(
        [bwrap, "--dev-bind", "/", "/", "--unshare-net", "--unshare-pid", "--proc", "/proc", "--die-with-parent", "--", "true"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip(f"bubblewrap namespace self-test unavailable: {probe.stderr.strip()}")
    _, cases = _fixture(tmp_path)
    data = yaml.safe_load(cases.read_text())
    case = data["cases"][0]
    case["audit"] = []
    case["server"] = {"command": "python -m http.server 8199 --bind 127.0.0.1", "port": 8199}
    case["command"] = (
        "python -c 'import os,socket,urllib.request; "
        'assert [line.split(":")[0].strip() for line in open("/proc/net/dev") if ":" in line] == ["lo"]; '
        'assert os.listdir(os.environ["HOME"]) == []; '
        'assert urllib.request.urlopen("http://127.0.0.1:8199/").status == 200; '
        'print("private-network-ok")\''
    )
    case["log_markers"] = ["private-network-ok"]
    cases.write_text(yaml.safe_dump(data))
    log_dir = tmp_path / "logs"
    result = _dry_run(tmp_path, "--execute", "--tmp-root", str(tmp_path / "work"), "--log-dir", str(log_dir), "--min-mem-gb", "0")
    assert result.returncode == 0, (result.stdout, result.stderr, (log_dir / "preflight.txt").read_text())
    assert "PASS demo" in (log_dir / "summary.txt").read_text()


def test_execute_refuses_nearer_env_when_bwrap_works(tmp_path: Path) -> None:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        pytest.skip("bubblewrap is unavailable")
    probe = subprocess.run(
        [bwrap, "--dev-bind", "/", "/", "--unshare-net", "--unshare-pid", "--proc", "/proc", "--die-with-parent", "--", "true"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip(f"bubblewrap namespace self-test unavailable: {probe.stderr.strip()}")
    _fixture(tmp_path)
    work = tmp_path / "work"
    (work / "cases").mkdir(parents=True)
    (work / "cases/.env").write_text("SECRET=sample\n")
    log_dir = tmp_path / "logs"
    result = _dry_run(tmp_path, "--execute", "--tmp-root", str(work), "--log-dir", str(log_dir), "--min-mem-gb", "0")
    assert result.returncode == 2
    assert "cases/.env exists" in result.stderr
    assert (log_dir / "overall.exit").read_text().strip() == "2"


def test_missing_sandbox_command_exit_is_fail_when_bwrap_works(tmp_path: Path) -> None:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        pytest.skip("bubblewrap is unavailable")
    probe = subprocess.run(
        [bwrap, "--dev-bind", "/", "/", "--unshare-net", "--unshare-pid", "--proc", "/proc", "--die-with-parent", "--", "true"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip(f"bubblewrap namespace self-test unavailable: {probe.stderr.strip()}")
    _, cases = _fixture(tmp_path)
    data = yaml.safe_load(cases.read_text())
    data["cases"][0]["command"] = 'kill -KILL "$PPID"'
    data["cases"][0]["audit"] = []
    cases.write_text(yaml.safe_dump(data))
    log_dir = tmp_path / "logs"
    result = _dry_run(tmp_path, "--execute", "--tmp-root", str(tmp_path / "work"), "--log-dir", str(log_dir), "--min-mem-gb", "0")
    assert result.returncode == 1
    assert "FAIL demo -- sandbox error: command exit file missing" in (log_dir / "summary.txt").read_text()
    assert (log_dir / "overall.exit").read_text().strip() == "1"


@pytest.mark.parametrize("command", ["list", "count"])
def test_only_refuses_an_id_that_names_no_case(tmp_path: Path, command: str, capsys: pytest.CaptureFixture[str]) -> None:
    # A mistyped id must not shrink the selection silently: the gate would run fewer cases and could still PASS.
    _, cases = _fixture(tmp_path)
    assert examples_gate.main([command, "--cases", str(cases), "--only", "demo"]) == 0
    assert examples_gate.main([command, "--cases", str(cases), "--only", "demo,dmeo"]) == 2
    assert "--only names no case: dmeo" in capsys.readouterr().err
