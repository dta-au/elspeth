"""The sharded suite preserves selection and refuses incomplete coverage evidence."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import coverage
import pytest
import yaml
from scripts.ci_test_shards import collection_digest, combine_shards, shard_index

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_shards_are_stable_disjoint_and_complete() -> None:
    nodeids = [f"tests/example.py::test_example[{index}]" for index in range(1000)]
    groups = [{nodeid for nodeid in nodeids if shard_index(nodeid, 4) == index} for index in range(4)]
    assert all(groups)
    assert set.union(*groups) == set(nodeids)
    assert sum(map(len, groups)) == len(nodeids)
    assert shard_index("tests/example.py::test_example[0]", 4) == 3
    assert [shard_index(nodeid, 4) for nodeid in reversed(nodeids)] == list(reversed([shard_index(nodeid, 4) for nodeid in nodeids]))


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
        assert manifest["exit_status"] == 0
        assert all("test_protected" not in nodeid for nodeid in manifest["selected_nodeids"])
        selected.extend(manifest["selected_nodeids"])
    assert len(selected) == len(set(selected)) == 40


def _artifacts(tmp_path: Path) -> Path:
    nodeids = [f"test_sample.py::test_example[{index}]" for index in range(100)]
    artifacts = tmp_path / "artifacts"
    for index in range(4):
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
                    "shard_count": 4,
                    "shard_index": index,
                    "collection_digest": collection_digest(nodeids),
                    "selected_nodeids": [nodeid for nodeid in nodeids if shard_index(nodeid, 4) == index],
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


def test_workflow_requires_all_shards_and_combined_unchanged_floors() -> None:
    jobs = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yaml").read_text(encoding="utf-8"))["jobs"]
    matrix = jobs["test"]["strategy"]["matrix"]
    assert matrix["shard"] == [0, 1, 2, 3]
    assert jobs["test"]["env"]["PYTEST_WORKERS"] == "2"
    assert "matrix.shard" in jobs["test"]["env"]["CI_CHECKOUT_PATH"]
    aggregate = jobs["coverage"]
    assert aggregate["needs"] == ["test"]
    assert aggregate["if"] == "always()"
    assert "if" not in jobs["test"]
    # Main's public PRs keep the same hosted routing as every existing CI job.
    assert jobs["test"]["runs-on"] == jobs["static-analysis"]["runs-on"] == aggregate["runs-on"]
    assert "github.event_name != 'pull_request'" in aggregate["runs-on"]
    assert "ubuntu-24.04" in aggregate["runs-on"]
    commands = "\n".join(step.get("run", "") for step in aggregate["steps"])
    assert "needs.test.result" in commands
    for floor in (85, 92, 99, 90, 62):
        assert f"--fail-under={floor}" in commands
    assert "coverage" in jobs["ci-success"]["needs"]
    summary = "\n".join(step.get("run", "") for step in jobs["ci-success"]["steps"])
    assert "needs.coverage.result" in summary
