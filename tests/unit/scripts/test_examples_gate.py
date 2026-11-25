"""Offline examples gate contracts, exercised with small local fixtures."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts import examples_gate


def _needs_external_work_base(candidate: Path) -> bool:
    return candidate.resolve().is_relative_to(Path("/tmp"))


@pytest.fixture
def tmp_path(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    candidate = tmp_path_factory.mktemp("examples-gate")
    if not _needs_external_work_base(candidate):
        yield candidate
        return
    work = Path.home() / ".cache" / "elspeth-examples-gate-tests" / f"{os.getpid()}-{uuid.uuid4().hex}"
    work.mkdir(parents=True)
    try:
        yield work
    finally:
        shutil.rmtree(work)


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [(Path("/tmp/pytest-case"), True), (Path("/var/tmp/pytest-case"), False), (Path.home() / "gate-case", False)],
)
def test_execute_work_base_selection(candidate: Path, expected: bool) -> None:
    assert _needs_external_work_base(candidate) is expected


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


def _dry_run(tmp_path: Path, *options: str, env: dict[str, str] | None = None, gate: Path = GATE) -> subprocess.CompletedProcess[str]:
    tree, cases = (tmp_path / "tree", tmp_path / "cases.yaml") if (tmp_path / "cases.yaml").exists() else _fixture(tmp_path)
    root = tmp_path / "checkout"
    root.mkdir(exist_ok=True)
    run_env = {**os.environ, "HOME": str(_ABSENT / "home")}
    run_env.pop("XDG_CACHE_HOME", None)
    run_env.update(env or {})
    venv = Path(sys.executable).parent.parent
    return subprocess.run(
        ["bash", str(gate), "--root", str(root), "--tree", str(tree), "--cases", str(cases), "--venv", str(venv), *options],
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
    second["command"] = "echo unit\x1fseparator"
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


# A fake bwrap that does not run the self-test probe must forge this run's nonce explicitly:
# the probe's arguments are -c SOURCE NONCE ...
_FORGED_SELF_TEST = 'while [ "$1" != -c ]; do shift; done\necho "self-test-ok $3"\n'


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
    assert "driver_sha256=" in preflight
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
    case["server"] = {"command": "chaosweb serve --port 8199 --preset=realistic --workers=1", "port": 8199}
    case["command"] = (
        "python -c 'import os,socket; "
        'assert [line.split(":")[0].strip() for line in open("/proc/net/dev") if ":" in line] == ["lo"]; '
        'assert os.listdir(os.environ["HOME"]) == []; '
        'socket.create_connection(("127.0.0.1", 8199), timeout=5).close(); '
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_row", 8),
        ("expect_exit", "0"),
        ("files", {"output": "present"}),
        ("requires", {"home_ro": ["../other"]}),
        ("server", {"command": "other serve", "port": 8199}),
        ("server", {"command": "chaosllm serve --port 8199 `unexpected`", "port": 8199}),
        ("env", {"OUTPUT": "a b"}),
    ],
)
def test_validate_rejects_bad_case_shape(tmp_path: Path, field: str, value: Any) -> None:
    tree, cases = _fixture(tmp_path)
    data = yaml.safe_load(cases.read_text())
    data["cases"][0][field] = value
    cases.write_text(yaml.safe_dump(data))
    assert _validate(tree, cases) == 2


@pytest.mark.parametrize(("field", "value"), [("source_row", 8), ("status", "completed")])
def test_validate_rejects_bad_audit_shape(tmp_path: Path, field: str, value: Any) -> None:
    tree, cases = _fixture(tmp_path)
    data = yaml.safe_load(cases.read_text())
    data["cases"][0]["audit"][0][field] = value
    cases.write_text(yaml.safe_dump(data))
    assert _validate(tree, cases) == 2


@pytest.mark.parametrize(
    ("server_failed", "exit_text", "sandbox_rc", "expected"),
    [(True, "0", 0, 1), (False, None, 1, 1), (False, "0", 1, 1), (False, "0", 0, 0)],
)
def test_classify_sandbox_result(tmp_path: Path, server_failed: bool, exit_text: str | None, sandbox_rc: int, expected: int) -> None:
    exit_file = tmp_path / "command.exit"
    marker = tmp_path / "server.failed"
    if exit_text is not None:
        exit_file.write_text(exit_text)
    if server_failed:
        marker.touch()
    assert (
        examples_gate.main(["classify", "--server-marker", str(marker), "--exit-file", str(exit_file), "--sandbox-rc", str(sandbox_rc)])
        == expected
    )


@pytest.mark.parametrize(
    ("passed", "failed", "notrun", "frozen", "expected"),
    [(1, 0, 0, "YES", "0"), (0, 0, 0, "YES", "1"), (1, 1, 0, "YES", "1"), (1, 0, 1, "YES", "1"), (1, 0, 0, "NO", "1")],
)
def test_overall_accounting(capsys: pytest.CaptureFixture[str], passed: int, failed: int, notrun: int, frozen: str, expected: str) -> None:
    assert (
        examples_gate.main(
            ["overall", "--pass-count", str(passed), "--fail-count", str(failed), "--notrun-count", str(notrun), "--frozen", frozen]
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == expected


def test_home_ro_files_must_be_under_a_declared_directory(tmp_path: Path) -> None:
    tree, cases = _fixture(tmp_path)
    data = yaml.safe_load(cases.read_text())
    data["cases"][0]["requires"] = {"home_ro": [".cache/model"], "files": ["~/.cache/other/model.bin"]}
    cases.write_text(yaml.safe_dump(data))
    assert _validate(tree, cases) == 2


def test_preflight_and_list_use_declared_home_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tree, cases = _fixture(tmp_path)
    home = tmp_path / "home"
    model_file = home / ".cache/model/weights.bin"
    model_file.parent.mkdir(parents=True)
    model_file.write_bytes(b"cached")
    data = yaml.safe_load(cases.read_text())
    data["cases"][0]["requires"] = {"home_ro": [".cache/model"], "files": ["~/.cache/model/weights.bin"]}
    cases.write_text(yaml.safe_dump(data))
    monkeypatch.setenv("HOME", str(home))
    assert _validate(tree, cases) == 0
    capsys.readouterr()
    assert examples_gate.main(["preflight", "--cases", str(cases), "--tree", str(tree), "--case", "demo"]) == 0
    assert "preflight ok" in capsys.readouterr().out
    assert examples_gate.main(["list", "--cases", str(cases)]) == 0
    assert ".cache/model" in capsys.readouterr().out


def test_reset_audit_removes_stale_db_in_case_copy(tmp_path: Path) -> None:
    tree, cases = _fixture(tmp_path)
    db = tree / "examples/demo/audit.db"
    db.touch()
    Path(f"{db}-wal").touch()
    Path(f"{db}-shm").touch()
    Path(f"{db}-journal").touch()
    assert examples_gate.main(["reset-audit", "--cases", str(cases), "--root", str(tree), "--case", "demo"]) == 0
    assert not db.exists()
    assert not Path(f"{db}-wal").exists()
    assert not Path(f"{db}-shm").exists()
    assert not Path(f"{db}-journal").exists()


def test_digest_raises_on_walk_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def failed_walk(_tree: Path, *, followlinks: bool, onerror: Any) -> Any:
        assert not followlinks
        onerror(OSError("unreadable directory"))
        return iter(())

    monkeypatch.setattr(examples_gate.os, "walk", failed_walk)
    with pytest.raises(OSError, match="unreadable directory"):
        examples_gate.digest_tree(tmp_path, tmp_path / "manifest")


def test_symlinked_tree_uses_physical_path_for_output_refusal(tmp_path: Path) -> None:
    tree, _ = _fixture(tmp_path)
    via = tmp_path / "via"
    via.symlink_to(tree, target_is_directory=True)
    result = _dry_run(tmp_path, "--tree", str(via), "--tmp-root", str(tree / "work"))
    assert result.returncode == 2
    assert "inside --tree" in result.stderr


@pytest.mark.parametrize("option", ["--log-dir", "--stop-file"])
def test_control_path_inside_tree_is_refused(tmp_path: Path, option: str) -> None:
    tree, _ = _fixture(tmp_path)
    result = _dry_run(tmp_path, option, str(tree / "control"))
    assert result.returncode == 2
    assert "inside --tree" in result.stderr


@pytest.mark.parametrize("option", ["--log-dir", "--stop-file"])
def test_control_path_under_tmp_is_refused(tmp_path: Path, option: str) -> None:
    result = _dry_run(tmp_path, option, "/tmp/elspeth-gate-control")
    assert result.returncode == 2
    assert "must not be under /tmp" in result.stderr


def test_missing_cases_directory_is_refused(tmp_path: Path) -> None:
    result = _dry_run(tmp_path, "--cases", str(tmp_path / "absent" / "cases.yaml"))
    assert result.returncode == 2
    assert "case data directory" in result.stderr


def test_fake_bwrap_records_shared_mount_flags_and_partial_summary(tmp_path: Path) -> None:
    _, cases = _fixture(tmp_path)
    data = yaml.safe_load(cases.read_text())
    data["cases"][0]["command"] = "echo verified"
    data["cases"][0]["audit"] = []
    second = dict(data["cases"][0])
    second.update(id="other", configs=[], skip="not selected")
    data["cases"].append(second)
    cases.write_text(yaml.safe_dump(data))
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    self_flags = tmp_path / "self-flags"
    case_flags = tmp_path / "case-flags"
    fake = fake_bin / "bwrap"
    fake.write_text(
        "#!/bin/bash\n"
        "flags=()\n"
        'while [ "$1" != "--" ]; do flags+=("$1"); shift; done\n'
        "shift\n"
        'if [ "$1" = bash ]; then\n'
        f"  printf '%s\\n' \"${{flags[@]}}\" > {shlex.quote(str(case_flags))}\n"
        '  exec "$@"\n'
        "fi\n"
        f"printf '%s\\n' \"${{flags[@]}}\" > {shlex.quote(str(self_flags))}\n" + _FORGED_SELF_TEST
    )
    fake.chmod(0o755)
    log_dir = tmp_path / "logs"
    result = _dry_run(
        tmp_path,
        "--execute",
        "--only",
        "demo",
        "--tmp-root",
        str(tmp_path / "work"),
        "--log-dir",
        str(log_dir),
        "--min-mem-gb",
        "0",
        env={"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    self_args = self_flags.read_text().splitlines()
    case_args = case_flags.read_text().splitlines()
    common = self_args[: self_args.index("--bind")]
    assert case_args[: len(common)] == common
    assert "--ro-bind" in common
    assert "--unshare-net" in common
    assert "--unshare-pid" in common
    assert common[common.index("/run") - 1 : common.index("/run") + 1] == ["--tmpfs", "/run"]
    # The filesystem is an allowlist: the host root is never bound, and the sandbox root is read-only.
    pairs = list(pairwise(common))
    assert ("/", "/") not in pairs
    assert self_args[-2:] == ["--remount-ro", "/"] and case_args[-2:] == ["--remount-ro", "/"]
    assert {source for source, _ in examples_gate.bind_sources(case_args)} >= {"/usr", str(Path(sys.executable).parent.parent.resolve())}
    assert "only=demo" in (log_dir / "preflight.txt").read_text()
    assert "scope=PARTIAL" in (log_dir / "summary.txt").read_text()
    assert (log_dir / "overall.exit").read_text().strip() == "0"


def test_unexpected_copy_failure_marks_overall_aborted(tmp_path: Path) -> None:
    _, cases = _fixture(tmp_path)
    data = yaml.safe_load(cases.read_text())
    data["cases"][0]["audit"] = []
    cases.write_text(yaml.safe_dump(data))
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    bwrap = fake_bin / "bwrap"
    bwrap.write_text(
        '#!/bin/bash\nwhile [ "$1" != "--" ]; do shift; done\nshift\nif [ "$1" = bash ]; then exit 0; fi\n' + _FORGED_SELF_TEST
    )
    bwrap.chmod(0o755)
    copy = fake_bin / "cp"
    counter = tmp_path / "cp-count"
    copy.write_text(
        "#!/bin/bash\n"
        f"counter={shlex.quote(str(counter))}\n"
        'n=$(cat "$counter" 2>/dev/null || echo 0)\n'
        'n=$((n + 1)); echo "$n" > "$counter"\n'
        'if [ "$n" -ge 3 ]; then exit 1; fi\n'
        'exec /usr/bin/cp "$@"\n'
    )
    copy.chmod(0o755)
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
    assert result.returncode != 0
    assert "(aborted)" in (log_dir / "overall.exit").read_text()


def test_checker_stderr_does_not_prefix_pass_verdict(tmp_path: Path) -> None:
    _, cases = _fixture(tmp_path)
    data = yaml.safe_load(cases.read_text())
    data["cases"][0]["command"] = "echo verified"
    data["cases"][0]["audit"] = []
    cases.write_text(yaml.safe_dump(data))
    driver_dir = tmp_path / "driver"
    driver_dir.mkdir()
    gate = driver_dir / "examples-gate.sh"
    gate.write_bytes(GATE.read_bytes())
    helper = driver_dir / "examples_gate.py"
    source = examples_gate.__file__
    helper_text = Path(source).read_text()
    needle = "    print(f\"{verdict} {case['id']} -- {detail}\")"
    assert needle in helper_text
    helper.write_text(helper_text.replace(needle, '    print("WARNING checker", file=sys.stderr)\n' + needle))
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake = fake_bin / "bwrap"
    fake.write_text(
        '#!/bin/bash\nwhile [ "$1" != "--" ]; do shift; done\nshift\nif [ "$1" = bash ]; then exec "$@"; fi\n' + _FORGED_SELF_TEST
    )
    fake.chmod(0o755)
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
        gate=gate,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    summary = (log_dir / "summary.txt").read_text()
    assert summary.startswith("PASS demo --")
    assert "WARNING checker" not in summary
    assert "WARNING checker" in (log_dir / "logs/demo.check.stderr.log").read_text()


def test_changed_helper_snapshot_marks_tree_unfrozen(tmp_path: Path) -> None:
    _, cases = _fixture(tmp_path)
    data = yaml.safe_load(cases.read_text())
    data["cases"][0]["command"] = "echo verified"
    data["cases"][0]["audit"] = []
    cases.write_text(yaml.safe_dump(data))
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    log_dir = tmp_path / "logs"
    fake = fake_bin / "bwrap"
    fake.write_text(
        "#!/bin/bash\n"
        'while [ "$1" != "--" ]; do shift; done\n'
        "shift\n"
        'if [ "$1" = bash ]; then\n'
        f"  printf '\\n# changed\\n' >> {shlex.quote(str(log_dir / 'examples_gate.py'))}\n"
        '  exec "$@"\n'
        "fi\n" + _FORGED_SELF_TEST
    )
    fake.chmod(0o755)
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
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "frozen=NO" in (log_dir / "summary.txt").read_text()
    assert (log_dir / "overall.exit").read_text().strip() == "1"


def _plant_socket(directory: Path, name: str = "planted.sock") -> Path:
    # Bound by a relative name: an absolute tmp_path can exceed the AF_UNIX path limit.
    import socket

    directory.mkdir(parents=True, exist_ok=True)
    cwd = Path.cwd()
    os.chdir(directory)
    try:
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(name)
        listener.close()
    finally:
        os.chdir(cwd)
    return directory / name


def test_socket_walk_refuses_a_socket_under_a_bind_and_names_the_bind(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bound = tmp_path / "bound"
    socket_path = _plant_socket(bound / "deep" / "er")
    (bound / "plain.txt").write_text("x")
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "plain.txt").write_text("x")
    assert examples_gate.main(["socket-walk", "--allow", str(tmp_path), "--", "--ro-bind", str(clean), str(clean), "--unshare-net"]) == 0
    assert "REFUSE" not in capsys.readouterr().out
    assert (
        examples_gate.main(
            ["socket-walk", "--allow", str(tmp_path), "--", "--ro-bind", str(clean), str(clean), "--bind", str(bound), "/case"]
        )
        == 1
    )
    out = capsys.readouterr().out
    assert f"REFUSE bind {bound}: socket {socket_path}" in out
    assert f"REFUSE bind {clean}" not in out


def test_socket_walk_follows_a_bound_symlink_to_its_socket(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    socket_path = _plant_socket(tmp_path / "real")
    link = tmp_path / "link"
    link.symlink_to(socket_path)
    assert examples_gate.main(["socket-walk", "--allow", str(tmp_path), "--", "--ro-bind-try", str(link), "/x"]) == 1
    assert f"REFUSE bind {link}: socket {socket_path}" in capsys.readouterr().out


def test_socket_walk_skips_only_directories_this_user_cannot_search(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can search every directory")
    bound = tmp_path / "bound"
    unsearchable = bound / "no-search"
    unlistable = bound / "search-only"
    unsearchable.mkdir(parents=True)
    unlistable.mkdir()
    unsearchable.chmod(0o000)
    unlistable.chmod(0o100)  # names beneath it stay reachable, so it cannot be cleared by a walk
    try:
        assert examples_gate.main(["socket-walk", "--allow", str(tmp_path), "--", "--ro-bind", str(bound), str(bound)]) == 1
    finally:
        unsearchable.chmod(0o700)
        unlistable.chmod(0o700)
    out = capsys.readouterr().out
    assert f"REFUSE bind {bound}: unlistable directory {unlistable}" in out
    assert f"bind {bound}: 1 directories this user cannot search" in out
    assert str(unsearchable) not in out


def test_socket_walk_refuses_a_missing_bind_but_not_an_optional_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "missing"
    assert examples_gate.main(["socket-walk", "--allow", str(tmp_path), "--", "--ro-bind-try", str(missing), "/x"]) == 0
    assert examples_gate.main(["socket-walk", "--allow", str(tmp_path), "--", "--ro-bind", str(missing), "/x"]) == 1
    assert f"REFUSE bind {missing}: missing" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["--frob", "x"], ["--ro-bind", "/usr"], ["--unshare-net", "--bind"]])
def test_socket_walk_refuses_an_unclassified_or_truncated_option(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert examples_gate.main(["socket-walk", "--", *argv]) == 2
    assert "DATA ERROR" in capsys.readouterr().err


def test_bind_sources_skip_symlink_and_mount_arguments() -> None:
    argv = ["--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--tmpfs", "/run", "--remount-ro", "/", "--bind", "/c", "/c"]
    assert examples_gate.bind_sources(argv) == [("/usr", False), ("/c", False)]


def test_a_sandbox_command_that_never_runs_the_probe_fails_the_self_test(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake = fake_bin / "bwrap"
    fake.write_text("#!/bin/bash\nexit 0\n")
    fake.chmod(0o755)
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
    assert not (log_dir / "current-case.txt").exists()


def test_a_socket_in_the_case_copy_refuses_the_run_and_names_the_bind(tmp_path: Path) -> None:
    tree, _ = _fixture(tmp_path)
    planted = _plant_socket(tree / "examples" / "demo" / "sock")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake = fake_bin / "bwrap"
    fake.write_text('#!/bin/bash\nwhile [ "$1" != "--" ]; do shift; done\nshift\nif [ "$1" = bash ]; then exit 0; fi\n' + _FORGED_SELF_TEST)
    fake.chmod(0o755)
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
    assert "case demo sandbox bind check failed" in result.stderr
    assert f"/demo/root/examples/demo/sock/{planted.name}" in result.stderr
    assert not (log_dir / "logs/demo.sandbox.log").exists()


def _gate_with_an_open_descriptor(tmp_path: Path, gate: Path, case_command: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    _, cases = _fixture(tmp_path)
    data = yaml.safe_load(cases.read_text())
    data["cases"][0]["audit"] = []
    data["cases"][0]["command"] = case_command
    data["cases"][0]["log_markers"] = ["descriptors-ok"]
    cases.write_text(yaml.safe_dump(data))
    log_dir = tmp_path / "logs"
    held = (tmp_path / "held.txt").open("w")
    try:
        tree, cases_path = tmp_path / "tree", tmp_path / "cases.yaml"
        run_env = {**os.environ, "HOME": str(_ABSENT / "home")}
        run_env.pop("XDG_CACHE_HOME", None)
        result = subprocess.run(
            [
                "bash",
                str(gate),
                "--root",
                str(tmp_path / "checkout"),
                "--tree",
                str(tree),
                "--cases",
                str(cases_path),
                "--venv",
                str(Path(sys.executable).parent.parent),
                "--execute",
                "--tmp-root",
                str(tmp_path / "work"),
                "--log-dir",
                str(log_dir),
                "--min-mem-gb",
                "0",
            ],
            capture_output=True,
            text=True,
            env=run_env,
            cwd=tmp_path,
            check=False,
            pass_fds=(held.fileno(),),
        )
    finally:
        held.close()
    return result, log_dir


_LISTS_ONLY_STANDARD_DESCRIPTORS = (
    "python -c 'import os; "
    'extra = [fd for fd in os.listdir("/proc/self/fd") if int(fd) > 2 and os.path.exists("/proc/self/fd/" + fd)]; '
    'assert extra == [], extra; print("descriptors-ok")\''
)


def test_no_descriptor_above_two_reaches_the_sandbox_when_bwrap_works(tmp_path: Path) -> None:
    reason = _bwrap_unusable_reason()
    if reason is not None:
        pytest.skip(reason)
    (tmp_path / "checkout").mkdir()
    result, log_dir = _gate_with_an_open_descriptor(tmp_path, GATE, _LISTS_ONLY_STANDARD_DESCRIPTORS)
    assert result.returncode == 0, (result.stdout, result.stderr, (log_dir / "preflight.txt").read_text())
    assert "PASS demo" in (log_dir / "summary.txt").read_text()


def test_an_inherited_descriptor_fails_the_self_test_when_bwrap_works(tmp_path: Path) -> None:
    # The control for the test above: the same run through a gate that does not close descriptors.
    reason = _bwrap_unusable_reason()
    if reason is not None:
        pytest.skip(reason)
    (tmp_path / "checkout").mkdir()
    driver = tmp_path / "driver"
    driver.mkdir()
    gate_text = GATE.read_text()
    closer = '            if [ "$fd" -gt 2 ]; then exec {fd}>&-; fi\n'
    assert gate_text.count(closer) == 1
    (driver / "examples-gate.sh").write_text(gate_text.replace(closer, "            :\n"))
    shutil.copy2(GATE.parent / "examples_gate.py", driver / "examples_gate.py")
    result, log_dir = _gate_with_an_open_descriptor(tmp_path, driver / "examples-gate.sh", _LISTS_ONLY_STANDARD_DESCRIPTORS)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "bwrap private-network self-test failed" in result.stderr
    assert "held.txt" in (log_dir / "self-test.out").read_text()


def test_the_case_copy_holds_tracked_content_only_and_the_environment_no_credentials(tmp_path: Path) -> None:
    tree, cases = _fixture(tmp_path)
    data = yaml.safe_load(cases.read_text())
    data["cases"][0]["audit"] = []
    data["cases"][0]["command"] = "echo verified"
    cases.write_text(yaml.safe_dump(data))
    checkout = tmp_path / "checkout"
    shutil.copytree(tree, checkout)
    (checkout / ".claude").mkdir()
    (checkout / ".claude" / "settings.json").write_text("{}\n")
    git = ["git", "-C", str(checkout), "-c", "user.name=gate-test", "-c", "user.email=gate-test@example.invalid"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "fixture"], check=True)
    # Untracked: never exported.
    (checkout / ".env").write_text("SAMPLE_PROVIDER_KEY=not-a-real-value\n")
    (checkout / ".claude" / "untracked.txt").write_text("x\n")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    seen_env = tmp_path / "seen-env"
    seen_files = tmp_path / "seen-files"
    fake = fake_bin / "bwrap"
    fake.write_text(
        "#!/bin/bash\n"
        'while [ "$1" != "--" ]; do shift; done\n'
        "shift\n"
        'if [ "$1" = bash ]; then\n'
        f"  env > {shlex.quote(str(seen_env))}\n"
        f'  (cd "$3" && find . -mindepth 1 | sort) > {shlex.quote(str(seen_files))}\n'
        '  exec "$@"\n'
        "fi\n" + _FORGED_SELF_TEST
    )
    fake.chmod(0o755)
    log_dir = tmp_path / "logs"
    run_env = {**os.environ, "HOME": str(_ABSENT / "home")}
    run_env.pop("XDG_CACHE_HOME", None)
    run_env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "OPENAI_API_KEY": "not-a-real-value",
            "ANTHROPIC_API_KEY": "not-a-real-value",
            "AWS_SECRET_ACCESS_KEY": "not-a-real-value",
        }
    )
    result = subprocess.run(
        [
            "bash",
            str(GATE),
            "--root",
            str(checkout),
            "--cases",
            str(cases),
            "--venv",
            str(Path(sys.executable).parent.parent),
            "--execute",
            "--tmp-root",
            str(tmp_path / "work"),
            "--log-dir",
            str(log_dir),
            "--min-mem-gb",
            "0",
        ],
        capture_output=True,
        text=True,
        env=run_env,
        cwd=tmp_path,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr, (log_dir / "preflight.txt").read_text())
    exported = list((tmp_path / "work" / "trees").iterdir())
    assert len(exported) == 1
    # Controls: tracked .claude content is exported; the untracked files exist in the checkout.
    assert (exported[0] / ".claude" / "settings.json").is_file()
    assert not (exported[0] / ".env").exists() and not (exported[0] / ".claude" / "untracked.txt").exists()
    assert (checkout / ".env").is_file()
    files = seen_files.read_text().splitlines()
    assert "./examples/demo/settings.yaml" in files
    assert not [name for name in files if name.startswith("./.claude") or name.endswith(".env")], files
    names = {line.split("=", 1)[0] for line in seen_env.read_text().splitlines() if "=" in line}
    assert "PYTHONPATH" in names
    assert not names & {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY"}
    assert not [name for name in names if any(word in name for word in ("KEY", "TOKEN", "SECRET")) and name != "ELSPETH_FINGERPRINT_KEY"]


def test_socket_walk_refuses_the_host_root_before_walking_any_bind(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # The planted socket is in a bind listed first: had any walk started, it would be reported.
    bound = tmp_path / "bound"
    _plant_socket(bound)
    argv = ["socket-walk", "--allow", str(tmp_path), "--", "--bind", str(bound), "/case", "--ro-bind", "/", "/"]
    assert examples_gate.main(argv) == 1
    assert capsys.readouterr().out.splitlines() == ["REFUSE bind /: the host root"]


@pytest.mark.parametrize(
    "source,reason",
    [
        ("/proc/self", "/proc/self is a kernel or device tree"),
        ("/sys", "/sys is a kernel or device tree"),
        ("/var", "outside the allowed bind roots"),
    ],
)
def test_socket_walk_refuses_a_source_outside_the_allowed_roots(source: str, reason: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert examples_gate.main(["socket-walk", "--", "--ro-bind", source, "/x"]) == 1
    assert capsys.readouterr().out.splitlines() == [f"REFUSE bind {source}: {reason}"]


def test_a_mount_point_beneath_a_source_is_a_finding_and_is_not_entered() -> None:
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        point = line.split()[4]
        if point.startswith("/dev/") and point.count("/") == 2:
            mounts.append(point)
    if not mounts:
        pytest.skip("no mount point directly under /dev here")
    findings, _ = examples_gate.sockets_under(Path("/dev"))
    for point in mounts:
        assert f"mount point {point}" in findings
    assert not [finding for finding in findings if any(finding.startswith(f"socket {point}/") for point in mounts)]


def test_a_bind_check_that_ends_without_a_verdict_says_how_it_ended(tmp_path: Path) -> None:
    _fixture(tmp_path)
    driver = tmp_path / "driver"
    driver.mkdir()
    shutil.copy2(GATE, driver / "examples-gate.sh")
    helper_text = (GATE.parent / "examples_gate.py").read_text()
    needle = "def cmd_socket_walk(args: argparse.Namespace) -> int:\n"
    assert helper_text.count(needle) == 1
    (driver / "examples_gate.py").write_text(
        helper_text.replace(needle, needle + "    import signal\n\n    os.kill(os.getpid(), signal.SIGTERM)\n")
    )
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
        gate=driver / "examples-gate.sh",
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "REFUSED: shared sandbox bind check ended without a result (signal 15)" in result.stderr
    assert not (log_dir / "self-test.out").exists()


def test_a_host_root_in_the_shared_binds_is_refused_by_the_bind_check_before_the_self_test(tmp_path: Path) -> None:
    _fixture(tmp_path)
    driver = tmp_path / "driver"
    driver.mkdir()
    gate_text = GATE.read_text()
    needle = "BWRAP_ARGS=(--ro-bind /usr /usr)\n"
    assert gate_text.count(needle) == 1
    (driver / "examples-gate.sh").write_text(gate_text.replace(needle, "BWRAP_ARGS=(--ro-bind / / --ro-bind /usr /usr)\n"))
    shutil.copy2(GATE.parent / "examples_gate.py", driver / "examples_gate.py")
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
        gate=driver / "examples-gate.sh",
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "REFUSED: shared sandbox bind check failed: REFUSE bind /: the host root" in result.stderr
    assert not (log_dir / "self-test.out").exists()
