"""The sharded suite preserves selection and refuses incomplete coverage evidence."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import coverage
import pytest
import yaml
from scripts.ci_test_shards import (
    ASSIGNMENT_ALGORITHM,
    DURATION_PROFILE_PATH,
    LONG_RUN_CASES,
    _publish_manifest,
    build_long_run_manifest,
    collection_digest,
    combine_shards,
    evidence_identity,
    finalize_long_run_manifest,
    load_duration_profile,
    long_run_call_result,
    ordinary_assignment,
    select_long_run_nodeids,
    shard_index,
    validate_long_run_shards,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _local_producer_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Parent CI/xdist identity must not leak into these synthetic producers.
    for name in ("GITHUB_ACTIONS", "GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB"):
        monkeypatch.delenv(name, raising=False)


def _init_checkout(checkout: Path) -> str:
    profile = checkout / DURATION_PROFILE_PATH
    profile.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO_ROOT / DURATION_PROFILE_PATH, profile)
    subprocess.run(["git", "init", str(checkout)], capture_output=True, text=True, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Shard Test", "-c", "user.email=shard@example.invalid", "commit", "--allow-empty", "-m", "fixture"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=True,
    )
    commit = evidence_identity(checkout)["tested_commit"]
    assert commit is not None
    return commit


def test_shards_are_stable_disjoint_and_complete() -> None:
    nodeids = [f"tests/example.py::test_example[{index}]" for index in range(1000)]
    groups = [{nodeid for nodeid in nodeids if shard_index(nodeid, 4) == index} for index in range(4)]
    assert all(groups)
    assert set.union(*groups) == set(nodeids)
    assert sum(map(len, groups)) == len(nodeids)
    assert shard_index("tests/example.py::test_example[0]", 4) == 3
    assert [shard_index(nodeid, 4) for nodeid in reversed(nodeids)] == list(reversed([shard_index(nodeid, 4) for nodeid in nodeids]))


def test_long_run_catalog_names_the_three_existing_cases() -> None:
    prefix = "tests/unit/architecture/test_web_landscape_mutation_fencing.py::"
    expected = {
        "no-escape": prefix + "test_no_mutation_alias_wrapper_dynamic_or_raw_write_escape_exists",
        "reserve-actual-sources": prefix + "test_required_work_reserve_admission_on_actual_sources",
        "protected-formal-recovery": prefix + "test_required_work_reserve_rejects_protected_instance_formal_recovery",
    }
    assert expected == LONG_RUN_CASES
    module = ast.parse((REPO_ROOT / prefix.split("::")[0]).read_text(encoding="utf-8"))
    definitions = {node.name for node in module.body if isinstance(node, ast.FunctionDef)}
    assert all(nodeid.split("::")[1] in definitions for nodeid in LONG_RUN_CASES.values())


@pytest.mark.parametrize("reverse", [False, True])
def test_long_run_selector_preserves_complete_disjoint_union_and_full_digest(reverse: bool) -> None:
    ordinary = [f"test_sample.py::test_ordinary[{index}]" for index in range(100)]
    nodeids = ordinary + list(LONG_RUN_CASES.values())
    if reverse:
        nodeids.reverse()
    original = nodeids.copy()
    digest = collection_digest(nodeids)
    marked = list(reversed(LONG_RUN_CASES.values()))
    shards = [select_long_run_nodeids(nodeids, marked, "ordinary", None, 4, index) for index in range(4)]
    assignment = ordinary_assignment(ordinary, 4, load_duration_profile(REPO_ROOT))
    assert all(shards)
    for index, selected in enumerate(shards):
        assert selected == [nodeid for nodeid in nodeids if nodeid in ordinary and assignment[nodeid] == index]
    proofs = [select_long_run_nodeids(nodeids, marked, "proof", case, 1, 0) for case in LONG_RUN_CASES]
    assert proofs == [[nodeid] for nodeid in LONG_RUN_CASES.values()]
    combined = [nodeid for selected in shards + proofs for nodeid in selected]
    assert len(combined) == len(set(combined)) == len(nodeids)
    assert set(combined) == set(nodeids)
    assert collection_digest(combined) == digest
    assert collection_digest([nodeid for selected in shards for nodeid in selected]) != digest
    assert nodeids == original
    assert marked == list(reversed(LONG_RUN_CASES.values()))


@pytest.mark.parametrize(
    "partition,case,count,index",
    [
        ("all", None, 4, 0),
        ("unknown", None, 4, 0),
        ("ordinary", "no-escape", 4, 0),
        ("ordinary", "unknown", 4, 0),
        ("proof", None, 1, 0),
        ("proof", "unknown", 1, 0),
        ("proof", "no-escape", 4, 0),
        ("proof", "no-escape", 2, 1),
        ("ordinary", None, 0, 0),
        ("ordinary", None, -1, 0),
        ("ordinary", None, 4, -1),
        ("ordinary", None, 4, 4),
        ("ordinary", None, True, 0),
        ("ordinary", None, 4.0, 0),
        ("ordinary", None, "4", 0),
        ("ordinary", None, None, 0),
        ("ordinary", None, 4, False),
        ("ordinary", None, 4, 0.0),
        ("ordinary", None, 4, "0"),
        ("ordinary", None, 4, None),
        ("proof", "no-escape", True, 0),
        ("proof", "no-escape", 1, False),
        ("proof", "no-escape", 1.0, 0),
        ("proof", "no-escape", 1, 0.0),
    ],
)
def test_long_run_selector_rejects_invalid_selection(partition: str, case: str | None, count: int, index: int) -> None:
    marked = list(LONG_RUN_CASES.values())
    nodeids = ["test_sample.py::test_ordinary", *marked]
    assert select_long_run_nodeids(nodeids, marked, "ordinary", None, 1, 0) == nodeids[:1]
    assert select_long_run_nodeids(nodeids, marked, "proof", "no-escape", 1, 0) == [LONG_RUN_CASES["no-escape"]]
    with pytest.raises(ValueError):
        select_long_run_nodeids(nodeids, marked, partition, case, count, index)


@pytest.mark.parametrize("partition,case", [("ordinary", None), *(("proof", case) for case in LONG_RUN_CASES)])
@pytest.mark.parametrize(
    "mutation",
    [
        "missing_no_escape",
        "missing_reserve",
        "missing_recovery",
        "duplicate_ordinary",
        "duplicate_case",
        "missing_marker",
        "extra_marker",
        "duplicate_marker",
        "misflagged",
        "deselected_case",
    ],
)
def test_long_run_selector_rejects_ambiguous_collection_or_marking(partition: str, case: str | None, mutation: str) -> None:
    marked = list(LONG_RUN_CASES.values())
    nodeids = ["test_sample.py::test_ordinary", *marked]
    expected = [LONG_RUN_CASES[case]] if case is not None else nodeids[:1]
    assert select_long_run_nodeids(nodeids, marked, partition, case, 1, 0) == expected
    missing = {"missing_no_escape": 0, "missing_reserve": 1, "missing_recovery": 2}
    if mutation in missing:
        nodeids.remove(marked[missing[mutation]])
    elif mutation == "duplicate_ordinary":
        nodeids.append(nodeids[0])
    elif mutation == "duplicate_case":
        nodeids.append(marked[0])
    elif mutation == "missing_marker":
        marked.pop()
    elif mutation == "extra_marker":
        marked.append(nodeids[0])
    elif mutation == "duplicate_marker":
        marked.append(marked[0])
    elif mutation == "misflagged":
        marked[0] = nodeids[0]
    else:
        nodeids.remove(marked.pop())
    with pytest.raises(ValueError):
        select_long_run_nodeids(nodeids, marked, partition, case, 1, 0)


@pytest.mark.parametrize("case", list(LONG_RUN_CASES))
@pytest.mark.parametrize("sequence", ["wrong_right", "right_right", "failed_passed", "right_wrong"])
def test_long_run_call_refusal_cannot_be_overwritten(tmp_path: Path, case: str, sequence: str) -> None:
    nodeid = LONG_RUN_CASES[case]
    assert long_run_call_result(case, nodeid, "call", True, False, None) is True
    first_node = "test_wrong.py::test_wrong" if sequence == "wrong_right" else nodeid
    first = long_run_call_result(case, first_node, "call", sequence != "failed_passed", False, None)
    expected_first = sequence not in {"wrong_right", "failed_passed"}
    assert first is expected_first
    assert long_run_call_result(case, nodeid, "setup", False, False, first) is first
    assert long_run_call_result(case, nodeid, "teardown", True, False, first) is first
    second_node = "test_wrong.py::test_wrong" if sequence == "right_wrong" else nodeid
    refused = long_run_call_result(case, second_node, "call", True, False, first)
    assert refused is False
    assert long_run_call_result(case, nodeid, "call", True, False, refused) is False
    _init_checkout(tmp_path)
    marked = list(LONG_RUN_CASES.values())
    manifest = build_long_run_manifest(tmp_path, marked, marked, "proof", case, 1, 0)
    assert finalize_long_run_manifest(manifest, 0, refused)["exit_status"] == 1


@pytest.mark.parametrize("previous", [None, False, True])
@pytest.mark.parametrize("phase", ["setup", "teardown"])
def test_long_run_noncall_reports_preserve_state(previous: bool | None, phase: str) -> None:
    assert long_run_call_result("no-escape", "unrelated", phase, False, True, previous) is previous


@pytest.mark.parametrize("field", ["passed", "xfail_marked", "previous"])
@pytest.mark.parametrize("value", [0, 1, 1.0, "true"])
def test_long_run_call_admission_requires_literal_booleans(field: str, value: object) -> None:
    arguments = {"passed": True, "xfail_marked": False, "previous": None}
    assert long_run_call_result("no-escape", LONG_RUN_CASES["no-escape"], "call", **arguments) is True
    arguments[field] = value
    with pytest.raises(ValueError):
        long_run_call_result("no-escape", LONG_RUN_CASES["no-escape"], "call", **arguments)


@pytest.mark.parametrize("case,phase", [("unknown", "call"), ("no-escape", "unknown")])
def test_long_run_call_admission_rejects_unknown_identity(case: str, phase: str) -> None:
    with pytest.raises(ValueError):
        long_run_call_result(case, LONG_RUN_CASES["no-escape"], phase, True, False, None)


@pytest.mark.parametrize("partition,case", [("ordinary", None), *(("proof", case) for case in LONG_RUN_CASES)])
@pytest.mark.parametrize("mutation", [None, "wrong_role", "missing_role", "wrong_sha"])
def test_long_run_manifest_builder_binds_actual_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, partition: str, case: str | None, mutation: str | None
) -> None:
    commit = _init_checkout(tmp_path)
    role = "test" if partition == "ordinary" else "proof"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_SHA", commit)
    monkeypatch.setenv("GITHUB_RUN_ID", "1234")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("GITHUB_JOB", role)
    marked = list(LONG_RUN_CASES.values())
    nodeids = ["test_sample.py::test_ordinary", *marked]
    manifest = build_long_run_manifest(tmp_path, nodeids, marked, partition, case, 1, 0)
    assert manifest == {
        "schema_version": 4,
        "assignment_algorithm": ASSIGNMENT_ALGORITHM,
        "duration_profile_sha256": load_duration_profile(tmp_path).sha256,
        "tested_commit": commit,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "github_run_id": "1234",
        "github_run_attempt": "2",
        "producer_job": role,
        "partition": partition,
        "proof_case": case,
        "shard_count": 1,
        "shard_index": 0,
        "collection_digest": collection_digest(nodeids),
        "selected_nodeids": [LONG_RUN_CASES[case]] if case is not None else nodeids[:1],
        "exit_status": None,
    }
    if mutation is None:
        return
    if mutation == "wrong_role":
        monkeypatch.setenv("GITHUB_JOB", "proof" if role == "test" else "test")
    elif mutation == "missing_role":
        monkeypatch.delenv("GITHUB_JOB")
    else:
        monkeypatch.setenv("GITHUB_SHA", "0" * 40)
    with pytest.raises(ValueError):
        build_long_run_manifest(tmp_path, nodeids, marked, partition, case, 1, 0)


@pytest.mark.parametrize("call", [None, False, True])
@pytest.mark.parametrize("exit_status", [0, 1, 5])
def test_long_run_finalizer_copies_truthful_terminal_evidence(tmp_path: Path, call: bool | None, exit_status: int) -> None:
    _init_checkout(tmp_path)
    marked = list(LONG_RUN_CASES.values())
    manifest = build_long_run_manifest(tmp_path, marked, marked, "proof", "no-escape", 1, 0)
    original = copy.deepcopy(manifest)
    terminal = finalize_long_run_manifest(manifest, exit_status, call)
    assert manifest == original
    assert terminal is not manifest
    assert terminal["exit_status"] == (1 if exit_status == 0 and call is not True else exit_status)
    assert terminal["proof_call_passed"] is (call is True)
    assert "coverage_sha256" not in terminal
    with pytest.raises(ValueError):
        finalize_long_run_manifest(terminal, exit_status, call)


@pytest.mark.parametrize(
    "field,value", [("exit", False), ("exit", 0.0), ("exit", -1), ("exit", pytest.ExitCode.OK), ("call", 1), ("call", 1.0)]
)
def test_long_run_finalizer_refuses_numeric_coercion(tmp_path: Path, field: str, value: object) -> None:
    _init_checkout(tmp_path)
    marked = list(LONG_RUN_CASES.values())
    manifest = build_long_run_manifest(tmp_path, marked, marked, "proof", "no-escape", 1, 0)
    assert finalize_long_run_manifest(manifest, 0, True)["exit_status"] == 0
    with pytest.raises(ValueError):
        finalize_long_run_manifest(manifest, value if field == "exit" else 0, value if field == "call" else True)


def test_long_run_ordinary_finalizer_and_missing_coverage(tmp_path: Path) -> None:
    _init_checkout(tmp_path)
    marked = list(LONG_RUN_CASES.values())
    manifest = build_long_run_manifest(tmp_path, ["ordinary", *marked], marked, "ordinary", None, 1, 0)
    terminal = finalize_long_run_manifest(manifest, 0, None)
    assert terminal["exit_status"] == 0
    assert "proof_call_passed" not in terminal
    with pytest.raises(ValueError):
        finalize_long_run_manifest(manifest, 0, True)
    with pytest.raises(FileNotFoundError):
        finalize_long_run_manifest(manifest, 0, None, tmp_path / "missing-database")
    assert manifest["exit_status"] is None


@pytest.mark.parametrize(
    "case,outcome",
    [("ordinary", "pass"), ("ordinary", "fail")]
    + [(case, outcome) for case in LONG_RUN_CASES for outcome in ("pass", "fail", "skip", "setup_skip", "xfail", "xpass", "no_call")],
)
def test_long_run_producer_with_real_pytest_cov(tmp_path: Path, case: str, outcome: str) -> None:
    commit = _init_checkout(tmp_path)
    module = tmp_path / "covered.py"
    module.write_text("def double(value):\n    return value * 2\n", encoding="utf-8")
    partition = "ordinary" if case == "ordinary" else "proof"
    selected_case = None if partition == "ordinary" else case
    body = {
        "pass": "assert double(2) == 4",
        "fail": "assert double(2) == 5",
        "skip": "double(2); pytest.skip('call skip control')",
        "setup_skip": "assert double(2) == 4",
        "xfail": "double(2); pytest.xfail('call xfail control')",
        "xpass": "assert double(2) == 4",
        "no_call": "assert double(2) == 4",
    }[outcome]
    module_source = "import pytest\nfrom covered import double\n"
    for key, nodeid in LONG_RUN_CASES.items():
        decorator = "@pytest.mark.fixture_long_run\n"
        if key == selected_case and outcome == "setup_skip":
            decorator += "@pytest.mark.skip(reason='setup skip control')\n"
        if key == selected_case and outcome == "xpass":
            decorator += "@pytest.mark.xfail(reason='xpass control', strict=False)\n"
        case_body = body if key == selected_case else "assert True"
        module_source += f"{decorator}def {nodeid.split('::')[1]}(): {case_body}\n"
    module_source += f"def test_ordinary(): {body if partition == 'ordinary' else 'assert True'}\n"
    tests = tmp_path / LONG_RUN_CASES["no-escape"].split("::")[0]
    tests.parent.mkdir(parents=True)
    tests.write_text(module_source, encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    fixture_long_run: tiny producer fixture\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text(
        "import json\nimport os\nfrom pathlib import Path\nimport pytest\n"
        "from scripts.ci_test_shards import build_long_run_manifest, long_run_call_result, finalize_long_run_manifest, _publish_manifest\n"
        f"PARTITION = {partition!r}\nCASE = {selected_case!r}\nNO_CALL = {outcome == 'no_call'!r}\n"
        "state = None\nmanifest = None\n"
        "def pytest_collection_modifyitems(items):\n"
        "    global manifest\n"
        "    nodeids = [item.nodeid for item in items]\n"
        "    marked = [item.nodeid for item in items if list(item.iter_markers(name='fixture_long_run'))]\n"
        "    manifest = build_long_run_manifest(Path.cwd(), nodeids, marked, PARTITION, CASE, 1, 0)\n"
        "    _publish_manifest(Path('producer.json'), manifest)\n"
        "    items[:] = [] if NO_CALL else [item for item in items if item.nodeid in manifest['selected_nodeids']]\n"
        "@pytest.hookimpl(wrapper=True)\n"
        "def pytest_runtest_makereport(item, call):\n"
        "    global state\n"
        "    report = yield\n"
        "    if PARTITION == 'proof':\n"
        "        state = long_run_call_result(CASE, item.nodeid, report.when, report.passed, bool(list(item.iter_markers(name='xfail'))), state)\n"
        "    return report\n"
        "@pytest.hookimpl(hookwrapper=True, tryfirst=True)\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    yield\n"
        "    terminal = finalize_long_run_manifest(manifest, int(exitstatus), state, Path(os.environ['COVERAGE_FILE']))\n"
        "    session.exitstatus = terminal['exit_status']\n"
        "    _publish_manifest(Path('producer.json'), terminal)\n",
        encoding="utf-8",
    )
    database = tmp_path / "coverage-data"
    environment = os.environ.copy()
    for name in ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID"):
        environment.pop(name, None)
    environment.update(
        PYTHONPATH=str(REPO_ROOT) + os.pathsep + environment.get("PYTHONPATH", ""),
        COVERAGE_FILE=str(database),
        GITHUB_ACTIONS="true",
        GITHUB_SHA=commit,
        GITHUB_RUN_ID="1234",
        GITHUB_RUN_ATTEMPT="2",
        GITHUB_JOB="test" if partition == "ordinary" else "proof",
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-n", "0", "--cov=covered", "--cov-report=", "--cov-fail-under=0", "-q"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    (tmp_path / "producer.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    (tmp_path / "producer.exit").write_text(f"{result.returncode}\n", encoding="utf-8")
    expected_exit = 0 if outcome == "pass" else (5 if outcome == "no_call" else 1)
    assert result.returncode == expected_exit, result.stdout + result.stderr
    manifest = json.loads((tmp_path / "producer.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 4
    assert manifest["proof_case"] == selected_case
    assert manifest["producer_job"] == environment["GITHUB_JOB"]
    assert manifest["tested_commit"] == commit
    assert manifest["exit_status"] == expected_exit
    assert manifest["coverage_sha256"] == hashlib.sha256(database.read_bytes()).hexdigest()
    if partition == "proof":
        assert manifest["selected_nodeids"] == [LONG_RUN_CASES[case]]
        assert manifest["proof_call_passed"] is (outcome == "pass")
    else:
        assert "proof_call_passed" not in manifest
    data = coverage.CoverageData(basename=str(database))
    data.read()
    assert data.lines(str(module)) == ([1] if outcome in {"setup_skip", "no_call"} else [1, 2])
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("workers", ["0", "2"])
def test_plugin_preserves_marker_selection_and_reports_final_status(tmp_path: Path, workers: str) -> None:
    (tmp_path / "test_sample.py").write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('value', range(40))\n"
        "def test_selected(value): assert value >= 0\n"
        "@pytest.mark.protected\n"
        "def test_protected(): raise AssertionError('protected test selected')\n",
        encoding="utf-8",
    )
    selected: list[str] = []
    environment = os.environ.copy()
    for name in ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID"):
        environment.pop(name, None)
    for index in range(4):
        report = tmp_path / f"report-{index}.json"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(tmp_path / "test_sample.py"),
                "-c",
                "/dev/null",
                "--rootdir",
                str(tmp_path),
                "-p",
                "scripts.ci_test_shards",
                "-m",
                "not protected",
                "-n",
                workers,
                "--ci-shard-count",
                "4",
                "--ci-shard-index",
                str(index),
                "--ci-shard-report",
                str(report),
                "-q",
            ],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=90,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        manifest = json.loads(report.read_text(encoding="utf-8"))
        assert manifest["schema_version"] == 2
        assert manifest["tested_commit"] == evidence_identity(REPO_ROOT)["tested_commit"]
        assert manifest["python_version"] == f"{sys.version_info.major}.{sys.version_info.minor}"
        assert manifest["github_run_id"] is manifest["github_run_attempt"] is manifest["producer_job"] is None
        assert manifest["exit_status"] == 0
        assert all("test_protected" not in nodeid for nodeid in manifest["selected_nodeids"])
        selected.extend(manifest["selected_nodeids"])
    assert len(selected) == len(set(selected)) == 40


def _artifacts(tmp_path: Path, count: int = 4) -> Path:
    commit = _init_checkout(tmp_path)
    nodeids = [f"test_sample.py::test_example[{index}]" for index in range(100)]
    artifacts = tmp_path / "artifacts"
    for index in range(count):
        source = tmp_path / f"producer-{index}" / "src" / "elspeth"
        source.mkdir(parents=True)
        module = source / "sample.py"
        module.write_text("value = 1\nvalue += 1\n", encoding="utf-8")
        folder = artifacts / f"coverage-shard-3.13-{index}"
        folder.mkdir(parents=True)
        data = coverage.CoverageData(basename=str(folder / "coverage-data"))
        data.add_lines({str(module): {1 if index < 2 else 2}})
        data.write()
        (folder / "shard-manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "tested_commit": commit,
                    "python_version": "3.13",
                    "github_run_id": None,
                    "github_run_attempt": None,
                    "producer_job": None,
                    "partition": "all",
                    "shard_count": count,
                    "shard_index": index,
                    "collection_digest": collection_digest(nodeids),
                    "selected_nodeids": [nodeid for nodeid in nodeids if shard_index(nodeid, count) == index],
                    "exit_status": 0,
                    "coverage_sha256": hashlib.sha256((folder / "coverage-data").read_bytes()).hexdigest(),
                }
            ),
            encoding="utf-8",
        )
    return artifacts


def test_plugin_records_a_failing_pytest_exit(tmp_path: Path) -> None:
    sample = tmp_path / "test_failure.py"
    sample.write_text(
        "import pytest\n@pytest.mark.parametrize('case', range(40))\ndef test_failure(case): assert False\n", encoding="utf-8"
    )
    report = tmp_path / "shard.json"
    environment = os.environ.copy()
    for name in ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID"):
        environment.pop(name, None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(sample),
            "-c",
            "/dev/null",
            "--rootdir",
            str(tmp_path),
            "-p",
            "scripts.ci_test_shards",
            "-n",
            "0",
            "--ci-shard-count",
            "4",
            "--ci-shard-index",
            "0",
            "--ci-shard-report",
            str(report),
            "-q",
        ],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert json.loads(report.read_text(encoding="utf-8"))["exit_status"] == 1


@pytest.mark.parametrize("wrong_sha", [False, True])
def test_plugin_verifies_native_actions_commit_and_removes_stale_report(tmp_path: Path, wrong_sha: bool) -> None:
    sample = tmp_path / "test_native.py"
    executed = tmp_path / "executed"
    sample.write_text(f"from pathlib import Path\ndef test_native(): Path({str(executed)!r}).touch()\n", encoding="utf-8")
    report = tmp_path / "shard.json"
    report.write_text('{"exit_status": 0}', encoding="utf-8")
    environment = os.environ.copy()
    for name in ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID"):
        environment.pop(name, None)
    commit = evidence_identity(REPO_ROOT)["tested_commit"]
    environment.update(
        GITHUB_ACTIONS="true",
        GITHUB_SHA="0" * 40 if wrong_sha else commit,
        GITHUB_RUN_ID="1234",
        GITHUB_RUN_ATTEMPT="2",
        GITHUB_JOB="test",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(sample),
            "-c",
            "/dev/null",
            "--rootdir",
            str(tmp_path),
            "-p",
            "scripts.ci_test_shards",
            "-n",
            "0",
            "--ci-shard-count",
            "1",
            "--ci-shard-index",
            "0",
            "--ci-shard-report",
            str(report),
            "-q",
        ],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    if wrong_sha:
        assert result.returncode == 4, result.stdout + result.stderr
        assert "Checked-out Git HEAD differs" in result.stdout + result.stderr
        assert not report.exists()
        assert not executed.exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert executed.exists()
        manifest = json.loads(report.read_text(encoding="utf-8"))
        assert manifest["tested_commit"] == commit
        assert manifest["github_run_id"] == "1234"
        assert manifest["github_run_attempt"] == "2"
        assert manifest["producer_job"] == "test"
        assert manifest["exit_status"] == 0


def test_manifest_binds_finalized_real_pytest_cov_database(tmp_path: Path) -> None:
    module = tmp_path / "covered.py"
    module.write_text("def double(value):\n    return value * 2\n", encoding="utf-8")
    sample = tmp_path / "test_covered.py"
    sample.write_text(
        "import pytest\nfrom covered import double\n@pytest.mark.parametrize('value', range(40))\n"
        "def test_double(value): assert double(value) == value * 2\n",
        encoding="utf-8",
    )
    report = tmp_path / "shard.json"
    database = tmp_path / "coverage-data"
    environment = os.environ.copy()
    for name in ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID"):
        environment.pop(name, None)
    environment["COVERAGE_FILE"] = str(database)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(sample),
            "-c",
            "/dev/null",
            "--rootdir",
            str(tmp_path),
            "-p",
            "scripts.ci_test_shards",
            "-n",
            "2",
            "--ci-shard-count",
            "4",
            "--ci-shard-index",
            "0",
            "--ci-shard-report",
            str(report),
            "--cov=covered",
            "--cov-report=",
            "--cov-fail-under=0",
            "-q",
        ],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads(report.read_text(encoding="utf-8"))
    assert manifest["coverage_sha256"] == hashlib.sha256(database.read_bytes()).hexdigest()
    data = coverage.CoverageData(basename=str(database))
    data.read()
    assert data.lines(str(module)) == [1, 2]


@pytest.mark.parametrize("existing", [False, True], ids=["absent", "unfinalized"])
@pytest.mark.parametrize("fault", ["write", "replace", "interrupt"])
def test_manifest_publisher_never_exposes_partial_terminal_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool, fault: str
) -> None:
    report = tmp_path / "shard.json"
    terminal = {"exit_status": 0, "coverage_sha256": "finalized"}
    _publish_manifest(report, terminal)
    assert json.loads(report.read_text(encoding="utf-8")) == terminal
    assert not list(tmp_path.glob("*.tmp"))
    report.unlink()
    prior = '{"exit_status": null}'
    if existing:
        report.write_text(prior, encoding="utf-8")
    write_text = Path.write_text

    def partial_write(path: Path, text: str, **kwargs: Any) -> int:
        assert path.parent == report.parent
        assert path.name == f"{report.name}.{os.getpid()}.tmp"
        write_text(path, text[:10], **kwargs)
        raise OSError("temporary write failed")

    def failed_replace(path: Path, destination: Path) -> Path:
        assert path.parent == destination.parent == report.parent
        assert destination == report
        assert json.loads(path.read_text(encoding="utf-8")) == terminal
        if fault == "interrupt":
            raise KeyboardInterrupt("publication interrupted")
        raise OSError("replace failed")

    if fault == "write":
        monkeypatch.setattr(Path, "write_text", partial_write)
    else:
        monkeypatch.setattr(Path, "replace", failed_replace)
    with pytest.raises(KeyboardInterrupt if fault == "interrupt" else OSError):
        _publish_manifest(report, terminal)
    if existing:
        assert report.read_text(encoding="utf-8") == prior
    else:
        assert not report.exists()


@pytest.mark.parametrize("partition", ["all", "proof"])
@pytest.mark.parametrize("failure", [False, True], ids=["passing", "failing"])
@pytest.mark.parametrize("fault", [None, "write", "replace", "interrupt"])
def test_terminal_publication_with_real_pytest_cov_and_faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, partition: str, failure: bool, fault: str | None
) -> None:
    _partition_suite(tmp_path)
    module = tmp_path / "covered.py"
    module.write_text("def double(value):\n    return value * 2\n", encoding="utf-8")
    proof = tmp_path / LONG_RUN_CASES["no-escape"].split("::")[0]
    source = proof.read_text(encoding="utf-8")
    proof.write_text(
        "from covered import double\n" + source.replace("assert True", f"assert double(2) == {5 if failure else 4}"),
        encoding="utf-8",
    )
    database = tmp_path / "coverage-data"
    monkeypatch.setenv("COVERAGE_FILE", str(database))
    arguments = ["--cov=covered", "--cov-report=", "--cov-fail-under=0"]
    result, report = _partition_run(tmp_path, partition, extra_args=arguments, case="no-escape" if partition == "proof" else None)
    (tmp_path / "producer-control.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    (tmp_path / "producer-control.exit").write_text(f"{result.returncode}\n", encoding="utf-8")
    assert result.returncode == (1 if failure else 0), result.stdout + result.stderr
    manifest = json.loads(report.read_text(encoding="utf-8"))
    assert manifest["exit_status"] == result.returncode
    assert manifest["coverage_sha256"] == hashlib.sha256(database.read_bytes()).hexdigest()
    if partition == "proof":
        assert manifest["proof_call_passed"] is (not failure)
    data = coverage.CoverageData(basename=str(database))
    data.read()
    assert data.lines(str(module)) == [1, 2]
    assert not list(tmp_path.glob("*.tmp"))
    if fault is None:
        return
    # Inject only terminal publication faults, after collection evidence exists.
    (tmp_path / "conftest.py").write_text(
        "from pathlib import Path\nimport json\nimport pytest\n"
        f"FAULT = {fault!r}\n"
        "def pytest_configure(config):\n"
        "    patch = pytest.MonkeyPatch()\n"
        "    write_text = Path.write_text\n"
        "    replace = Path.replace\n"
        "    def write(path, text, **kwargs):\n"
        "        if path.name.endswith('.tmp') and json.loads(text)['exit_status'] is not None and FAULT == 'write':\n"
        "            write_text(path, text[:10], **kwargs)\n"
        "            raise OSError('terminal temporary write failed')\n"
        "        return write_text(path, text, **kwargs)\n"
        "    def publish(path, target):\n"
        "        if path.name.endswith('.tmp') and json.loads(path.read_text())['exit_status'] is not None:\n"
        "            if FAULT == 'replace':\n"
        "                raise OSError('terminal replace failed')\n"
        "            if FAULT == 'interrupt':\n"
        "                raise KeyboardInterrupt('terminal publication interrupted')\n"
        "        return replace(path, target)\n"
        "    patch.setattr(Path, 'write_text', write)\n"
        "    patch.setattr(Path, 'replace', publish)\n",
        encoding="utf-8",
    )
    result, report = _partition_run(tmp_path, partition, extra_args=arguments, case="no-escape" if partition == "proof" else None)
    (tmp_path / "producer-fault.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    (tmp_path / "producer-fault.exit").write_text(f"{result.returncode}\n", encoding="utf-8")
    assert result.returncode != 0, result.stdout + result.stderr
    expected = {
        "write": "terminal temporary write failed",
        "replace": "terminal replace failed",
        "interrupt": "terminal publication interrupted",
    }
    assert expected[fault] in result.stdout + result.stderr
    manifest = json.loads(report.read_text(encoding="utf-8"))
    assert manifest["exit_status"] is None
    assert "coverage_sha256" not in manifest
    assert "proof_call_passed" not in manifest
    assert database.is_file()


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_manifest",
        "missing_database",
        "failed_shard",
        "wrong_collection",
        "wrong_bucket",
        "duplicate",
        "omitted_id",
        "missing_hash",
        "tampered_database",
        "swapped_database",
        "empty_database",
        "foreign_database",
        "wrong_commit",
        "wrong_version",
        "wrong_run",
        "wrong_producer",
        "invalid_attempt",
        "missing_identity",
        "legacy_schema",
        "unfinalized",
        "partitioned_manifest",
    ],
)
def test_coverage_combine_refuses_incomplete_or_inconsistent_shards(tmp_path: Path, mutation: str) -> None:
    artifacts = _artifacts(tmp_path)
    source = tmp_path / "src" / "elspeth"
    source.mkdir(parents=True)
    (source / "sample.py").write_text("value = 1\nvalue += 1\n", encoding="utf-8")
    # Each negative control starts from evidence that successfully combines.
    combine_shards(artifacts, 4, tmp_path)
    (tmp_path / ".coverage").unlink()
    folder = artifacts / "coverage-shard-3.13-0"
    report = folder / "shard-manifest.json"
    manifest = json.loads(report.read_text(encoding="utf-8"))
    if mutation == "missing_manifest":
        report.unlink()
    elif mutation == "missing_database":
        (folder / "coverage-data").unlink()
    elif mutation == "failed_shard":
        manifest["exit_status"] = 1
    elif mutation == "unfinalized":
        manifest["exit_status"] = None
    elif mutation == "partitioned_manifest":
        manifest["partition"] = "ordinary"
    elif mutation == "wrong_commit":
        manifest["tested_commit"] = "0" * 40
    elif mutation == "wrong_version":
        manifest["python_version"] = "3.12"
    elif mutation == "wrong_run":
        manifest["github_run_id"] = "9999"
    elif mutation == "wrong_producer":
        manifest["producer_job"] = "other-job"
    elif mutation == "invalid_attempt":
        manifest["github_run_attempt"] = "not-an-attempt"
    elif mutation == "missing_identity":
        del manifest["github_run_id"]
    elif mutation == "legacy_schema":
        manifest["schema_version"] = 1
    elif mutation == "wrong_collection":
        manifest["collection_digest"] = "different"
    elif mutation == "wrong_bucket":
        manifest["selected_nodeids"].append("wrong-bucket")
    elif mutation == "duplicate":
        manifest["selected_nodeids"].append(manifest["selected_nodeids"][0])
    elif mutation == "omitted_id":
        manifest["selected_nodeids"].pop()
    elif mutation == "missing_hash":
        del manifest["coverage_sha256"]
    elif mutation == "tampered_database":
        with (folder / "coverage-data").open("ab") as stream:
            stream.write(b"tampered")
    elif mutation == "swapped_database":
        (folder / "coverage-data").write_bytes((artifacts / "coverage-shard-3.13-1" / "coverage-data").read_bytes())
    elif mutation == "empty_database":
        database = folder / "coverage-data"
        database.unlink()
        data = coverage.CoverageData(basename=str(database))
        data.add_lines({str(tmp_path / "producer-0" / "src" / "elspeth" / "sample.py"): set()})
        data.write()
        manifest["coverage_sha256"] = hashlib.sha256(database.read_bytes()).hexdigest()
    else:
        database = folder / "coverage-data"
        database.unlink()
        data = coverage.CoverageData(basename=str(database))
        data.add_lines({str(tmp_path / "foreign.py"): {1}})
        data.write()
        manifest["coverage_sha256"] = hashlib.sha256(database.read_bytes()).hexdigest()
    if report.exists():
        report.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises((ValueError, FileNotFoundError)):
        combine_shards(artifacts, 4, tmp_path)


def test_combine_remaps_checkout_roots_and_unions_coverage(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    destination = tmp_path / "aggregate"
    (destination / "src" / "elspeth").mkdir(parents=True)
    (destination / "src" / "elspeth" / "sample.py").write_text("value = 1\nvalue += 1\n", encoding="utf-8")
    combine_shards(artifacts, 4, destination)
    data = coverage.CoverageData(basename=str(destination / ".coverage"))
    data.read()
    files = list(data.measured_files())
    assert len(files) == 1
    assert Path(files[0]) == destination / "src" / "elspeth" / "sample.py"
    assert data.lines(files[0]) == [1, 2]


@pytest.mark.parametrize("mutation", [None, "wrong_run", "invalid_attempt", "wrong_producer", "wrong_checkout_sha"])
def test_combine_binds_native_run_and_accepts_partial_rerun_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str | None
) -> None:
    artifacts = _artifacts(tmp_path)
    source = tmp_path / "src" / "elspeth"
    source.mkdir(parents=True)
    (source / "sample.py").write_text("value = 1\nvalue += 1\n", encoding="utf-8")
    commit = evidence_identity(tmp_path)["tested_commit"]
    assert commit is not None
    for name, value in {
        "GITHUB_ACTIONS": "true",
        "GITHUB_SHA": commit,
        "GITHUB_RUN_ID": "1234",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_JOB": "coverage",
    }.items():
        monkeypatch.setenv(name, value)
    for index in range(4):
        report = artifacts / f"coverage-shard-3.13-{index}" / "shard-manifest.json"
        manifest = json.loads(report.read_text(encoding="utf-8"))
        manifest.update(github_run_id="1234", github_run_attempt="1" if index < 2 else "2", producer_job="test")
        report.write_text(json.dumps(manifest), encoding="utf-8")
    # Control: native evidence from both attempts must combine before mutation.
    combine_shards(artifacts, 4, tmp_path)
    (tmp_path / ".coverage").unlink()
    if mutation is None:
        return
    report = artifacts / "coverage-shard-3.13-0" / "shard-manifest.json"
    manifest = json.loads(report.read_text(encoding="utf-8"))
    if mutation == "wrong_run":
        manifest["github_run_id"] = "9999"
    elif mutation == "invalid_attempt":
        manifest["github_run_attempt"] = "0"
    elif mutation == "wrong_producer":
        manifest["producer_job"] = "coverage"
    else:
        monkeypatch.setenv("GITHUB_SHA", "0" * 40)
    report.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        combine_shards(artifacts, 4, tmp_path)


def _partition_suite(tmp_path: Path, mutation: str | None = None, case: str = "no-escape") -> None:
    _init_checkout(tmp_path)
    module = tmp_path / LONG_RUN_CASES["no-escape"].split("::")[0]
    module.parent.mkdir(parents=True)
    name = LONG_RUN_CASES[case].split("::")[1]
    decorators = "@pytest.mark.landscape_long_run\n"
    body = "assert True"
    extra = ""
    if mutation == "missing_proof":
        name = "test_renamed_proof"
    elif mutation == "missing_marker":
        decorators = ""
    elif mutation == "extra_marker":
        extra = "@pytest.mark.landscape_long_run\ndef test_extra_proof(): assert True\n"
    elif mutation == "misflagged":
        decorators = ""
        extra = "@pytest.mark.landscape_long_run\ndef test_impostor(): assert True\n"
    elif mutation == "deselected_proof":
        decorators += "@pytest.mark.protected\n"
    elif mutation == "skip":
        body = "pytest.skip('control')"
    elif mutation == "setup_skip":
        decorators += "@pytest.mark.skip(reason='control')\n"
    elif mutation == "xfail":
        body = "pytest.xfail('control')"
    elif mutation == "xpass":
        decorators += "@pytest.mark.xfail(reason='control', strict=False)\n"
    elif mutation == "failure":
        body = "assert False"
    module.write_text(
        f"import pytest\n{decorators}def {name}(): {body}\n{extra}"
        + "".join(
            f"@pytest.mark.landscape_long_run\ndef {nodeid.split('::')[1]}(): assert True\n"
            for key, nodeid in LONG_RUN_CASES.items()
            if key != case
        )
        + "@pytest.mark.parametrize('value', range(40))\ndef test_ordinary(value): assert value >= 0\n"
        "@pytest.mark.protected\ndef test_protected(): assert False\n",
        encoding="utf-8",
    )
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    landscape_long_run: exhaustive proof\n    protected: omitted control\n",
        encoding="utf-8",
    )


def _partition_run(
    tmp_path: Path,
    partition: str = "all",
    count: int = 1,
    index: int = 0,
    workers: str = "0",
    extra_args: list[str] | None = None,
    case: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    report = tmp_path / f"{partition}-{case}-{index}.json"
    environment = os.environ.copy()
    for name in ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID"):
        environment.pop(name, None)
    environment["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/",
            "-p",
            "scripts.ci_test_shards",
            "-m",
            "not protected",
            "-n",
            workers,
            "--ci-shard-count",
            str(count),
            "--ci-shard-index",
            str(index),
            "--ci-shard-report",
            str(report),
            "--ci-partition",
            partition,
            *(["--ci-proof-case", case] if case is not None else []),
            "-q",
            *(extra_args or []),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    (tmp_path / f"{report.stem}.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    (tmp_path / f"{report.stem}.exit").write_text(f"{result.returncode}\n", encoding="utf-8")
    return result, report


def test_active_partitions_preserve_complete_disjoint_marker_selected_collection(tmp_path: Path) -> None:
    _partition_suite(tmp_path)
    result, report = _partition_run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    complete = json.loads(report.read_text(encoding="utf-8"))
    assert complete["schema_version"] == 2
    assert "proof_case" not in complete and "proof_call_passed" not in complete
    nodeids = complete["selected_nodeids"]
    assert len(nodeids) == 43
    assert LONG_RUN_CASES["no-escape"] in nodeids
    assert not any("test_protected" in nodeid for nodeid in nodeids)
    ordinary: list[str] = []
    for index in range(4):
        result, report = _partition_run(tmp_path, "ordinary", 4, index)
        assert result.returncode == 0, result.stdout + result.stderr
        manifest = json.loads(report.read_text(encoding="utf-8"))
        assert manifest["partition"] == "ordinary"
        assert manifest["schema_version"] == 4 and manifest["proof_case"] is None
        assert "proof_call_passed" not in manifest
        assert manifest["collection_digest"] == complete["collection_digest"]
        assignment = ordinary_assignment(
            [nodeid for nodeid in complete["selected_nodeids"] if nodeid not in LONG_RUN_CASES.values()],
            4,
            load_duration_profile(tmp_path),
        )
        assert all(assignment[nodeid] == index for nodeid in manifest["selected_nodeids"])
        ordinary.extend(manifest["selected_nodeids"])
    proofs = []
    for case, nodeid in LONG_RUN_CASES.items():
        result, report = _partition_run(tmp_path, "proof", case=case)
        assert result.returncode == 0, result.stdout + result.stderr
        proof = json.loads(report.read_text(encoding="utf-8"))
        assert proof["partition"] == "proof" and proof["proof_case"] == case
        assert proof["schema_version"] == 4
        assert proof["selected_nodeids"] == [nodeid]
        assert proof["collection_digest"] == complete["collection_digest"]
        assert proof["proof_call_passed"] is True
        assert proof["exit_status"] == 0
        proofs.extend(proof["selected_nodeids"])
    assert len(ordinary) == len(set(ordinary)) == 40
    assert not set(proofs) & set(ordinary)
    assert len(ordinary + proofs) == len(set(ordinary + proofs)) == len(nodeids)
    assert set(ordinary + proofs) == set(nodeids)


@pytest.mark.parametrize("mutation", ["missing_proof", "missing_marker", "extra_marker", "misflagged", "deselected_proof"])
@pytest.mark.parametrize("partition", ["ordinary", "proof"])
@pytest.mark.parametrize("case", list(LONG_RUN_CASES))
def test_partition_refuses_changed_proof_identity_or_marker(tmp_path: Path, mutation: str, partition: str, case: str) -> None:
    _partition_suite(tmp_path, mutation, case)
    result, report = _partition_run(tmp_path, partition, case=case if partition == "proof" else None)
    assert result.returncode == 4, result.stdout + result.stderr
    assert "Long-run selection requires" in result.stdout + result.stderr
    assert not report.exists()


@pytest.mark.parametrize("mutation", ["skip", "setup_skip", "xfail", "xpass", "failure"])
@pytest.mark.parametrize("case", list(LONG_RUN_CASES))
def test_proof_partition_requires_an_actual_successful_call(tmp_path: Path, mutation: str, case: str) -> None:
    _partition_suite(tmp_path, mutation, case)
    result, report = _partition_run(tmp_path, "proof", case=case)
    assert result.returncode == 1, result.stdout + result.stderr
    manifest = json.loads(report.read_text(encoding="utf-8"))
    assert manifest["selected_nodeids"] == [LONG_RUN_CASES[case]]
    assert manifest["proof_call_passed"] is False
    assert manifest["exit_status"] == 1


def test_default_partition_does_not_require_proof_marker(tmp_path: Path) -> None:
    _partition_suite(tmp_path, "missing_marker")
    result, report = _partition_run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads(report.read_text(encoding="utf-8"))
    assert manifest["partition"] == "all"
    assert len(manifest["selected_nodeids"]) == 43
    assert LONG_RUN_CASES["no-escape"] in manifest["selected_nodeids"]
    assert "proof_call_passed" not in manifest


@pytest.mark.parametrize("count,workers", [(4, "0"), (1, "2")])
def test_proof_partition_rejects_multiple_shards_or_workers(tmp_path: Path, count: int, workers: str) -> None:
    _partition_suite(tmp_path)
    result, report = _partition_run(tmp_path, "proof", count, 0, workers, case="no-escape")
    assert result.returncode == 4, result.stdout + result.stderr
    assert "requires one shard at index zero" in result.stdout + result.stderr
    assert not report.exists()


@pytest.mark.parametrize(
    "partition,case",
    [
        ("proof", None),
        ("proof", ""),
        ("proof", "unknown"),
        ("ordinary", "no-escape"),
        ("all", "no-escape"),
    ],
)
def test_partition_cli_requires_case_only_for_proof(tmp_path: Path, partition: str, case: str | None) -> None:
    _partition_suite(tmp_path)
    result, report = _partition_run(tmp_path, partition, case=case)
    assert result.returncode == 4, result.stdout + result.stderr
    assert not report.exists()


def test_all_without_shard_flags_still_refuses_case(tmp_path: Path) -> None:
    _partition_suite(tmp_path)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "scripts.ci_test_shards", "-n", "0", "--ci-proof-case=no-escape"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 4, completed.stdout + completed.stderr
    (tmp_path / "no-shard-case.log").write_text(completed.stdout + completed.stderr)
    (tmp_path / "no-shard-case.exit").write_text(f"{completed.returncode}\n")


@pytest.mark.parametrize("case", list(LONG_RUN_CASES))
@pytest.mark.parametrize("sequence", ["wrong_right", "right_right", "failed_passed"])
def test_active_call_adapter_preserves_absorbing_refusal(tmp_path: Path, case: str, sequence: str) -> None:
    _partition_suite(tmp_path)
    (tmp_path / "conftest.py").write_text(
        "import copy\nimport pytest\nfrom scripts import ci_test_shards as ci\n"
        f"SEQUENCE = {sequence!r}\n"
        "@pytest.hookimpl(wrapper=True, trylast=True)\n"
        "def pytest_runtest_makereport(item, call):\n"
        "    report = yield\n"
        "    if report.when == 'call':\n"
        "        original = item.nodeid\n"
        "        if SEQUENCE == 'wrong_right': item._nodeid = 'wrong::node'\n"
        "        first = copy.copy(report)\n"
        "        if SEQUENCE == 'failed_passed': first.outcome = 'failed'\n"
        "        for phase in ['call', 'setup', 'teardown']:\n"
        "            synthetic = copy.copy(first)\n"
        "            synthetic.when = phase\n"
        "            reducer = ci.pytest_runtest_makereport(item, call)\n"
        "            next(reducer)\n"
        "            try: reducer.send(synthetic)\n"
        "            except StopIteration: pass\n"
        "        item._nodeid = original\n"
        "    return report\n",
        encoding="utf-8",
    )
    result, report = _partition_run(tmp_path, "proof", case=case)
    assert result.returncode == 1, result.stdout + result.stderr
    manifest = json.loads(report.read_text())
    assert manifest["selected_nodeids"] == [LONG_RUN_CASES[case]]
    assert manifest["proof_call_passed"] is False and manifest["exit_status"] == 1


@pytest.mark.parametrize(
    "partition,case,fault",
    [
        ("ordinary" if case is None else "proof", case, fault)
        for case in [None, *LONG_RUN_CASES]
        for fault in ["native_status", "inner_exception", "teardown"]
    ]
    + [("all", None, "inner_exception")],
)
def test_active_finalization_uses_effective_native_status_and_refuses_inner_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, partition: str, case: str | None, fault: str
) -> None:
    _partition_suite(tmp_path)
    (tmp_path / "covered.py").write_text("def double(value):\n    return value * 2\n")
    module = tmp_path / LONG_RUN_CASES["no-escape"].split("::")[0]
    module.write_text("from covered import double\n" + module.read_text().replace("assert True", "assert double(2) == 4"))
    (tmp_path / "conftest.py").write_text(
        "import pytest\n"
        f"FAULT = {fault!r}\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    if FAULT == 'native_status': session.exitstatus = pytest.ExitCode.TESTS_FAILED\n"
        "    elif FAULT == 'inner_exception': raise RuntimeError('controlled inner finalizer failure')\n"
        "@pytest.fixture(autouse=True)\n"
        "def final_teardown():\n"
        "    yield\n"
        "    if FAULT == 'teardown': pytest.fail('controlled teardown failure')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("COVERAGE_FILE", str(tmp_path / "coverage-data"))
    result, report = _partition_run(
        tmp_path,
        partition,
        case=case,
        extra_args=["--cov=covered", "--cov-report=", "--cov-fail-under=0"],
    )
    assert result.returncode != 0, result.stdout + result.stderr
    manifest = json.loads(report.read_text())
    if fault == "inner_exception":
        assert "controlled inner finalizer failure" in result.stdout + result.stderr
        assert manifest["exit_status"] is None and "coverage_sha256" not in manifest
        assert "proof_call_passed" not in manifest
    else:
        assert manifest["exit_status"] == result.returncode == 1
        assert manifest["coverage_sha256"] == hashlib.sha256((tmp_path / "coverage-data").read_bytes()).hexdigest()
        if case is not None:
            assert manifest["proof_call_passed"] is True


@pytest.mark.parametrize("case", list(LONG_RUN_CASES))
def test_active_no_call_refusal_is_visible_to_inner_finalizers(tmp_path: Path, case: str) -> None:
    _partition_suite(tmp_path)
    (tmp_path / "conftest.py").write_text(
        "from pathlib import Path\nimport pytest\n"
        "def pytest_runtest_protocol(item, nextitem): return True\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED\n"
        "    Path('inner-status.txt').write_text(str(int(session.exitstatus)))\n",
        encoding="utf-8",
    )
    result, report = _partition_run(tmp_path, "proof", case=case)
    assert result.returncode == 1, result.stdout + result.stderr
    assert (tmp_path / "inner-status.txt").read_text() == "1"
    manifest = json.loads(report.read_text())
    assert manifest["proof_call_passed"] is False and manifest["exit_status"] == 1


def _long_run_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str, count: int = 4) -> Path:
    artifacts = _artifacts(tmp_path, count)
    nodeids = [f"test_sample.py::test_example[{index}]" for index in range(100)] + list(LONG_RUN_CASES.values())
    profile = load_duration_profile(tmp_path)
    assignment = ordinary_assignment(nodeids[:-3], count, profile)
    commit = evidence_identity(tmp_path)["tested_commit"]
    assert commit is not None
    for name, value in {
        "GITHUB_ACTIONS": "true",
        "GITHUB_SHA": commit,
        "GITHUB_RUN_ID": "1234",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_JOB": "coverage",
    }.items():
        monkeypatch.setenv(name, value)
    for index in range(count):
        folder = artifacts / f"coverage-shard-3.13-{index}"
        report = folder / "shard-manifest.json"
        manifest = json.loads(report.read_text(encoding="utf-8"))
        attempt = "1" if index < 2 else "2"
        manifest.update(
            schema_version=4,
            assignment_algorithm=ASSIGNMENT_ALGORITHM,
            duration_profile_sha256=profile.sha256,
            selected_nodeids=[nodeid for nodeid in nodeids[:-3] if assignment[nodeid] == index],
            proof_case=None,
            collection_digest=collection_digest(nodeids),
            partition="ordinary",
            python_version=version,
            github_run_id="1234",
            github_run_attempt=attempt,
            producer_job="test",
        )
        if version == "3.12":
            (folder / "coverage-data").unlink()
            del manifest["coverage_sha256"]
        report.write_text(json.dumps(manifest), encoding="utf-8")
        folder.rename(artifacts / f"ordinary-shard-{version}-{index}-attempt-{attempt}")
    for case, line in zip(LONG_RUN_CASES, [3, 4, 5], strict=True):
        folder = artifacts / f"landscape-proof-{case}-{version}-attempt-2"
        folder.mkdir()
        manifest = {
            "schema_version": 4,
            "assignment_algorithm": ASSIGNMENT_ALGORITHM,
            "duration_profile_sha256": profile.sha256,
            "tested_commit": commit,
            "python_version": version,
            "github_run_id": "1234",
            "github_run_attempt": "2",
            "producer_job": "proof",
            "partition": "proof",
            "proof_case": case,
            "shard_count": 1,
            "shard_index": 0,
            "collection_digest": collection_digest(nodeids),
            "selected_nodeids": [LONG_RUN_CASES[case]],
            "exit_status": 0,
            "proof_call_passed": True,
        }
        if version == "3.13":
            source = tmp_path / f"producer-{case}" / "src" / "elspeth" / "sample.py"
            source.parent.mkdir(parents=True)
            source.write_text("value = 1\nvalue += 1\nno_escape = 3\nreserve = 4\nrecovery = 5\n", encoding="utf-8")
            data = coverage.CoverageData(basename=str(folder / "coverage-data"))
            data.add_lines({str(source): {line}})
            data.write()
            manifest["coverage_sha256"] = hashlib.sha256((folder / "coverage-data").read_bytes()).hexdigest()
        (folder / "shard-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "src" / "elspeth").mkdir(parents=True)
    (tmp_path / "src" / "elspeth" / "sample.py").write_text(
        "value = 1\nvalue += 1\nno_escape = 3\nreserve = 4\nrecovery = 5\n", encoding="utf-8"
    )
    return artifacts


@pytest.mark.parametrize("version", ["3.12", "3.13"])
def test_long_run_consumer_requires_seven_producers_and_all_case_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    artifacts = _long_run_artifacts(tmp_path, monkeypatch, version)
    resolved = validate_long_run_shards(artifacts, 4, tmp_path, version)
    assert len(resolved) == 7
    for case in LONG_RUN_CASES:
        assert resolved[f"landscape-proof-{case}-{version}"] == f"landscape-proof-{case}-{version}-attempt-2"
    assert resolved[f"ordinary-shard-{version}-0"] == f"ordinary-shard-{version}-0-attempt-1"
    databases = [folder / "coverage-data" for folder in artifacts.iterdir() if (folder / "coverage-data").exists()]
    if version == "3.13":
        assert len(databases) == 7
        for case, line in zip(LONG_RUN_CASES, [3, 4, 5], strict=True):
            data = coverage.CoverageData(basename=str(artifacts / resolved[f"landscape-proof-{case}-{version}"] / "coverage-data"))
            data.read()
            assert [data.lines(filename) for filename in data.measured_files()] == [[line]]
        combined = coverage.CoverageData(basename=str(tmp_path / ".coverage"))
        combined.read()
        assert combined.lines(str(tmp_path / "src" / "elspeth" / "sample.py")) == [1, 2, 3, 4, 5]
    else:
        assert databases == []
        assert not (tmp_path / ".coverage").exists()


@pytest.mark.parametrize("version", ["3.12", "3.13"])
@pytest.mark.parametrize("producer", ["ordinary", *LONG_RUN_CASES])
@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "schema",
        "legacy_partitioned_schema",
        "assignment_algorithm",
        "duration_profile",
        "schema_float",
        "count_bool",
        "count_float",
        "index_bool",
        "index_float",
        "case_missing",
        "case_wrong",
        "wrong_run",
        "wrong_sha",
        "wrong_role",
        "wrong_version",
        "wrong_partition",
        "attempt",
        "failed",
        "unfinalized",
        "exit_bool",
        "exit_float",
        "call",
        "call_numeric",
        "call_missing",
        "omission",
        "overlap",
        "duplicate",
        "bucket",
        "digest",
    ],
)
def test_long_run_consumer_rejects_selected_invalid_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str, producer: str, mutation: str
) -> None:
    artifacts = _long_run_artifacts(tmp_path, monkeypatch, version)
    validate_long_run_shards(artifacts, 4, tmp_path, version)
    (tmp_path / ".coverage").unlink(missing_ok=True)
    folder = artifacts / (
        f"ordinary-shard-{version}-0-attempt-1" if producer == "ordinary" else f"landscape-proof-{producer}-{version}-attempt-2"
    )
    report = folder / "shard-manifest.json"
    manifest = json.loads(report.read_text())
    changes = {
        "schema": ("schema_version", 2),
        "legacy_partitioned_schema": ("schema_version", 3),
        "assignment_algorithm": ("assignment_algorithm", "sha256-modulo"),
        "duration_profile": ("duration_profile_sha256", "0" * 64),
        "schema_float": ("schema_version", 4.0),
        "count_bool": ("shard_count", True),
        "count_float": ("shard_count", 4.0 if producer == "ordinary" else 1.0),
        "index_bool": ("shard_index", False),
        "index_float": ("shard_index", 0.0),
        "case_wrong": ("proof_case", "unknown"),
        "wrong_run": ("github_run_id", "9999"),
        "wrong_sha": ("tested_commit", "0" * 40),
        "wrong_role": ("producer_job", "coverage"),
        "wrong_version": ("python_version", "unknown"),
        "wrong_partition": ("partition", "all"),
        "attempt": ("github_run_attempt", "99"),
        "failed": ("exit_status", 1),
        "unfinalized": ("exit_status", None),
        "exit_bool": ("exit_status", False),
        "exit_float": ("exit_status", 0.0),
        "call": ("proof_call_passed", False),
        "call_numeric": ("proof_call_passed", 1),
        "digest": ("collection_digest", "different"),
    }
    if mutation == "missing":
        shutil.rmtree(folder)
    elif mutation == "case_missing":
        del manifest["proof_case"]
    elif mutation == "call_missing":
        if producer == "ordinary":
            manifest["proof_call_passed"] = None
        else:
            del manifest["proof_call_passed"]
    elif mutation == "overlap":
        manifest["selected_nodeids"].append(LONG_RUN_CASES["no-escape"])
    elif mutation == "duplicate":
        manifest["selected_nodeids"].append(manifest["selected_nodeids"][0])
    elif mutation == "omission":
        manifest["selected_nodeids"].pop()
    elif mutation == "bucket":
        manifest["selected_nodeids"] = ["test_wrong.py::test_wrong"]
    else:
        field, value = changes[mutation]
        manifest[field] = value
    if report.exists():
        report.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises((ValueError, FileNotFoundError)):
        validate_long_run_shards(artifacts, 4, tmp_path, version)


@pytest.mark.parametrize("producer", [*(f"ordinary-{index}" for index in range(4)), *LONG_RUN_CASES])
@pytest.mark.parametrize("mutation", ["missing", "hash", "tampered", "foreign", "empty"])
def test_long_run_consumer_validates_each_real_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, producer: str, mutation: str
) -> None:
    artifacts = _long_run_artifacts(tmp_path, monkeypatch, "3.13")
    validate_long_run_shards(artifacts, 4, tmp_path, "3.13")
    (tmp_path / ".coverage").unlink()
    if producer.startswith("ordinary-"):
        index = int(producer.removeprefix("ordinary-"))
        folder = artifacts / f"ordinary-shard-3.13-{index}-attempt-{'1' if index < 2 else '2'}"
    else:
        folder = artifacts / f"landscape-proof-{producer}-3.13-attempt-2"
    database = folder / "coverage-data"
    report = folder / "shard-manifest.json"
    manifest = json.loads(report.read_text())
    if mutation == "missing":
        database.unlink()
    elif mutation == "hash":
        manifest["coverage_sha256"] = "wrong"
    elif mutation == "tampered":
        with database.open("ab") as stream:
            stream.write(b"tampered")
    else:
        database.unlink()
        data = coverage.CoverageData(basename=str(database))
        data.add_lines({str(tmp_path / "foreign.py"): {1}} if mutation == "foreign" else {})
        data.write()
        manifest["coverage_sha256"] = hashlib.sha256(database.read_bytes()).hexdigest()
    report.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises((ValueError, FileNotFoundError)):
        validate_long_run_shards(artifacts, 4, tmp_path, "3.13")


@pytest.mark.parametrize("version", ["3.12", "3.13"])
@pytest.mark.parametrize("producer", ["ordinary", *LONG_RUN_CASES])
@pytest.mark.parametrize("selected", [False, True], ids=["superseded", "selected"])
@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "truncated",
        "non_object",
        "wrong_case",
        "wrong_commit",
        "wrong_run",
        "wrong_version",
        "wrong_producer",
        "wrong_shard",
        "wrong_attempt",
        "schema_float",
        "status_bool",
        "failed",
        "unfinalized",
        "database",
        "wrong_hash",
        "tampered_database",
    ],
)
def test_long_run_consumer_retains_newest_attempt_content_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str, producer: str, selected: bool, damage: str
) -> None:
    artifacts = _long_run_artifacts(tmp_path, monkeypatch, version)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "3")
    stem = f"ordinary-shard-{version}-0" if producer == "ordinary" else f"landscape-proof-{producer}-{version}"
    older = artifacts / f"{stem}-attempt-{'1' if producer == 'ordinary' else '2'}"
    newer = artifacts / f"{stem}-attempt-3"
    shutil.copytree(older, newer)
    report = newer / "shard-manifest.json"
    manifest = json.loads(report.read_text())
    manifest["github_run_attempt"] = "3"
    report.write_text(json.dumps(manifest), encoding="utf-8")
    assert validate_long_run_shards(artifacts, 4, tmp_path, version)[stem] == newer.name
    (tmp_path / ".coverage").unlink(missing_ok=True)
    folder = newer if selected else older
    report = folder / "shard-manifest.json"
    if damage == "missing":
        report.unlink()
    elif damage == "truncated":
        report.write_text('{"schema_version":', encoding="utf-8")
    elif damage == "non_object":
        report.write_text("[]", encoding="utf-8")
    elif damage in {"database", "wrong_hash", "tampered_database"}:
        if version == "3.13":
            database = folder / "coverage-data"
            if damage == "database":
                database.unlink()
            elif damage == "tampered_database":
                with database.open("ab") as stream:
                    stream.write(b"tampered")
            else:
                manifest = json.loads(report.read_text())
                manifest["coverage_sha256"] = "wrong"
                report.write_text(json.dumps(manifest), encoding="utf-8")
        else:
            report.write_text("[]", encoding="utf-8")
    else:
        manifest = json.loads(report.read_text())
        field, value = {
            "wrong_case": ("proof_case", "unknown"),
            "failed": ("exit_status", 1),
            "unfinalized": ("exit_status", None),
            "wrong_commit": ("tested_commit", "0" * 40),
            "wrong_run": ("github_run_id", "9999"),
            "wrong_version": ("python_version", "unknown"),
            "wrong_producer": ("producer_job", "unknown"),
            "wrong_shard": ("shard_index", 99),
            "wrong_attempt": ("github_run_attempt", "99"),
            "schema_float": ("schema_version", 4.0),
            "status_bool": ("exit_status", False),
        }[damage]
        manifest[field] = value
        report.write_text(json.dumps(manifest), encoding="utf-8")
    if selected:
        with pytest.raises((ValueError, FileNotFoundError)):
            validate_long_run_shards(artifacts, 4, tmp_path, version)
    else:
        resolved = validate_long_run_shards(artifacts, 4, tmp_path, version)
        assert len(resolved) == 7
        assert resolved[stem] == newer.name
        if version == "3.13":
            data = coverage.CoverageData(basename=str(tmp_path / ".coverage"))
            data.read()
            assert data.lines(str(tmp_path / "src" / "elspeth" / "sample.py")) == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("version", ["3.12", "3.13"])
@pytest.mark.parametrize("case", list(LONG_RUN_CASES))
@pytest.mark.parametrize("mutation", ["unknown", "alias", "future", "duplicate", "singleton_name", "actual_sha"])
def test_long_run_consumer_refuses_invalid_native_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str, case: str, mutation: str
) -> None:
    artifacts = _long_run_artifacts(tmp_path, monkeypatch, version)
    validate_long_run_shards(artifacts, 4, tmp_path, version)
    (tmp_path / ".coverage").unlink(missing_ok=True)
    if mutation == "actual_sha":
        monkeypatch.setenv("GITHUB_SHA", "0" * 40)
    elif mutation == "duplicate":
        folders = list(artifacts.iterdir())
        duplicate = artifacts / f"landscape-proof-{case}-{version}-attempt-2"
        monkeypatch.setattr(Path, "iterdir", lambda path: iter([*folders, duplicate]))
    else:
        name = {
            "unknown": f"landscape-proof-unknown-{version}-attempt-1",
            "alias": f"landscape-proof-{case}-{version}-attempt-02",
            "future": f"landscape-proof-{case}-{version}-attempt-3",
            "singleton_name": f"landscape-proof-{version}-attempt-2",
        }[mutation]
        (artifacts / name).mkdir()
    with pytest.raises(ValueError):
        validate_long_run_shards(artifacts, 4, tmp_path, version)


@pytest.mark.parametrize("count", [True, 4.0, 0, -1])
def test_long_run_consumer_requires_strict_shard_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int) -> None:
    artifacts = _long_run_artifacts(tmp_path, monkeypatch, "3.12")
    validate_long_run_shards(artifacts, 4, tmp_path, "3.12")
    with pytest.raises(ValueError):
        validate_long_run_shards(artifacts, count, tmp_path, "3.12")


@pytest.mark.parametrize("version", ["3.12", "3.13"])
def test_long_run_consumer_bucket_check_is_independent_of_union_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    artifacts = _long_run_artifacts(tmp_path, monkeypatch, version)
    validate_long_run_shards(artifacts, 4, tmp_path, version)
    reports = list(artifacts.glob("*/shard-manifest.json"))
    manifests = {report: json.loads(report.read_text()) for report in reports}
    first = artifacts / f"ordinary-shard-{version}-0-attempt-1" / "shard-manifest.json"
    second = artifacts / f"ordinary-shard-{version}-1-attempt-1" / "shard-manifest.json"
    manifests[first]["selected_nodeids"][0], manifests[second]["selected_nodeids"][0] = (
        manifests[second]["selected_nodeids"][0],
        manifests[first]["selected_nodeids"][0],
    )
    for report, manifest in manifests.items():
        report.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="misassigned"):
        validate_long_run_shards(artifacts, 4, tmp_path, version)


@pytest.mark.parametrize("version", ["3.12", "3.13"])
def test_long_run_consumer_rederives_unequal_weights_instead_of_equal_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    artifacts = _long_run_artifacts(tmp_path, monkeypatch, version)
    validate_long_run_shards(artifacts, 4, tmp_path, version)
    reports = list(artifacts.glob("*/shard-manifest.json"))
    manifests = {report: json.loads(report.read_text()) for report in reports}
    ordinary = [manifest for manifest in manifests.values() if manifest["partition"] == "ordinary"]
    nodeids = [node for manifest in ordinary for node in manifest["selected_nodeids"]]
    old_assignment = ordinary_assignment(nodeids, 4, load_duration_profile(tmp_path))
    slow = next(node for node in sorted(nodeids) if old_assignment[node] != 0)
    path = tmp_path / DURATION_PROFILE_PATH
    profile_json = json.loads(path.read_text())
    profile_json["weights_ms"] = {slow: 1000000}
    path.write_text(json.dumps(profile_json), encoding="utf-8")
    profile = load_duration_profile(tmp_path)
    weighted_assignment = ordinary_assignment(nodeids, 4, profile)
    assert weighted_assignment[slow] == 0
    assert weighted_assignment != old_assignment
    for report, manifest in manifests.items():
        manifest["duration_profile_sha256"] = profile.sha256
        if manifest["partition"] == "ordinary":
            manifest["selected_nodeids"] = [node for node in nodeids if weighted_assignment[node] == manifest["shard_index"]]
        report.write_text(json.dumps(manifest), encoding="utf-8")
    assert len(validate_long_run_shards(artifacts, 4, tmp_path, version)) == 7
    for report, manifest in manifests.items():
        if manifest["partition"] == "ordinary":
            manifest["selected_nodeids"] = [node for node in nodeids if old_assignment[node] == manifest["shard_index"]]
        report.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="misassigned"):
        validate_long_run_shards(artifacts, 4, tmp_path, version)


def test_partitioned_validator_consumes_real_successful_pytest_producers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _partition_suite(tmp_path)
    source = tmp_path / "producer" / "src" / "elspeth"
    source.mkdir(parents=True)
    source_text = (
        "def double(value):\n    return value * 2\n"
        "def no_escape():\n    return 3\n"
        "def reserve():\n    return 4\n"
        "def recovery():\n    return 5\n"
    )
    (source / "sample.py").write_text(source_text, encoding="utf-8")
    module = tmp_path / LONG_RUN_CASES["no-escape"].split("::")[0]
    definitions = "import pytest\nfrom runpy import run_path\n"
    for nodeid, function, value in zip(LONG_RUN_CASES.values(), ["no_escape", "reserve", "recovery"], [3, 4, 5], strict=True):
        definitions += (
            f"@pytest.mark.landscape_long_run\ndef {nodeid.split('::')[1]}():\n"
            f"    assert run_path({str(source / 'sample.py')!r})[{function!r}]() == {value}\n"
        )
    definitions += (
        "@pytest.mark.parametrize('value', range(40))\ndef test_ordinary(value):\n"
        f"    assert run_path({str(source / 'sample.py')!r})['double'](value) == value * 2\n"
    )
    module.write_text(definitions, encoding="utf-8")
    commit = evidence_identity(tmp_path)["tested_commit"]
    assert commit is not None
    for name, value in {
        "GITHUB_ACTIONS": "true",
        "GITHUB_SHA": commit,
        "GITHUB_RUN_ID": "1234",
        "GITHUB_RUN_ATTEMPT": "2",
    }.items():
        monkeypatch.setenv(name, value)
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    producers = [("ordinary", 8, index, None) for index in range(8)] + [("proof", 1, 0, case) for case in LONG_RUN_CASES]
    for partition, count, index, case in producers:
        stem = f"ordinary-shard-{version}-{index}" if partition == "ordinary" else f"landscape-proof-{case}-{version}"
        folder = artifacts / f"{stem}-attempt-2"
        folder.mkdir()
        monkeypatch.setenv("GITHUB_JOB", "test" if partition == "ordinary" else "proof")
        monkeypatch.setenv("COVERAGE_FILE", str(folder / "coverage-data"))
        extra = [f"--cov={source}", "--cov-config=/dev/null", "--cov-report=", "--cov-fail-under=0"] if version == "3.13" else []
        result, report = _partition_run(tmp_path, partition, count, index, extra_args=extra, case=case)
        assert result.returncode == 0, result.stdout + result.stderr
        report.rename(folder / "shard-manifest.json")
        if version == "3.13":
            data = coverage.CoverageData(basename=str(folder / "coverage-data"))
            data.read()
            executed = set(data.lines(str(source / "sample.py")))
            expected = {2} if case is None else {dict(zip(LONG_RUN_CASES, [4, 6, 8], strict=True))[case]}
            assert executed & {2, 4, 6, 8} == expected
    destination = tmp_path / "aggregate"
    (destination / "src" / "elspeth").mkdir(parents=True)
    (destination / "src" / "elspeth" / "sample.py").write_text(source_text, encoding="utf-8")
    profile = destination / DURATION_PROFILE_PATH
    profile.parent.mkdir(parents=True)
    shutil.copyfile(tmp_path / DURATION_PROFILE_PATH, profile)
    resolved = validate_long_run_shards(artifacts, 8, destination, version)
    assert len(resolved) == 11
    if version == "3.13":
        data = coverage.CoverageData(basename=str(destination / ".coverage"))
        data.read()
        assert data.lines(str(destination / "src" / "elspeth" / "sample.py")) == list(range(1, 9))


def test_partitioned_cli_returns_selected_attempt_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = _long_run_artifacts(tmp_path, monkeypatch, "3.12")
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "ci_test_shards.py"),
            "--artifact-dir",
            str(artifacts),
            "--shard-count",
            "4",
            "--checkout",
            str(tmp_path),
            "--partitioned",
            "--python-version",
            "3.12",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    selected = json.loads(result.stdout)["selected_artifacts"]
    assert len(selected) == 7
    assert selected["ordinary-shard-3.12-0"] == "ordinary-shard-3.12-0-attempt-1"
    for case in LONG_RUN_CASES:
        assert selected[f"landscape-proof-{case}-3.12"] == f"landscape-proof-{case}-3.12-attempt-2"


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 2.0),
        ("shard_count", 4.0),
        ("shard_index", False),
        ("shard_index", 0.0),
        ("exit_status", False),
        ("exit_status", 0.0),
    ],
)
def test_all_mode_manifest_refuses_boolean_and_float_integer_substitutes(tmp_path: Path, field: str, value: object) -> None:
    artifacts = _artifacts(tmp_path)
    source = tmp_path / "src" / "elspeth"
    source.mkdir(parents=True)
    (source / "sample.py").write_text("value = 1\nvalue += 1\n", encoding="utf-8")
    combine_shards(artifacts, 4, tmp_path)
    (tmp_path / ".coverage").unlink()
    report = artifacts / "coverage-shard-3.13-0" / "shard-manifest.json"
    manifest = json.loads(report.read_text(encoding="utf-8"))
    manifest[field] = value
    report.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        combine_shards(artifacts, 4, tmp_path)
    assert not (tmp_path / ".coverage").exists()


def test_workflow_requires_all_shards_and_combined_unchanged_floors() -> None:
    jobs = _activation_workflow_jobs()
    matrix = jobs["test"]["strategy"]["matrix"]
    assert matrix["shard"] == list(range(8))
    assert jobs["test"]["strategy"]["max-parallel"] == 2
    assert jobs["test"]["env"]["PYTEST_WORKERS"] == "2"
    assert "matrix.shard" in jobs["test"]["env"]["CI_CHECKOUT_PATH"]
    aggregate = jobs["coverage"]
    assert aggregate["needs"] == ["test", "landscape-proof"]
    assert (
        aggregate["if"]
        == "always() && (github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository)"
    )
    commands = "\n".join(step.get("run", "") for step in aggregate["steps"])
    assert "needs.test.result" in commands
    assert "needs.landscape-proof.result" in commands
    for floor in (85, 92, 99, 90, 62):
        assert f"--fail-under={floor}" in commands
    assert "coverage" in jobs["ci-success"]["needs"]
    summary = "\n".join(step.get("run", "") for step in jobs["ci-success"]["steps"])
    assert "needs.coverage.result" in summary


def _activation_workflow_jobs() -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / "config/cicd/prepared-nyx/ci.yaml").read_text(encoding="utf-8"))["jobs"]


def _activation_step(job: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [step for step in job["steps"] if step.get("name") == name]
    assert len(matches) == 1, f"missing or duplicate activation step: {name}"
    return matches[0]


def _assert_activation_contract(jobs: dict[str, Any]) -> None:
    caller = jobs["landscape-proof"]
    assert set(caller) == {"name", "if", "uses"}
    assert caller["uses"] == "./.github/workflows/landscape-long-run.yml"
    assert caller["if"] == "github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository"
    for name in ("Run tests with coverage", "Run tests without coverage"):
        run = _activation_step(jobs["test"], name)["run"]
        tokens = shlex.split(run.replace("\\\n", " "), comments=True)
        args = tokens[tokens.index("pytest") + 1 :]
        assert args[:3] == ["tests/", "-p", "scripts.ci_test_shards"]
        assert args.count("--ci-partition=ordinary") == 1
        assert "--ci-shard-count=8" in args and "--ci-shard-index=${{ matrix.shard }}" in run
        assert "--ci-shard-report=shard-manifest.json" in args
        assert not any(arg == "-m" or arg.startswith("--markexpr") or arg in {"-o", "--override-ini"} for arg in args)
    upload = _activation_step(jobs["test"], "Upload shard coverage and selection evidence")
    assert upload["if"] == "always()"  # Both interpreters publish complete selection evidence.
    assert upload["with"]["name"] == "ordinary-shard-${{ matrix.python-version }}-${{ matrix.shard }}-attempt-${{ github.run_attempt }}"
    assert set(upload["with"]["path"].splitlines()) == {
        "${{ env.CI_CHECKOUT_PATH }}/coverage-data",
        "${{ env.CI_CHECKOUT_PATH }}/shard-manifest.json",
    }
    assert upload["with"]["if-no-files-found"] == "error"
    junit = _activation_step(jobs["test"], "Upload JUnit report")
    assert junit["if"] == "always()"
    assert junit["with"]["name"] == "pytest-junit-${{ matrix.python-version }}-${{ matrix.shard }}-attempt-${{ github.run_attempt }}"
    aggregate = jobs["coverage"]
    assert aggregate["needs"] == ["test", "landscape-proof"]
    gate = _activation_step(aggregate, "Require every test shard and exhaustive proof to pass")
    assert aggregate["steps"][0] is gate
    for producer in ("test", "landscape-proof"):
        assert f'if [ "${{{{ needs.{producer}.result }}}}" != "success" ]; then' in gate["run"]
    assert "continue-on-error" not in gate and "||" not in gate["run"]
    for version in ("3.12", "3.13"):
        for partition, pattern in (
            ("ordinary", f"ordinary-shard-{version}-*-attempt-*"),
            ("proof", f"landscape-proof-*-{version}-attempt-*"),
        ):
            download = _activation_step(aggregate, f"Download Python {version} {partition} evidence")
            assert download["uses"] == "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
            assert download["with"] == {"pattern": pattern, "path": "${{ env.CI_CHECKOUT_PATH }}/proof-evidence/" + version}
        name = (
            "Validate Python 3.12 ordinary and proof selection"
            if version == "3.12"
            else "Validate Python 3.13 selection and combine coverage"
        )
        validation = _activation_step(aggregate, name)
        assert validation["working-directory"] == "${{ env.CI_CHECKOUT_PATH }}"
        assert shlex.split(validation["run"]) == [
            "uv",
            "run",
            "python",
            "scripts/ci_test_shards.py",
            "--artifact-dir",
            f"proof-evidence/{version}",
            "--shard-count",
            "8",
            "--partitioned",
            "--python-version",
            version,
        ]
        assert "if" not in validation and "continue-on-error" not in validation
    success = jobs["ci-success"]
    assert "landscape-proof" in success["needs"] and "coverage" in success["needs"]
    assert success["if"] == "always()"
    assert 'if [[ "${{ needs.landscape-proof.result }}" != "success" ]]; then' in _activation_step(success, "Check all jobs passed")["run"]
    report = _activation_step(aggregate, "Upload combined coverage report")
    assert report["if"] == "always()"
    assert report["with"]["name"] == "coverage-report-attempt-${{ github.run_attempt }}"


def test_active_workflow_binds_ordinary_proof_and_native_aggregate_contract() -> None:
    _assert_activation_contract(_activation_workflow_jobs())
    module = ast.parse((REPO_ROOT / LONG_RUN_CASES["no-escape"].split("::")[0]).read_text(encoding="utf-8"))
    names = {nodeid.split("::")[1] for nodeid in LONG_RUN_CASES.values()}
    marked = {
        node.name
        for node in module.body
        if isinstance(node, ast.FunctionDef)
        and "pytest.mark.landscape_long_run" in [ast.unparse(decorator) for decorator in node.decorator_list]
    }
    assert marked == names


@pytest.mark.parametrize(
    "mutation",
    ["exclusion", "caller", "dropped_caller", "dependency", "native_result", "upload", "download", "version", "success_dependency"],
)
def test_activation_contract_refuses_disconnected_proof_paths(mutation: str) -> None:
    jobs = copy.deepcopy(_activation_workflow_jobs())
    if mutation == "exclusion":
        step = _activation_step(jobs["test"], "Run tests with coverage")
        step["run"] = step["run"].replace("--ci-partition=ordinary", "--ci-partition=all")
    elif mutation == "caller":
        jobs["landscape-proof"]["uses"] = "./.github/workflows/missing-proof.yml"
    elif mutation == "dropped_caller":
        del jobs["landscape-proof"]
    elif mutation == "dependency":
        jobs["coverage"]["needs"].remove("landscape-proof")
    elif mutation == "native_result":
        step = _activation_step(jobs["coverage"], "Require every test shard and exhaustive proof to pass")
        step["run"] = step["run"].replace("needs.landscape-proof.result", "needs.test.result")
    elif mutation == "upload":
        _activation_step(jobs["test"], "Upload shard coverage and selection evidence")["if"] = "matrix.coverage && always()"
    elif mutation == "download":
        jobs["coverage"]["steps"].remove(_activation_step(jobs["coverage"], "Download Python 3.12 proof evidence"))
    elif mutation == "version":
        step = _activation_step(jobs["coverage"], "Validate Python 3.12 ordinary and proof selection")
        step["run"] = step["run"].replace("--python-version 3.12", "--python-version 3.13")
    elif mutation == "success_dependency":
        jobs["ci-success"]["needs"].remove("landscape-proof")
    with pytest.raises((AssertionError, KeyError)):
        _assert_activation_contract(jobs)


@pytest.mark.parametrize(
    "job_name,step_name", [("coverage", "Require every test shard and exhaustive proof to pass"), ("ci-success", "Check all jobs passed")]
)
@pytest.mark.parametrize("proof_result", ["success", "failure", "skipped", "cancelled", "timed_out", ""])
def test_native_proof_result_must_succeed_before_evidence_or_ci_success(job_name: str, step_name: str, proof_result: str) -> None:
    job = _activation_workflow_jobs()[job_name]
    run = _activation_step(job, step_name)["run"]
    run = run.replace("${{ needs.landscape-proof.result }}", proof_result)
    for producer in job["needs"]:
        if producer != "landscape-proof":
            run = run.replace(f"${{{{ needs.{producer}.result }}}}", "success")
    assert "${{" not in run
    completed = subprocess.run(["bash", "-e", "-c", run], capture_output=True, text=True, check=False, timeout=5)
    assert completed.returncode == (0 if proof_result == "success" else 1), completed.stdout + completed.stderr


@pytest.mark.parametrize("version", ["3.12", "3.13"])
@pytest.mark.parametrize("omit_proof", [False, True])
def test_yaml_partition_validation_consumes_real_tiny_evidence_and_refuses_missing_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str, omit_proof: bool
) -> None:
    artifacts = _long_run_artifacts(tmp_path, monkeypatch, version, 8)
    root = tmp_path / "proof-evidence"
    root.mkdir()
    artifacts.rename(root / version)
    if omit_proof:
        shutil.rmtree(root / version / f"landscape-proof-no-escape-{version}-attempt-2")
    name = (
        "Validate Python 3.12 ordinary and proof selection" if version == "3.12" else "Validate Python 3.13 selection and combine coverage"
    )
    run = _activation_step(_activation_workflow_jobs()["coverage"], name)["run"]
    tokens = shlex.split(run)
    assert tokens[:4] == ["uv", "run", "python", "scripts/ci_test_shards.py"]
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / tokens[3]), *tokens[4:]],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert (completed.returncode != 0) is omit_proof, completed.stdout + completed.stderr
    if omit_proof:
        assert "Missing producer evidence" in completed.stderr
    else:
        assert len(json.loads(completed.stdout)["selected_artifacts"]) == 11
    assert (tmp_path / ".coverage").exists() is (version == "3.13" and not omit_proof)
