"""Serial timing collection and its coverage evidence fail closed independently."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import coverage
import pytest
from scripts.ci_test_shards import (
    LONG_RUN_CASES,
    build_timing_manifest,
    collection_digest,
    evidence_identity,
    finalize_long_run_manifest,
    validate_long_run_shards,
)
from tests.unit.cicd.test_ci_sharding import _init_checkout, _long_run_artifacts, _partition_run, _partition_suite


@pytest.fixture(autouse=True)
def _local_producer_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GITHUB_ACTIONS", "GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("nodeids", "marked"),
    [
        ([], []),
        (["test_tiny.py::test_timing"], []),
        (["test_tiny.py::test_timing", "test_tiny.py::test_unmarked"], ["test_tiny.py::test_timing"]),
        (["test_tiny.py::test_timing"] * 2, ["test_tiny.py::test_timing"] * 2),
        ([LONG_RUN_CASES["no-escape"]], [LONG_RUN_CASES["no-escape"]]),
    ],
    ids=["empty", "unmarked", "partly-marked", "duplicate", "proof-overlap"],
)
def test_timing_manifest_requires_nonempty_unique_marked_nonproof_collection(tmp_path: Path, nodeids: list[str], marked: list[str]) -> None:
    _init_checkout(tmp_path)
    positive = ["test_tiny.py::test_timing"]
    manifest = build_timing_manifest(tmp_path, positive, positive)
    assert manifest["selected_nodeids"] == manifest["timing_marked_nodeids"] == positive
    assert manifest["collection_digest"] == collection_digest(positive)
    with pytest.raises(ValueError):
        build_timing_manifest(tmp_path, nodeids, marked)


def test_timing_manifest_binds_native_producer_role(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commit = _init_checkout(tmp_path)
    for name, value in {
        "GITHUB_ACTIONS": "true",
        "GITHUB_SHA": commit,
        "GITHUB_RUN_ID": "1234",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_JOB": "timing-sensitive",
    }.items():
        monkeypatch.setenv(name, value)
    nodeids = ["test_tiny.py::test_timing"]
    manifest = build_timing_manifest(tmp_path, nodeids, nodeids)
    assert manifest["producer_job"] == "timing-sensitive"
    assert manifest["tested_commit"] == evidence_identity(tmp_path)["tested_commit"]
    monkeypatch.setenv("GITHUB_JOB", "test")
    with pytest.raises(ValueError, match="producer role"):
        build_timing_manifest(tmp_path, nodeids, nodeids)


def _tiny_timing_suite(tmp_path: Path) -> None:
    _partition_suite(tmp_path)
    (tmp_path / "tests" / "test_tiny_timing.py").write_text(
        "import pytest\n"
        "@pytest.mark.timing_sensitive\ndef test_timing(): assert True\n"
        "@pytest.mark.timing_sensitive\n@pytest.mark.slow\ndef test_slow(): assert False\n"
        "def test_unmarked(): assert False\n",
        encoding="utf-8",
    )
    with (tmp_path / "pytest.ini").open("a", encoding="utf-8") as stream:
        stream.write("    timing_sensitive: serial timing test\n    slow: excluded control\n")


def test_timing_partition_records_real_marker_selection_and_success(tmp_path: Path) -> None:
    _tiny_timing_suite(tmp_path)
    result, report = _partition_run(tmp_path, "timing", extra_args=["-m", "timing_sensitive and not slow"])
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads(report.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 4
    assert manifest["partition"] == "timing"
    assert manifest["selected_nodeids"] == manifest["timing_marked_nodeids"] == ["tests/test_tiny_timing.py::test_timing"]
    assert manifest["exit_status"] == 0
    assert manifest["shard_count"] == 1
    assert manifest["shard_index"] == 0
    assert "proof_call_passed" not in manifest
    assert manifest["timing_call_outcomes"] == {"tests/test_tiny_timing.py::test_timing": "passed"}
    assert manifest["timing_passed_count"] == 1


@pytest.mark.parametrize(
    ("control", "body", "expected", "exit_status"),
    [
        ("", "pytest.skip('controlled skip')", "skipped", 0),
        ("", "pytest.xfail('controlled xfail')", "xfailed", 0),
        ("@pytest.mark.xfail(reason='controlled xpass', strict=False)\n", "assert True", "xpassed", 0),
        ("", "assert False", "failed", 1),
        ("@pytest.mark.skip(reason='controlled setup skip')\n", "assert False", None, 0),
    ],
    ids=["call-skip", "xfail", "xpass", "failed", "setup-skip"],
)
def test_timing_partition_records_only_call_outcomes(
    tmp_path: Path, control: str, body: str, expected: str | None, exit_status: int
) -> None:
    _tiny_timing_suite(tmp_path)
    (tmp_path / "tests" / "test_tiny_timing.py").write_text(
        "import pytest\n"
        "@pytest.mark.timing_sensitive\ndef test_pass(): assert True\n"
        f"@pytest.mark.timing_sensitive\n{control}def test_control(): {body}\n",
        encoding="utf-8",
    )
    result, report = _partition_run(tmp_path, "timing", extra_args=["-m", "timing_sensitive"])
    assert result.returncode == exit_status, result.stdout + result.stderr
    manifest = json.loads(report.read_text(encoding="utf-8"))
    outcomes = {"tests/test_tiny_timing.py::test_pass": "passed"}
    if expected is not None:
        outcomes["tests/test_tiny_timing.py::test_control"] = expected
    assert manifest["timing_call_outcomes"] == outcomes
    assert manifest["timing_passed_count"] == 1
    assert manifest["exit_status"] == exit_status


def test_timing_finalizer_copies_call_evidence_without_mutating_collection_or_state(tmp_path: Path) -> None:
    _init_checkout(tmp_path)
    nodeids = ["test_tiny.py::test_pass", "test_tiny.py::test_skip"]
    manifest = build_timing_manifest(tmp_path, nodeids, nodeids)
    outcomes = dict(zip(nodeids, ["passed", "skipped"], strict=True))
    terminal = finalize_long_run_manifest(manifest, 0, None, timing_call_outcomes=outcomes)
    assert terminal["timing_call_outcomes"] == outcomes
    assert terminal["timing_passed_count"] == 1
    assert terminal["exit_status"] == 0
    assert manifest["exit_status"] is None
    assert "timing_call_outcomes" not in manifest
    outcomes.clear()
    assert terminal["timing_call_outcomes"] == dict(zip(nodeids, ["passed", "skipped"], strict=True))
    with pytest.raises(ValueError, match="unfinalized"):
        finalize_long_run_manifest(terminal, 0, None)


@pytest.mark.parametrize("collect_only", [False, True], ids=["all-skipped", "collect-only"])
def test_timing_real_no_pass_evidence_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collect_only: bool) -> None:
    _tiny_timing_suite(tmp_path)
    (tmp_path / "tests" / "test_tiny_timing.py").write_text(
        "import pytest\n@pytest.mark.timing_sensitive\ndef test_timing(): pytest.skip('controlled call skip')\n",
        encoding="utf-8",
    )
    result, report = _partition_run(
        tmp_path, "timing", extra_args=["-m", "timing_sensitive", *(["--collect-only"] if collect_only else [])]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads(report.read_text(encoding="utf-8"))
    nodeids = ["tests/test_tiny_timing.py::test_timing"]
    assert manifest["selected_nodeids"] == nodeids
    artifacts = _long_run_artifacts(tmp_path, monkeypatch, "3.12")
    validate_long_run_shards(artifacts, 4, tmp_path, "3.12")
    target = artifacts / "timing-shard-3.12-attempt-2" / "shard-manifest.json"
    # Supply only the native producer envelope from the artifact fixture. The
    # collection and call evidence must come from the real subprocess above.
    envelope = json.loads(target.read_text(encoding="utf-8"))
    for field in ("tested_commit", "duration_profile_sha256", "python_version", "github_run_id", "github_run_attempt", "producer_job"):
        manifest[field] = envelope[field]
    positive = {**manifest, "timing_call_outcomes": dict.fromkeys(nodeids, "passed"), "timing_passed_count": 1}
    target.write_text(json.dumps(positive), encoding="utf-8")
    validate_long_run_shards(artifacts, 4, tmp_path, "3.12")
    target.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="timing call"):
        validate_long_run_shards(artifacts, 4, tmp_path, "3.12")
    assert manifest["timing_call_outcomes"] == ({} if collect_only else {nodeids[0]: "skipped"})
    assert manifest["timing_passed_count"] == 0


@pytest.mark.parametrize("version", ["3.12", "3.13"])
@pytest.mark.parametrize(
    "mutation",
    [
        "all-skipped",
        "missing-one",
        "zero-passed",
        "collect-only",
        "missing-outcomes",
        "missing-count",
        "extra-id",
        "invalid-outcome",
        "bool-count",
    ],
)
def test_timing_consumer_requires_complete_calls_and_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str, mutation: str
) -> None:
    artifacts = _long_run_artifacts(tmp_path, monkeypatch, version)
    report = artifacts / f"timing-shard-{version}-attempt-2" / "shard-manifest.json"
    manifest = json.loads(report.read_text(encoding="utf-8"))
    nodeids = ["test_sample.py::test_timing", "test_sample.py::test_other_timing"]
    manifest.update(
        selected_nodeids=nodeids,
        timing_marked_nodeids=nodeids,
        collection_digest=collection_digest(nodeids),
        timing_call_outcomes={nodeids[0]: "passed", nodeids[1]: "skipped"},
        timing_passed_count=1,
    )
    report.write_text(json.dumps(manifest), encoding="utf-8")
    # An individual skipped call is valid alongside a passing call.
    validate_long_run_shards(artifacts, 4, tmp_path, version)
    if mutation == "all-skipped":
        manifest["timing_call_outcomes"] = dict.fromkeys(nodeids, "skipped")
        manifest["timing_passed_count"] = 0
    elif mutation == "missing-one":
        del manifest["timing_call_outcomes"][nodeids[1]]
    elif mutation == "zero-passed":
        manifest["timing_passed_count"] = 0
    elif mutation == "collect-only":
        manifest["timing_call_outcomes"] = {}
        manifest["timing_passed_count"] = 0
    elif mutation == "missing-outcomes":
        del manifest["timing_call_outcomes"]
    elif mutation == "missing-count":
        del manifest["timing_passed_count"]
    elif mutation == "extra-id":
        manifest["timing_call_outcomes"]["test_sample.py::test_foreign"] = "passed"
        manifest["timing_passed_count"] = 2
    elif mutation == "invalid-outcome":
        manifest["timing_call_outcomes"][nodeids[1]] = "unknown"
    else:
        manifest["timing_passed_count"] = True
    report.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="timing call"):
        validate_long_run_shards(artifacts, 4, tmp_path, version)


@pytest.mark.parametrize(("count", "workers"), [(2, "0"), (1, "1"), (1, "2")])
def test_timing_partition_refuses_multiple_shards_or_workers(tmp_path: Path, count: int, workers: str) -> None:
    _tiny_timing_suite(tmp_path)
    result, report = _partition_run(tmp_path, "timing", count=count, workers=workers, extra_args=["-m", "timing_sensitive and not slow"])
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    assert "serial pytest execution" in result.stdout + result.stderr
    assert not report.exists()


@pytest.mark.parametrize("selection", ["timing_sensitive and slow and not slow", "not timing_sensitive"])
def test_timing_partition_refuses_empty_or_unmarked_real_collection(tmp_path: Path, selection: str) -> None:
    _tiny_timing_suite(tmp_path)
    result, _report = _partition_run(tmp_path, "timing", extra_args=["-m", selection])
    assert result.returncode != 0, result.stdout + result.stderr
    assert "Timing evidence requires" in result.stdout + result.stderr


@pytest.mark.parametrize("version", ["3.12", "3.13"])
def test_timing_consumer_requires_its_producer_and_includes_covered_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    artifacts = _long_run_artifacts(tmp_path, monkeypatch, version)
    resolved = validate_long_run_shards(artifacts, 4, tmp_path, version)
    assert resolved[f"timing-shard-{version}"] == f"timing-shard-{version}-attempt-2"
    if version == "3.13":
        data = coverage.CoverageData(basename=str(tmp_path / ".coverage"))
        data.read()
        assert data.lines(str(tmp_path / "src" / "elspeth" / "sample.py")) == [1, 2, 3, 4, 5, 6]
    shutil.rmtree(artifacts / resolved[f"timing-shard-{version}"])
    with pytest.raises(ValueError, match="Missing producer evidence"):
        validate_long_run_shards(artifacts, 4, tmp_path, version)


@pytest.mark.parametrize("version", ["3.12", "3.13"])
@pytest.mark.parametrize(
    "mutation",
    ["sha", "run", "attempt", "python", "role", "empty", "markers", "missing-markers", "duplicate", "overlap", "digest"],
)
def test_timing_consumer_refuses_invalid_identity_collection_or_marker_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str, mutation: str
) -> None:
    artifacts = _long_run_artifacts(tmp_path, monkeypatch, version)
    validate_long_run_shards(artifacts, 4, tmp_path, version)
    report = artifacts / f"timing-shard-{version}-attempt-2" / "shard-manifest.json"
    manifest = json.loads(report.read_text(encoding="utf-8"))
    identity_changes = {
        "sha": ("tested_commit", "0" * 40),
        "run": ("github_run_id", "9999"),
        "attempt": ("github_run_attempt", "1"),
        "python": ("python_version", "3.99"),
        "role": ("producer_job", "test"),
        "digest": ("collection_digest", "0" * 64),
    }
    if mutation in identity_changes:
        field, value = identity_changes[mutation]
        manifest[field] = value
    elif mutation == "missing-markers":
        del manifest["timing_marked_nodeids"]
    elif mutation == "markers":
        manifest["timing_marked_nodeids"] = []
    else:
        if mutation == "empty":
            nodeids = []
        elif mutation == "duplicate":
            nodeids = manifest["selected_nodeids"] * 2
        else:
            ordinary = artifacts / f"ordinary-shard-{version}-0-attempt-1" / "shard-manifest.json"
            nodeids = json.loads(ordinary.read_text(encoding="utf-8"))["selected_nodeids"][:1]
        manifest["selected_nodeids"] = manifest["timing_marked_nodeids"] = nodeids
        manifest["collection_digest"] = collection_digest(nodeids)
        if mutation == "overlap":
            manifest["timing_call_outcomes"] = dict.fromkeys(nodeids, "passed")
            manifest["timing_passed_count"] = len(nodeids)
    report.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Timing collection overlaps" if mutation == "overlap" else None):
        validate_long_run_shards(artifacts, 4, tmp_path, version)


@pytest.mark.parametrize("mutation", ["missing", "hash", "tampered", "empty", "foreign"])
def test_timing_consumer_verifies_its_coverage_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    artifacts = _long_run_artifacts(tmp_path, monkeypatch, "3.13")
    validate_long_run_shards(artifacts, 4, tmp_path, "3.13")
    folder = artifacts / "timing-shard-3.13-attempt-2"
    database = folder / "coverage-data"
    report = folder / "shard-manifest.json"
    manifest = json.loads(report.read_text(encoding="utf-8"))
    if mutation == "missing":
        database.unlink()
    elif mutation == "hash":
        manifest["coverage_sha256"] = "0" * 64
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


@pytest.mark.parametrize("selected_damage", ["truncated", "missing"])
def test_timing_latest_attempt_supersedes_bad_older_evidence_without_falling_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selected_damage: str
) -> None:
    artifacts = _long_run_artifacts(tmp_path, monkeypatch, "3.12")
    latest = artifacts / "timing-shard-3.12-attempt-2"
    older = artifacts / "timing-shard-3.12-attempt-1"
    shutil.copytree(latest, older)
    (older / "shard-manifest.json").write_text("{", encoding="utf-8")
    resolved = validate_long_run_shards(artifacts, 4, tmp_path, "3.12")
    assert resolved["timing-shard-3.12"] == latest.name
    report = latest / "shard-manifest.json"
    if selected_damage == "truncated":
        report.write_text("{", encoding="utf-8")
    else:
        report.unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        validate_long_run_shards(artifacts, 4, tmp_path, "3.12")
