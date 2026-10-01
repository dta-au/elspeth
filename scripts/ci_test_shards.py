"""Partition pytest's selected node IDs and combine complete CI coverage evidence.

Load with ``python -m pytest -p scripts.ci_test_shards --ci-shard-count 4
--ci-shard-index 0 --ci-shard-report shard-manifest.json``. Marker selection
remains owned by pytest; the plugin partitions only the remaining items.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path

import coverage
import pytest


def shard_index(nodeid: str, count: int) -> int:
    """Assign a node independently of collection order or Python hash seed."""
    if count < 1:
        raise ValueError("Shard count must be positive")
    return int.from_bytes(hashlib.sha256(nodeid.encode("utf-8")).digest(), "big") % count


def collection_digest(nodeids: list[str]) -> str:
    encoded = json.dumps(sorted(nodeids), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("ci-shards")
    group.addoption("--ci-shard-count", type=int)
    group.addoption("--ci-shard-index", type=int)
    group.addoption("--ci-shard-report", type=Path)


def pytest_configure(config: pytest.Config) -> None:
    count = config.getoption("ci_shard_count")
    index = config.getoption("ci_shard_index")
    report = config.getoption("ci_shard_report")
    if count is None and index is None and report is None:
        return
    if count is None or index is None or report is None or count < 1 or not 0 <= index < count:
        raise pytest.UsageError("Supply a positive --ci-shard-count, an index in range, and --ci-shard-report")


def pytest_sessionstart(session: pytest.Session) -> None:
    report = session.config.getoption("ci_shard_report")
    if report is not None and "PYTEST_XDIST_WORKER" not in os.environ:
        report.unlink(missing_ok=True)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> Iterator[None]:
    # Wrap all collection hooks, including the project's trylast safety guards.
    # The outgoing leg observes their final marker-selected collection.
    yield
    count = config.getoption("ci_shard_count")
    if count is None:
        return
    index = config.getoption("ci_shard_index")
    report = config.getoption("ci_shard_report")
    digest = collection_digest([item.nodeid for item in items])
    selected = [item for item in items if shard_index(item.nodeid, count) == index]
    deselected = [item for item in items if shard_index(item.nodeid, count) != index]
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)
    # Workers share the report path and xdist verifies identical collections.
    # Atomic replacement prevents concurrent worker collection writes tearing it.
    temporary = report.with_name(f"{report.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "shard_count": count,
                "shard_index": index,
                "collection_digest": digest,
                "selected_nodeids": [item.nodeid for item in selected],
                "exit_status": None,
            }
        ),
        encoding="utf-8",
    )
    temporary.replace(report)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> Iterator[None]:
    # pytest-cov finalizes its database on the inner sessionfinish leg.
    yield
    report = session.config.getoption("ci_shard_report")
    if report is None or "PYTEST_XDIST_WORKER" in os.environ or not report.exists():
        return
    manifest = json.loads(report.read_text(encoding="utf-8"))
    manifest["exit_status"] = int(exitstatus)
    if session.config.getoption("cov_source", default=None):
        database = Path(os.environ.get("COVERAGE_FILE", ".coverage"))
        with database.open("rb") as stream:
            manifest["coverage_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    report.write_text(json.dumps(manifest), encoding="utf-8")


def combine_shards(artifact_dir: Path, count: int, checkout: Path) -> None:
    """Combine only a complete, successful, disjoint partition of one collection."""
    selected: list[str] = []
    digests: set[str] = set()
    databases: list[str] = []
    for index in range(count):
        folder = artifact_dir / f"coverage-shard-3.13-{index}"
        manifest = json.loads((folder / "shard-manifest.json").read_text(encoding="utf-8"))
        if manifest["shard_count"] != count or manifest["shard_index"] != index or manifest["exit_status"] != 0:
            raise ValueError(f"Shard {index} did not complete successfully with the expected identity")
        nodeids = manifest["selected_nodeids"]
        if not nodeids or any(shard_index(nodeid, count) != index for nodeid in nodeids):
            raise ValueError(f"Shard {index} has an empty or misassigned collection")
        selected.extend(nodeids)
        digests.add(manifest["collection_digest"])
        database = folder / "coverage-data"
        if not database.is_file():
            raise FileNotFoundError(database)
        with database.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if "coverage_sha256" not in manifest or manifest["coverage_sha256"] != digest:
            raise ValueError(f"Shard {index} coverage database differs from its finalized test evidence")
        shard_data = coverage.CoverageData(basename=str(database))
        shard_data.read()
        if not any(shard_data.lines(filename) for filename in shard_data.measured_files()):
            raise ValueError(f"Shard {index} coverage database has no executed source lines")
        databases.append(str(database.resolve()))
    if len(selected) != len(set(selected)) or digests != {collection_digest(selected)}:
        raise ValueError("Shard collections overlap, differ, or omit selected tests")
    # Coverage's path aliases map each per-job absolute checkout to this checkout.
    cov = coverage.Coverage(data_file=str(checkout / ".coverage"), config_file=False)
    cov.set_option("paths", {"source": [str(checkout / "src" / "elspeth"), "*/src/elspeth"]})
    cov.combine(data_paths=databases, strict=True, keep=True)
    data = cov.get_data()
    source_root = (checkout / "src" / "elspeth").resolve()
    files = data.measured_files()
    if not files or any(not Path(filename).resolve().is_relative_to(source_root) for filename in files):
        raise ValueError("Combined coverage contains no source files or files outside this checkout's src/elspeth")
    if not any(data.lines(filename) for filename in files):
        raise ValueError("Combined coverage contains no executed ELSPETH source lines")
    cov.save()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--checkout", type=Path, default=Path.cwd())
    args = parser.parse_args()
    combine_shards(args.artifact_dir, args.shard_count, args.checkout)


if __name__ == "__main__":
    main()
