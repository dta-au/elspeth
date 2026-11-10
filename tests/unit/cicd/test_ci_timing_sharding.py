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
    report.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
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
