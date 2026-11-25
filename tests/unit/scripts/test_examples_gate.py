"""Offline examples gate contracts, exercised with small local fixtures."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import sysconfig
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
    assert "bwrap sandbox self-test failed" in result.stderr
    assert (log_dir / "overall.exit").read_text().strip() == "2"
    assert not (log_dir / "current-case.txt").exists()


# A fake bwrap that does not run the self-test probe must forge this run's nonce explicitly:
# the probe's arguments are -c SOURCE NONCE ...
_FORGED_SELF_TEST = 'while [ "$1" != -c ]; do shift; done\necho "self-test-ok $3"\n'
# A fake bwrap runs the case on the host, with no private mounts: it forges the case's mount check
# (inner.sh argument 10, "${12}" after "bash inner.sh") as it forges the self-test.


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
    assert "bwrap sandbox self-test failed" in result.stderr
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
        '  printf \'print("mount-check-ok")\\n\' > "${12}"\n'
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
    venv = str(Path(sys.executable).parent.parent.resolve())
    assert {source for source, _ in examples_gate.bind_sources(case_args)} >= {"/usr", venv}
    # The venv is bound read-only (the self-test's write probe checks it inside), IPC and session are private,
    # and the /etc sources are exactly the listed entries.
    assert ("--ro-bind", venv, venv) in set(zip(common, common[1:], common[2:], strict=False))
    assert "--unshare-ipc" in common and "--new-session" in common
    etc_sources = {source for source, _ in examples_gate.bind_sources(common) if source.startswith("/etc")}
    assert etc_sources == set(examples_gate.ETC_BIND_SOURCES)
    assert "only=demo" in (log_dir / "preflight.txt").read_text()
    # The case's check covered the shared binds again (sources and mount points) and walked only its own.
    case_checks = [line for line in (log_dir / "preflight.txt").read_text().splitlines() if line.startswith("socket-walk complete: ")]
    assert len(case_checks) == 2, case_checks
    checked, walked = (int(word) for word in case_checks[1].replace(",", "").split() if word.isdigit())
    assert walked == 1 and checked > walked, case_checks
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
        '#!/bin/bash\nwhile [ "$1" != "--" ]; do shift; done\nshift\nif [ "$1" = bash ]; then printf \'print("mount-check-ok")\\n\' > "${12}"; exec "$@"; fi\n'
        + _FORGED_SELF_TEST
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
        '  printf \'print("mount-check-ok")\\n\' > "${12}"\n'
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
    assert "bwrap sandbox self-test failed" in result.stderr
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


def _gate_with_an_open_descriptor(
    tmp_path: Path, gate: Path, case_command: str, venv: Path | None = None
) -> tuple[subprocess.CompletedProcess[str], Path]:
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
                str(venv or Path(sys.executable).parent.parent),
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
    assert "bwrap sandbox self-test failed" in result.stderr
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
        '  printf \'print("mount-check-ok")\\n\' > "${12}"\n'
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


@pytest.mark.parametrize("source", ["/usr/../var", "/etc/../home"])
def test_a_source_with_dot_segments_is_judged_where_it_points(source: str) -> None:
    assert examples_gate.bind_source_refusal(source, []) == "outside the allowed bind roots"


def test_a_directory_or_file_linked_out_of_an_allowed_root_is_refused(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    (outside / "file.txt").write_text("x")
    (allowed / "dir-link").symlink_to(outside)
    (allowed / "file-link").symlink_to(outside / "file.txt")
    reason = examples_gate.bind_source_refusal(str(allowed / "dir-link"), [str(allowed)])
    assert reason == f"resolves to {outside}, outside the allowed bind roots"
    reason = examples_gate.bind_source_refusal(str(allowed / "file-link"), [str(allowed)])
    assert reason == f"resolves to {outside / 'file.txt'}, outside the allowed bind roots"
    assert examples_gate.bind_source_refusal(str(allowed), [str(allowed)]) is None


def test_mount_points_are_decoded_and_found_beneath_a_source_whatever_their_device(tmp_path: Path) -> None:
    source = tmp_path / "venv"
    source.mkdir()
    mountinfo = "\n".join(
        [
            "22 1 8:2 / / rw - ext4 /dev/sda2 rw",
            # A bind mount on the same filesystem (same device as its parent) and a mount over one file.
            f"40 22 8:2 /elsewhere {source}/lib/same\\040fs rw - ext4 /dev/sda2 rw",
            f"41 22 8:2 /a-file {source}/bin/tool rw - ext4 /dev/sda2 rw",
            f"42 22 0:5 / {source}2 rw - tmpfs tmpfs rw",
        ]
    )
    points = examples_gate.mount_points(mountinfo)
    assert f"{source}/lib/same fs" in points
    assert examples_gate.mounts_beneath(str(source), points) == [f"{source}/bin/tool", f"{source}/lib/same fs"]


def test_socket_walk_prints_its_completion_line_only_when_it_finished(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clean = tmp_path / "clean"
    clean.mkdir()
    assert examples_gate.main(["socket-walk", "--allow", str(tmp_path), "--", "--ro-bind", str(clean), "/x"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "socket-walk complete: 1 binds checked, 1 walked"
    _plant_socket(clean)
    assert examples_gate.main(["socket-walk", "--allow", str(tmp_path), "--", "--ro-bind", str(clean), "/x"]) == 1
    assert "socket-walk complete" not in capsys.readouterr().out


def test_shared_binds_are_checked_again_but_not_walked_again(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    shared = tmp_path / "shared"
    case = tmp_path / "case"
    case.mkdir()
    _plant_socket(shared)
    argv = ["--ro-bind", str(shared), "/s", "--bind", str(case), "/c"]
    assert examples_gate.main(["socket-walk", "--allow", str(tmp_path), "--shared-count", "3", "--", *argv]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "socket-walk complete: 2 binds checked, 1 walked"
    # Control: the same list with nothing marked shared walks the shared bind and finds its socket.
    assert examples_gate.main(["socket-walk", "--allow", str(tmp_path), "--", *argv]) == 1
    assert f"REFUSE bind {shared}: socket" in capsys.readouterr().out
    # The source check still covers shared binds.
    assert examples_gate.main(["socket-walk", "--shared-count", "3", "--", *argv]) == 1
    assert f"REFUSE bind {shared}: outside the allowed bind roots" in capsys.readouterr().out


def test_a_bind_check_that_exits_zero_early_is_no_verdict(tmp_path: Path) -> None:
    _fixture(tmp_path)
    driver = tmp_path / "driver"
    driver.mkdir()
    shutil.copy2(GATE, driver / "examples-gate.sh")
    helper_text = (GATE.parent / "examples_gate.py").read_text()
    needle = "def cmd_socket_walk(args: argparse.Namespace) -> int:\n"
    assert helper_text.count(needle) == 1
    (driver / "examples_gate.py").write_text(helper_text.replace(needle, needle + "    return 0\n"))
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
    assert "REFUSED: shared sandbox bind check ended without a result (exit 0)" in result.stderr


def test_a_same_filesystem_bind_mount_is_missed_by_device_and_caught_by_the_mount_table(tmp_path: Path) -> None:
    reason = _bwrap_unusable_reason()
    if reason is not None:
        pytest.skip(reason)
    source = tmp_path / "source"
    (source / "sub").mkdir(parents=True)
    (source / "file.txt").write_text("x")
    other = tmp_path / "other"
    other.mkdir()
    (other / "file.txt").write_text("y")
    probe = (
        "import json, sys; from pathlib import Path; import examples_gate as g; "
        "source = sys.argv[1]; "
        "by_device = [f for f in g.sockets_under(Path(source))[0] if f.startswith('mount point')]; "
        "by_table = g.mounts_beneath(source, g.mount_points(Path('/proc/self/mountinfo').read_text())); "
        "print(json.dumps({'by_device': by_device, 'by_table': by_table}))"
    )
    result = subprocess.run(
        [
            shutil.which("bwrap") or "bwrap",
            "--ro-bind",
            "/",
            "/",
            "--ro-bind",
            str(other),
            str(source / "sub"),
            "--ro-bind",
            str(other / "file.txt"),
            str(source / "file.txt"),
            "--",
            sys.executable,
            "-I",
            "-c",
            f"import sys; sys.path.insert(0, {str(GATE.parent)!r}); {probe}",
            str(source),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout)
    # Both mounts are on the same filesystem as the source: the device check sees neither.
    assert seen["by_device"] == []
    assert seen["by_table"] == [str(source / "file.txt"), str(source / "sub")]


# ------------------------------------------------------------- generated inputs

_NONCE = "0123456789abcdef0123456789abcdef"


def _generated_fixture(
    tmp_path: Path, *, source_path: str = "examples/demo/generated.csv", command: str = "./examples/demo/run.sh"
) -> tuple[Path, Path]:
    """The demo case reading an input its own launcher writes: gitignored, absent from the tree."""
    tree, cases = _fixture(tmp_path)
    example = tree / "examples/demo"
    (example / "settings.yaml").write_text(f"source: {{plugin: csv, options: {{path: '{source_path}'}}}}\nsinks: []\n")
    (example / "input.csv").unlink()
    (example / ".gitignore").write_text("generated.csv\nother.csv\nelsewhere.csv\n")
    data = yaml.safe_load(cases.read_text())
    data["cases"][0].update(
        command=command, generated_inputs={"generator": "examples/demo/run.sh", "paths": ["examples/demo/generated.csv"]}
    )
    cases.write_text(yaml.safe_dump(data))
    return tree, cases


def _preflight(tmp_path: Path, tree: Path, cases: Path, *, proof: str | None = f"self-test-ok {_NONCE}", nonce: str = _NONCE) -> int:
    argv = ["preflight", "--cases", str(cases), "--tree", str(tree), "--case", "demo"]
    if proof is not None:
        proof_file = tmp_path / "self-test.out"
        proof_file.write_text(proof + "\n")
        scratch = tmp_path / "scratch"
        scratch.mkdir(exist_ok=True)
        argv += ["--sandbox-proof", str(proof_file), "--nonce", nonce, "--scratch", str(scratch)]
    return examples_gate.main(argv)


def test_generated_input_is_deferred_only_with_this_runs_sandbox_proof(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tree, cases = _generated_fixture(tmp_path)
    assert _validate(tree, cases) == 0
    capsys.readouterr()
    assert _preflight(tmp_path, tree, cases) == 0
    assert "1 generated input(s) inspected after the run" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("mutation", "needle"),
    [
        ("no_proof", "no sandbox self-test proof"),
        ("other_nonce", "no sandbox self-test proof"),
        ("tracked", "exists before the run"),
        ("not_ignored", "is not gitignored"),
        ("no_generator", "generator examples/demo/run.sh is not a tracked file"),
        ("undeclared", "generated.csv missing or unreadable"),
    ],
)
def test_generated_input_refusals_before_the_run(tmp_path: Path, capsys: pytest.CaptureFixture[str], mutation: str, needle: str) -> None:
    tree, cases = _generated_fixture(tmp_path)
    proof = f"self-test-ok {_NONCE}"
    if mutation == "no_proof":
        code = _preflight(tmp_path, tree, cases, proof=None)
    else:
        if mutation == "other_nonce":
            proof = "self-test-ok " + "f" * 32
        elif mutation == "tracked":
            (tree / "examples/demo/generated.csv").write_text("id\n1\n")
        elif mutation == "not_ignored":
            (tree / "examples/demo/.gitignore").write_text("other.csv\n")
        elif mutation == "no_generator":
            (tree / "examples/demo/run.sh").unlink()
        else:
            data = yaml.safe_load(cases.read_text())
            data["cases"][0]["generated_inputs"]["paths"] = ["examples/demo/other.csv"]
            cases.write_text(yaml.safe_dump(data))
        code = _preflight(tmp_path, tree, cases, proof=proof)
    out = capsys.readouterr().out
    assert code == 1, out
    assert needle in out, out
    if mutation in {"no_proof", "other_nonce"}:
        # Without the proof nothing is deferred: the missing input is refused as before.
        assert "generated.csv missing or unreadable" in out, out


@pytest.mark.parametrize(
    ("command", "refused"),
    [
        ("./examples/demo/run.sh", False),
        ("DEMO_INPUT=examples/demo/generated.csv ./examples/demo/run.sh", False),
        ("DEMO_INPUT=examples/demo/elsewhere.csv ./examples/demo/run.sh", True),
    ],
)
def test_path_variable_resolves_from_the_case_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str, refused: bool
) -> None:
    # The command may point the variable at an undeclared, untracked file: absent from the export, it is refused.
    tree, cases = _generated_fixture(tmp_path, source_path="${DEMO_INPUT:-examples/demo/generated.csv}", command=command)
    assert _validate(tree, cases) == 0
    capsys.readouterr()
    code = _preflight(tmp_path, tree, cases)
    out = capsys.readouterr().out
    assert code == (1 if refused else 0), out
    assert ("elsewhere.csv missing or unreadable" in out) is refused, out


def test_resolve_source_path_uses_only_the_case_environment() -> None:
    raw = "${DEMO_INPUT:-examples/demo/generated.csv}"
    assert examples_gate.resolve_source_path(raw, {}) == "examples/demo/generated.csv"
    assert examples_gate.resolve_source_path(raw, {"DEMO_INPUT": "examples/demo/x.csv"}) == "examples/demo/x.csv"
    assert examples_gate.resolve_source_path("${UNSET_NO_DEFAULT}", {}) == "${UNSET_NO_DEFAULT}"
    case = {"id": "c", "command": "A=1 B='two words' ./run.sh C=not-an-assignment", "env": {"A": "env", "D": "4"}}
    assert examples_gate.case_environment(case) == {"A": "1", "B": "two words", "D": "4"}


@pytest.mark.parametrize(
    ("change", "value"),
    [
        ("generator", "examples/demo/other.sh"),
        ("path", "examples/other/generated.csv"),
        ("path", "/abs/generated.csv"),
        ("path", "examples/demo/../demo/generated.csv"),
        ("path", "examples/demo/*.csv"),
        ("path", "examples/demo/generated.txt"),
        ("duplicate", "examples/demo/generated.csv"),
    ],
)
def test_validate_rejects_bad_generated_inputs(tmp_path: Path, change: str, value: str) -> None:
    tree, cases = _generated_fixture(tmp_path)
    data = yaml.safe_load(cases.read_text())
    block = data["cases"][0]["generated_inputs"]
    if change == "generator":
        block["generator"] = value
    elif change == "path":
        block["paths"] = [value]
    else:
        block["paths"] = [value, value]
    cases.write_text(yaml.safe_dump(data))
    assert _validate(tree, cases) == 2


def _inspect(tmp_path: Path, name: str, content: str | None) -> tuple[list[str], list[str]]:
    root = tmp_path / "root"
    target = root / "examples/demo" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if content is not None:
        target.write_text(content)
    return examples_gate.generated_input_failures(
        root, {"generated_inputs": {"generator": "examples/demo/run.sh", "paths": [f"examples/demo/{name}"]}}
    )


@pytest.mark.parametrize(
    ("name", "content", "needle"),
    [
        ("generated.csv", "id,url\n1,https://example.com/a\n", "remote URL host example.com"),
        ("generated.jsonl", '{"parts": [{"source": "s3://bucket/key"}]}\n', "remote URL host bucket"),
        ("generated.json", '{"rows": ["ftp://files.example.org/x"]}', "remote URL host files.example.org"),
        ("generated.csv", "id,path\n1,{outside}\n", "outside the case copy"),
        ("generated.csv", "id,path\n1,file://{outside}\n", "outside the case copy"),
        ("generated.csv", "id,path\n1,../../../outside.txt\n", "outside the case copy"),
        ("generated.jsonl", "{not json\n", "unreadable"),
        ("generated.json", '{"v": "file://localhost/etc/passwd"}', "outside the case copy"),
        ("generated.json", '{"v": "file:/etc/passwd"}', "outside the case copy"),
        ("generated.json", '{"v": "file://elsewhere.example/x"}', "file URI host 'elsewhere.example'"),
        ("generated.json", '{"v": "/outside/name\\nsuffix"}', "outside the case copy"),
        ("generated.json", '{"v": "http://127.0.0.1\\t@outside.example/x"}', "control character after URL"),
        ("generated.csv", None, "not a file inside the case copy"),
    ],
)
def test_generated_input_inspection_fails_remote_or_outside_values(tmp_path: Path, name: str, content: str | None, needle: str) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("host file")
    failures, _ = _inspect(tmp_path, name, None if content is None else content.replace("{outside}", str(outside)))
    assert any(needle in failure for failure in failures), failures


def test_generated_input_inspection_accepts_loopback_and_paths_inside_the_copy(tmp_path: Path) -> None:
    inside = tmp_path / "root/examples/demo/payload.bin"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"x")
    content = f"id,url,path,rel\n1,http://127.0.0.1:8199/x,{inside},examples/demo/payload.bin\n2,,file://{inside},\n"
    failures, evidence = _inspect(tmp_path, "generated.csv", content)
    assert failures == []
    assert evidence and "none remote or outside the copy" in evidence[0]


def test_generated_input_symlinked_out_of_the_copy_fails(tmp_path: Path) -> None:
    outside = tmp_path / "outside.csv"
    outside.write_text("id\n1\n")
    target = tmp_path / "root/examples/demo/generated.csv"
    target.parent.mkdir(parents=True)
    target.symlink_to(outside)
    failures, _ = examples_gate.generated_input_failures(
        tmp_path / "root", {"generated_inputs": {"generator": "examples/demo/run.sh", "paths": ["examples/demo/generated.csv"]}}
    )
    assert any("not a file inside the case copy" in failure for failure in failures), failures


@pytest.mark.parametrize(("row", "verdict"), [("1,http://127.0.0.1:8199/x", "PASS"), ("1,https://example.com/x", "FAIL")])
def test_gate_runs_a_generated_input_case_and_inspects_it_after(tmp_path: Path, row: str, verdict: str) -> None:
    tree, cases = _generated_fixture(tmp_path)
    (tree / "examples/demo/run.sh").write_text(f"#!/bin/sh\nprintf 'id,url\\n{row}\\n' > examples/demo/generated.csv\necho verified\n")
    (tree / "examples/demo/run.sh").chmod(0o755)
    data = yaml.safe_load(cases.read_text())
    data["cases"][0]["audit"] = []
    cases.write_text(yaml.safe_dump(data))
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake = fake_bin / "bwrap"
    fake.write_text(
        '#!/bin/bash\nwhile [ "$1" != "--" ]; do shift; done\nshift\n'
        'if [ "$1" = bash ]; then printf \'print("mount-check-ok")\\n\' > "${12}"; exec "$@"; fi\n' + _FORGED_SELF_TEST
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
    summary = (log_dir / "summary.txt").read_text()
    # The gate handed preflight this run's self-test proof (else NOTRUN), and check read the written input.
    assert f"{verdict} demo" in summary, (summary, result.stderr)
    expected = "0" if verdict == "PASS" else "1"
    assert result.returncode == int(expected), (result.stdout, result.stderr)
    assert (log_dir / "overall.exit").read_text().strip() == expected
    if verdict == "FAIL":
        assert "remote URL host example.com" in summary, summary


def test_generated_input_relative_path_through_a_link_out_of_the_copy_fails(tmp_path: Path) -> None:
    # The launcher may make a link inside the copy after preflight; a relative value through it is resolved.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("host file")
    root = tmp_path / "root"
    (root / "examples/demo").mkdir(parents=True)
    (root / "examples/demo/link").symlink_to(outside)
    failures, _ = _inspect(tmp_path, "generated.csv", "id,path\n1,examples/demo/link/secret.txt\n")
    assert any("outside the case copy" in failure for failure in failures), failures
    (root / "examples/demo/link").unlink()
    (root / "examples/demo/link").mkdir()
    failures, _ = _inspect(tmp_path, "generated.csv", "id,path\n1,examples/demo/link/secret.txt\n")
    assert failures == []


def test_generated_input_case_command_is_one_launcher(tmp_path: Path) -> None:
    tree, cases = _generated_fixture(
        tmp_path,
        command="DEMO_INPUT=examples/demo/generated.csv ./examples/demo/run.sh && DEMO_INPUT=examples/demo/other.csv ./examples/demo/run.sh",
    )
    assert _validate(tree, cases) == 2


def test_path_variable_in_a_compound_command_is_not_resolved(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tree, cases = _fixture(tmp_path)
    (tree / "examples/demo/settings.yaml").write_text(
        "source: {plugin: csv, options: {path: '${DEMO_INPUT:-examples/demo/input.csv}'}}\nsinks: []\n"
    )
    data = yaml.safe_load(cases.read_text())
    for command, refused in [
        ("./examples/demo/run.sh", False),
        ("./examples/demo/run.sh && DEMO_INPUT=elsewhere.csv ./examples/demo/run.sh", True),
    ]:
        data["cases"][0]["command"] = command
        cases.write_text(yaml.safe_dump(data))
        code = examples_gate.main(["preflight", "--cases", str(cases), "--tree", str(tree), "--case", "demo"])
        out = capsys.readouterr().out
        assert code == (1 if refused else 0), out
        assert ("path variable in a case command with shell syntax" in out) is refused, out


def test_a_tracked_generator_matching_an_ignore_rule_is_accepted(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tree, cases = _generated_fixture(tmp_path)
    (tree / "examples/demo/.gitignore").write_text("generated.csv\n*.sh\n")
    assert _preflight(tmp_path, tree, cases) == 0, capsys.readouterr().out


# --------------------------------------------------------- round-3 review fixes


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("/etc", "not one of the listed /etc entries"),
        ("/etc/shadow", "not one of the listed /etc entries"),
        ("/etc/ssl", "not one of the listed /etc entries"),
        ("/etc/hosts", None),
        ("/etc/ssl/../hosts", None),
    ],
)
def test_only_the_listed_etc_entries_may_be_bound(source: str, reason: str | None) -> None:
    assert examples_gate.bind_source_refusal(source, []) == reason


def test_socket_walk_refuses_etc_whole_before_walking(capsys: pytest.CaptureFixture[str]) -> None:
    assert examples_gate.main(["socket-walk", "--", "--ro-bind", "/etc", "/etc"]) == 1
    assert "REFUSE bind /etc: not one of the listed /etc entries" in capsys.readouterr().out


def test_socket_walk_refuses_a_fifo_under_a_bind(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bound = tmp_path / "bound"
    (bound / "deep").mkdir(parents=True)
    assert examples_gate.main(["socket-walk", "--allow", str(tmp_path), "--", "--bind", str(bound), "/case"]) == 0
    capsys.readouterr()
    os.mkfifo(bound / "deep" / "pipe")
    assert examples_gate.main(["socket-walk", "--allow", str(tmp_path), "--", "--bind", str(bound), "/case"]) == 1
    assert f"REFUSE bind {bound}: fifo {bound / 'deep' / 'pipe'}" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("case_id", "accepted"), [(".", False), ("..", False), (".hidden", False), ("outside", False), ("self-test", False), ("a.b_c-1", True)]
)
def test_case_ids_exclude_dot_names_and_the_gates_own_directories(tmp_path: Path, case_id: str, accepted: bool) -> None:
    tree, cases = _fixture(tmp_path)
    data = yaml.safe_load(cases.read_text())
    data["cases"][0]["id"] = case_id
    data["examples"]["demo"]["configs"]["settings.yaml"]["case"] = case_id
    data["examples"]["demo"]["launchers"]["run.sh"]["case"] = case_id
    cases.write_text(yaml.safe_dump(data))
    assert _validate(tree, cases) == (0 if accepted else 2)


@pytest.mark.parametrize(("home_ro", "accepted"), [(".aws", False), (".config/app", False), (".cache", False), (".cache/model", True)])
def test_home_ro_is_limited_to_directories_under_the_home_cache(tmp_path: Path, home_ro: str, accepted: bool) -> None:
    tree, cases = _fixture(tmp_path)
    data = yaml.safe_load(cases.read_text())
    data["cases"][0]["requires"] = {"home_ro": [home_ro]}
    cases.write_text(yaml.safe_dump(data))
    assert _validate(tree, cases) == (0 if accepted else 2)


def test_a_home_cache_directory_linked_out_of_the_cache_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # The gate allows the physical ~/.cache, not the bound directory itself, so a link out of it is judged.
    cache = tmp_path / "home" / ".cache"
    (cache / "model").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (cache / "linked").symlink_to(elsewhere)
    assert examples_gate.main(["socket-walk", "--allow", str(cache), "--", "--ro-bind", str(cache / "model"), "/h/model"]) == 0
    capsys.readouterr()
    assert examples_gate.main(["socket-walk", "--allow", str(cache), "--", "--ro-bind", str(cache / "linked"), "/h/model"]) == 1
    assert f"REFUSE bind {cache / 'linked'}: resolves to {elsewhere}, outside the allowed bind roots" in capsys.readouterr().out


def _gate_variant(tmp_path: Path, old: str, new: str) -> Path:
    driver = tmp_path / "driver"
    driver.mkdir()
    gate_text = GATE.read_text()
    assert gate_text.count(old) == 1, old
    (driver / "examples-gate.sh").write_text(gate_text.replace(old, new))
    shutil.copy2(GATE.parent / "examples_gate.py", driver / "examples_gate.py")
    return driver / "examples-gate.sh"


def _throwaway_venv(tmp_path: Path) -> Path:
    # Stands in for the venv in tests that make its bind writable: the shared venv is never bound writable.
    # Outside the sandbox the gate's helper imports the shared venv's packages through a .pth, read only.
    venv = tmp_path / "throwaway-venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    shared = sysconfig.get_paths()["purelib"]
    (venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages" / "shared.pth").write_text(shared + "\n")
    # The gate's preflight requires these console scripts; the run is refused at the self-test before any is used.
    for name in ("elspeth", "chaosllm", "chaosweb"):
        (venv / "bin" / name).write_text("#!/bin/sh\nexit 1\n")
        (venv / "bin" / name).chmod(0o755)
    return venv.resolve()


@pytest.mark.parametrize("witness", ["mount table", "write probe"])
def test_a_writable_venv_bind_fails_the_self_test_when_bwrap_works(tmp_path: Path, witness: str) -> None:
    # The mount check names the writable venv first; with it removed, the write probe still refuses and
    # removes the file it made. Both run against a throwaway venv.
    reason = _bwrap_unusable_reason()
    if reason is not None:
        pytest.skip(reason)
    (tmp_path / "checkout").mkdir()
    venv = _throwaway_venv(tmp_path)
    gate = _gate_variant(tmp_path, '--ro-bind "$VENV" "$VENV"', '--bind "$VENV" "$VENV"')
    if witness == "write probe":
        text = gate.read_text()
        check = 'assert checked.returncode == 0 and checked.stdout.strip() == "mount-check-ok", (checked.stdout, checked.stderr)\n'
        assert text.count(check) == 1
        gate.write_text(text.replace(check, ""))
    result, log_dir = _gate_with_an_open_descriptor(tmp_path, gate, "echo descriptors-ok", venv=venv)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "bwrap sandbox self-test failed" in result.stderr
    self_test = (log_dir / "self-test.out").read_text()
    if witness == "mount table":
        assert f"writable mount outside the writable set: {venv}" in self_test, self_test
    else:
        assert f"write {venv}/examples-gate-probe succeeded" in self_test, self_test
    assert not (venv / "examples-gate-probe").exists()
    assert not (Path(sys.executable).parent.parent / "examples-gate-probe").exists()


@pytest.mark.parametrize("unshare_ipc", [True, False])
def test_a_host_sysv_segment_is_not_visible_inside_when_bwrap_works(tmp_path: Path, unshare_ipc: bool) -> None:
    # The same run with and without --unshare-ipc while this test holds a SysV segment on the host.
    reason = _bwrap_unusable_reason()
    if reason is not None:
        pytest.skip(reason)
    (tmp_path / "checkout").mkdir()
    if shutil.which("ipcmk") is None or shutil.which("ipcrm") is None:
        pytest.skip("ipcmk/ipcrm not installed")
    gate = GATE if unshare_ipc else _gate_variant(tmp_path, "--unshare-ipc ", "")
    made = subprocess.run(["ipcmk", "-M", "4096"], capture_output=True, text=True, check=True)
    shmid = made.stdout.split()[-1]
    try:
        result, log_dir = _gate_with_an_open_descriptor(tmp_path, gate, "echo descriptors-ok")
    finally:
        subprocess.run(["ipcrm", "-m", shmid], check=True)
    if unshare_ipc:
        assert result.returncode == 0, (result.stdout, result.stderr, (log_dir / "preflight.txt").read_text())
        assert "PASS demo" in (log_dir / "summary.txt").read_text()
    else:
        assert result.returncode == 2, (result.stdout, result.stderr)
        assert "('shm', [" in (log_dir / "self-test.out").read_text()


@pytest.mark.parametrize(
    ("variant", "where"),
    [
        (None, None),
        ("shared", "self-test"),
        ("case", "case"),
    ],
)
def test_every_mount_is_read_only_except_the_writable_set_when_bwrap_works(tmp_path: Path, variant: str | None, where: str | None) -> None:
    # Control: the real gate passes. "shared": the self-test's own probe bind made writable is refused.
    # "case": the case's own directory left out of its writable set is refused by the case's own check.
    reason = _bwrap_unusable_reason()
    if reason is not None:
        pytest.skip(reason)
    (tmp_path / "checkout").mkdir()
    if variant is None:
        gate = GATE
    elif variant == "shared":
        gate = _gate_variant(
            tmp_path, '--ro-bind "$SELF_TEST_DIR/ro" "$SELF_TEST_DIR/ro"', '--bind "$SELF_TEST_DIR/ro" "$SELF_TEST_DIR/ro"'
        )
    else:
        gate = _gate_variant(tmp_path, '"${WRITABLE_MOUNTS[@]}" "$case_dir" > "$LOG_DIR', '"${WRITABLE_MOUNTS[@]}" > "$LOG_DIR')
    result, log_dir = _gate_with_an_open_descriptor(tmp_path, gate, "echo descriptors-ok")
    if variant is None:
        assert result.returncode == 0, (result.stdout, result.stderr, (log_dir / "preflight.txt").read_text())
        assert "PASS demo" in (log_dir / "summary.txt").read_text()
        return
    assert result.returncode == 2, (result.stdout, result.stderr)
    if where == "self-test":
        assert "bwrap sandbox self-test failed" in result.stderr
        self_test = (log_dir / "self-test.out").read_text()
        assert "writable mount outside the writable set:" in self_test and "/self-test/ro" in self_test, self_test
    else:
        assert "case demo sandbox mount check failed: writable mount outside the writable set:" in result.stderr
        assert "/demo" in result.stderr.split("mount check failed:", 1)[1], result.stderr


def test_home_ro_is_bound_read_only_from_the_physical_cache_and_a_link_out_refuses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".cache" / "model").mkdir(parents=True)
    outside = tmp_path / "outside-cache"
    outside.mkdir()
    (home / ".cache" / "linked").symlink_to(outside)
    case_flags = tmp_path / "case-flags"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake = fake_bin / "bwrap"
    fake.write_text(
        "#!/bin/bash\n"
        "flags=()\n"
        'while [ "$1" != "--" ]; do flags+=("$1"); shift; done\n'
        "shift\n"
        'if [ "$1" = bash ]; then\n'
        f"  printf '%s\\n' \"${{flags[@]}}\" > {shlex.quote(str(case_flags))}\n"
        '  printf \'print("mount-check-ok")\\n\' > "${12}"\n'
        '  exec "$@"\n'
        "fi\n" + _FORGED_SELF_TEST
    )
    fake.chmod(0o755)
    _, cases = _fixture(tmp_path)
    data = yaml.safe_load(cases.read_text())
    data["cases"][0].update(command="echo verified", audit=[], requires={"home_ro": [".cache/model"]})
    cases.write_text(yaml.safe_dump(data))
    env = {"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}", "HOME": str(home)}
    options = ["--execute", "--only", "demo", "--tmp-root", str(tmp_path / "work"), "--min-mem-gb", "0"]
    result = _dry_run(tmp_path, *options, "--log-dir", str(tmp_path / "logs"), env=env)
    assert result.returncode == 0, (result.stdout, result.stderr)
    flags = case_flags.read_text().splitlines()
    triples = list(zip(flags, flags[1:], flags[2:], strict=False))
    assert any(
        option == "--ro-bind" and source == str((home / ".cache" / "model").resolve()) and target.endswith("/home/.cache/model")
        for option, source, target in triples
    ), flags
    data["cases"][0]["requires"] = {"home_ro": [".cache/linked"]}
    cases.write_text(yaml.safe_dump(data))
    result = _dry_run(tmp_path, *options, "--log-dir", str(tmp_path / "logs-linked"), env=env)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert f"resolves to {outside.resolve()}, outside the allowed bind roots" in result.stderr


def test_a_file_linked_out_of_an_allowed_root_is_refused(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    (cache / "model").symlink_to(secret)
    assert examples_gate.bind_source_refusal(str(cache / "model"), [str(cache)]) == f"resolves to {secret}, outside the allowed bind roots"
    (cache / "plain.txt").write_text("x")
    assert examples_gate.bind_source_refusal(str(cache / "plain.txt"), [str(cache)]) is None


@pytest.mark.parametrize(
    ("home_ro", "accepted"), [("./.cache/model", False), (".cache//model", False), (".cache/model/", False), (".cache/model", True)]
)
def test_home_ro_must_be_in_normal_form(tmp_path: Path, home_ro: str, accepted: bool) -> None:
    tree, cases = _fixture(tmp_path)
    data = yaml.safe_load(cases.read_text())
    data["cases"][0]["requires"] = {"home_ro": [home_ro]}
    cases.write_text(yaml.safe_dump(data))
    assert _validate(tree, cases) == (0 if accepted else 2)


def test_a_fifo_as_the_bind_source_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    os.mkfifo(tmp_path / "pipe")
    assert examples_gate.main(["socket-walk", "--allow", str(tmp_path), "--", "--ro-bind", str(tmp_path / "pipe"), "/p"]) == 1
    assert f"REFUSE bind {tmp_path / 'pipe'}: fifo {tmp_path / 'pipe'}" in capsys.readouterr().out


def test_an_etc_directory_entry_that_resolves_elsewhere_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    real_realpath = os.path.realpath
    monkeypatch.setattr(
        examples_gate.os.path, "realpath", lambda path: "/opt/elsewhere" if path == "/etc/alternatives" else real_realpath(path)
    )
    monkeypatch.setattr(examples_gate.os.path, "isdir", lambda path: path == "/opt/elsewhere")
    assert examples_gate.bind_source_refusal("/etc/alternatives", []) == "resolves to /opt/elsewhere, outside the listed /etc entry"
