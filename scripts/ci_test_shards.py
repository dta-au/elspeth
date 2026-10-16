"""Partition pytest's selected node IDs and combine complete CI coverage evidence.

Load with ``python -m pytest -p scripts.ci_test_shards --ci-shard-count 4
--ci-shard-index 0 --ci-shard-report shard-manifest.json``. Marker selection
remains owned by pytest; the plugin partitions only the remaining items.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import os
import subprocess
import sys
from collections.abc import Generator, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import coverage
import pytest
from pluggy import Result

PROOF_MARKER = "landscape_long_run"
ASSIGNMENT_ALGORITHM = "duration-lpt-v1"
DURATION_PROFILE_PATH = Path("config/cicd/pytest-duration-profile.json")
_PROOF_CALL_PASSED = pytest.StashKey[bool | None]()
LONG_RUN_CASES = {
    "no-escape": (
        "tests/unit/architecture/test_web_landscape_mutation_fencing.py::test_no_mutation_alias_wrapper_dynamic_or_raw_write_escape_exists"
    ),
    "reserve-actual-sources": (
        "tests/unit/architecture/test_web_landscape_mutation_fencing.py::test_required_work_reserve_admission_on_actual_sources"
    ),
    "protected-formal-recovery": (
        "tests/unit/architecture/test_web_landscape_mutation_fencing.py::test_required_work_reserve_rejects_protected_instance_formal_recovery"
    ),
}


def shard_index(nodeid: str, count: int) -> int:
    """Assign a node independently of collection order or Python hash seed."""
    if count < 1:
        raise ValueError("Shard count must be positive")
    return int.from_bytes(hashlib.sha256(nodeid.encode("utf-8")).digest(), "big") % count


def collection_digest(nodeids: list[str]) -> str:
    encoded = json.dumps(sorted(nodeids), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class DurationProfile:
    default_weight_ms: int
    weights_ms: Mapping[str, int]
    sha256: str


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate duration-profile key: {key}")
        result[key] = value
    return result


def load_duration_profile(checkout: Path) -> DurationProfile:
    """Read the checked-out estimate; it never defines collection membership."""
    raw = (checkout / DURATION_PROFILE_PATH).read_bytes()
    value = json.loads(raw, object_pairs_hook=_unique_json_object)
    if not isinstance(value, dict) or set(value) != {"schema_version", "default_weight_ms", "weights_ms", "source"}:
        raise ValueError("Invalid duration profile shape")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1 or not isinstance(value["source"], dict):
        raise ValueError("Invalid duration profile identity")
    default = value["default_weight_ms"]
    weights = value["weights_ms"]
    if type(default) is not int or default < 1 or not isinstance(weights, dict):
        raise ValueError("Duration weights must be positive literal integers")
    if any(not isinstance(node, str) or not node or type(weight) is not int or weight < 1 for node, weight in weights.items()):
        raise ValueError("Duration weights require nonempty node IDs and positive literal integers")
    return DurationProfile(default, MappingProxyType(weights), hashlib.sha256(raw).hexdigest())


def ordinary_assignment(nodeids: list[str], count: int, profile: DurationProfile) -> dict[str, int]:
    """Deterministic longest-estimated-first scheduling over native IDs only."""
    if type(count) is not int or count < 1 or len(nodeids) != len(set(nodeids)):
        raise ValueError("Assignment requires a positive literal shard count and unique node IDs")
    loads = [(0, index) for index in range(count)]
    heapq.heapify(loads)
    assignment: dict[str, int] = {}
    for node in sorted(nodeids, key=lambda node: (-profile.weights_ms.get(node, profile.default_weight_ms), node)):
        load, index = heapq.heappop(loads)
        assignment[node] = index
        heapq.heappush(loads, (load + profile.weights_ms.get(node, profile.default_weight_ms), index))
    return assignment


def select_long_run_nodeids(
    nodeids: list[str],
    marked_nodeids: list[str],
    partition: str,
    case: str | None,
    count: int,
    index: int,
    profile: DurationProfile | None = None,
) -> list[str]:
    """Select ordinary buckets or one of the three exact long-run cases.

    The caller retains collection_digest(nodeids) over the full normal collection.
    All catalog cases must occur and be marked exactly once before partitioning.
    """
    if type(count) is not int or type(index) is not int or count < 1 or not 0 <= index < count:
        raise ValueError("Supply integer shard count/index in range")
    if partition not in {"ordinary", "proof"}:
        raise ValueError("Long-run selection requires an ordinary or proof partition")
    if (partition == "ordinary" and case is not None) or (partition == "proof" and case not in LONG_RUN_CASES):
        raise ValueError("Supply a known case only for the proof partition")
    if partition == "proof" and (count != 1 or index != 0):
        raise ValueError("Each long-run case requires one shard at index zero")
    collected = set(nodeids)
    cases = set(LONG_RUN_CASES.values())
    if len(nodeids) != len(collected):
        raise ValueError("Long-run selection requires unique collection node IDs")
    if not cases <= collected or set(marked_nodeids) != cases or len(marked_nodeids) != len(cases):
        raise ValueError("Long-run selection requires exactly the three catalog cases and their markers")
    if partition == "proof":
        assert case is not None
        return [LONG_RUN_CASES[case]]
    if profile is None:
        profile = load_duration_profile(Path(__file__).resolve().parents[1])
    ordinary = [nodeid for nodeid in nodeids if nodeid not in cases]
    assignment = ordinary_assignment(ordinary, count, profile)
    return [nodeid for nodeid in ordinary if assignment[nodeid] == index]


def evidence_identity(checkout: Path) -> dict[str, str | None]:
    """Bind evidence to the checked-out commit and native Actions producer run."""
    result = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=checkout, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ValueError("Shard evidence requires a checkout with a verified Git HEAD")
    commit = result.stdout.strip()
    run_id = attempt = None
    if os.environ.get("GITHUB_ACTIONS") == "true":
        if os.environ.get("GITHUB_SHA") != commit:
            raise ValueError("Checked-out Git HEAD differs from the expected GITHUB_SHA")
        run_id = os.environ.get("GITHUB_RUN_ID")
        attempt = os.environ.get("GITHUB_RUN_ATTEMPT")
        if run_id is None or not run_id.isdecimal() or int(run_id) < 1:
            raise ValueError("Shard evidence requires the native GITHUB_RUN_ID")
        if attempt is None or not attempt.isdecimal() or int(attempt) < 1:
            raise ValueError("Shard evidence requires the native GITHUB_RUN_ATTEMPT")
    return {"tested_commit": commit, "github_run_id": run_id, "github_run_attempt": attempt}


def build_long_run_manifest(
    checkout: Path, nodeids: list[str], marked_nodeids: list[str], partition: str, case: str | None, count: int, index: int
) -> dict[str, object]:
    """Build schema-4 evidence from the complete marker-selected collection."""
    profile = load_duration_profile(checkout)
    selected = select_long_run_nodeids(nodeids, marked_nodeids, partition, case, count, index, profile)
    identity = evidence_identity(checkout)
    producer = os.environ.get("GITHUB_JOB") if identity["github_run_id"] is not None else None
    if identity["github_run_id"] is not None and producer != ("test" if partition == "ordinary" else "proof"):
        raise ValueError("Long-run evidence requires the exact native producer role")
    return {
        "schema_version": 4,
        "assignment_algorithm": ASSIGNMENT_ALGORITHM,
        "duration_profile_sha256": profile.sha256,
        **identity,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "producer_job": producer,
        "partition": partition,
        "proof_case": case,
        "shard_count": count,
        "shard_index": index,
        "collection_digest": collection_digest(nodeids),
        "selected_nodeids": selected,
        "exit_status": None,
    }


def long_run_call_result(case: str, nodeid: str, phase: str, passed: bool, xfail_marked: bool, previous: bool | None) -> bool | None:
    """Retain one exact call result, with failure/refusal absorbing later calls."""
    if case not in LONG_RUN_CASES or phase not in {"setup", "call", "teardown"}:
        raise ValueError("Supply a known long-run case and report phase")
    if type(passed) is not bool or type(xfail_marked) is not bool or (previous is not None and type(previous) is not bool):
        raise ValueError("Call admission requires literal boolean report/state fields")
    if phase != "call":
        return previous
    return previous is None and nodeid == LONG_RUN_CASES[case] and passed and not xfail_marked


def finalize_long_run_manifest(
    manifest: dict[str, object], exit_status: int, proof_call_passed: bool | None, coverage_database: Path | None = None
) -> dict[str, object]:
    """Copy owned collection evidence after coverage closes, before publication.

    The hook applies the effective exit to native session status before publishing
    this copy with _publish_manifest.
    """
    if type(exit_status) is not int or exit_status < 0:
        raise ValueError("Terminal evidence requires a nonnegative literal integer exit")
    if proof_call_passed is not None and type(proof_call_passed) is not bool:
        raise ValueError("Terminal evidence requires a literal boolean call result")
    if (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 4
        or manifest["exit_status"] is not None
        or "proof_call_passed" in manifest
        or "coverage_sha256" in manifest
    ):
        raise ValueError("Finalize only unfinalized schema-4 collection evidence")
    partition = manifest["partition"]
    case = manifest["proof_case"]
    if partition == "ordinary":
        if case is not None or proof_call_passed is not None:
            raise ValueError("Ordinary evidence cannot carry a proof case/call result")
    elif partition == "proof":
        if not isinstance(case, str) or case not in LONG_RUN_CASES or manifest["selected_nodeids"] != [LONG_RUN_CASES[case]]:
            raise ValueError("Proof evidence requires its exact catalog singleton")
    else:
        raise ValueError("Terminal evidence requires an ordinary or proof partition")
    terminal = {**manifest, "exit_status": exit_status}
    if partition == "proof":
        terminal["proof_call_passed"] = proof_call_passed is True
        if proof_call_passed is not True and exit_status == 0:
            terminal["exit_status"] = 1
    if coverage_database is not None:
        try:
            with coverage_database.open("rb") as stream:
                terminal["coverage_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
        except FileNotFoundError:
            # Collection can fail before coverage starts. Publish that failure
            # without claiming coverage; successful evidence still requires it.
            if terminal["exit_status"] == 0:
                raise
    return terminal


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("ci-shards")
    group.addoption("--ci-shard-count", type=int)
    group.addoption("--ci-shard-index", type=int)
    group.addoption("--ci-shard-report", type=Path)
    group.addoption("--ci-partition", choices=("all", "ordinary", "proof"), default="all")
    group.addoption("--ci-proof-case", choices=tuple(LONG_RUN_CASES))


def pytest_configure(config: pytest.Config) -> None:
    count = config.getoption("ci_shard_count")
    index = config.getoption("ci_shard_index")
    report = config.getoption("ci_shard_report")
    partition = config.getoption("ci_partition")
    case = config.getoption("ci_proof_case")
    if (partition == "proof" and case not in LONG_RUN_CASES) or (partition != "proof" and case is not None):
        raise pytest.UsageError("Supply --ci-proof-case only and always for the proof partition")
    if count is None and index is None and report is None and partition == "all":
        return
    if count is None or index is None or report is None or count < 1 or not 0 <= index < count:
        raise pytest.UsageError("Supply a positive --ci-shard-count, an index in range, and --ci-shard-report")
    if partition == "proof" and (count != 1 or index != 0 or config.getoption("numprocesses", default=None) not in (None, 0)):
        raise pytest.UsageError("The proof partition requires one shard at index zero and serial pytest execution")


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
    partition = config.getoption("ci_partition")
    try:
        if partition == "all":
            identity = evidence_identity(Path.cwd())
            producer_job = os.environ.get("GITHUB_JOB") if os.environ.get("GITHUB_ACTIONS") == "true" else None
            if os.environ.get("GITHUB_ACTIONS") == "true" and not producer_job:
                raise ValueError("Shard evidence requires the native GITHUB_JOB")
            selected = [item for item in items if shard_index(item.nodeid, count) == index]
            manifest = {
                "schema_version": 2,
                **identity,
                "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
                "producer_job": producer_job,
                "partition": partition,
                "shard_count": count,
                "shard_index": index,
                "collection_digest": collection_digest([item.nodeid for item in items]),
                "selected_nodeids": [item.nodeid for item in selected],
                "exit_status": None,
            }
        else:
            manifest = build_long_run_manifest(
                Path.cwd(),
                [item.nodeid for item in items],
                [item.nodeid for item in items if list(item.iter_markers(name=PROOF_MARKER))],
                partition,
                config.getoption("ci_proof_case"),
                count,
                index,
            )
            partition_nodeids = manifest["selected_nodeids"]
            assert isinstance(partition_nodeids, list)
            selected = [item for item in items if item.nodeid in partition_nodeids]
    except ValueError as error:
        raise pytest.UsageError(str(error)) from error
    selected_nodeids = {item.nodeid for item in selected}
    deselected = [item for item in items if item.nodeid not in selected_nodeids]
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)
    # Workers share the report path and xdist verifies identical collections.
    # Atomic replacement prevents concurrent worker collection writes tearing it.
    _publish_manifest(report, manifest)


def _publish_manifest(report: Path, manifest: dict[str, object]) -> None:
    """Publish complete JSON without exposing an interrupted temporary write."""
    temporary = report.with_name(f"{report.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(manifest), encoding="utf-8")
    temporary.replace(report)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if item.config.getoption("ci_partition") == "proof":
        item.config.stash[_PROOF_CALL_PASSED] = long_run_call_result(
            item.config.getoption("ci_proof_case"),
            item.nodeid,
            report.when,
            report.passed,
            bool(list(item.iter_markers(name="xfail"))),
            item.config.stash.get(_PROOF_CALL_PASSED, None),
        )
    return report


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> Generator[None, Result[object], None]:
    proof_partition = session.config.getoption("ci_partition") == "proof"
    proof_passed = session.config.stash.get(_PROOF_CALL_PASSED, None)
    if proof_partition and proof_passed is not True and session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    # pytest-cov finalizes its database on the inner sessionfinish leg.
    outcome = yield
    outcome.get_result()
    report = session.config.getoption("ci_shard_report")
    if report is None or "PYTEST_XDIST_WORKER" in os.environ or not report.exists():
        return
    manifest = json.loads(report.read_text(encoding="utf-8"))
    database = Path(os.environ.get("COVERAGE_FILE", ".coverage")) if session.config.getoption("cov_source", default=None) else None
    if session.config.getoption("ci_partition") != "all":
        manifest = finalize_long_run_manifest(manifest, int(session.exitstatus), proof_passed if proof_partition else None, database)
        effective_status = manifest["exit_status"]
        assert type(effective_status) is int
        session.exitstatus = effective_status
    else:
        manifest["exit_status"] = int(exitstatus)
    if database is not None and session.config.getoption("ci_partition") == "all":
        with database.open("rb") as stream:
            manifest["coverage_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    _publish_manifest(report, manifest)


def combine_shards(artifact_dir: Path, count: int, checkout: Path) -> None:
    """Combine only a complete, successful, disjoint partition of one collection."""
    selected: list[str] = []
    digests: set[str] = set()
    databases: list[str] = []
    identity = evidence_identity(checkout)
    for index in range(count):
        folder = artifact_dir / f"coverage-shard-3.13-{index}"
        manifest = json.loads((folder / "shard-manifest.json").read_text(encoding="utf-8"))
        # Downloads are confined to this native workflow run. Earlier producer
        # attempts at the same SHA remain valid when rerunning only failed jobs.
        if (
            not {"schema_version", "tested_commit", "python_version", "github_run_id", "github_run_attempt", "producer_job"}
            <= manifest.keys()
            or any(type(manifest.get(field)) is not int for field in ("schema_version", "shard_count", "shard_index", "exit_status"))
            or manifest["schema_version"] != 2
            or manifest.get("tested_commit") != identity["tested_commit"]
            or manifest.get("python_version") != "3.13"
            or manifest.get("partition") != "all"
            or manifest.get("github_run_id") != identity["github_run_id"]
            or manifest.get("producer_job") != ("test" if identity["github_run_id"] is not None else None)
        ):
            raise ValueError(f"Shard {index} evidence has the wrong tested commit, interpreter, or producer")
        attempt = manifest.get("github_run_attempt")
        if (identity["github_run_id"] is None and attempt is not None) or (
            identity["github_run_id"] is not None and (not isinstance(attempt, str) or not attempt.isdecimal() or int(attempt) < 1)
        ):
            raise ValueError(f"Shard {index} evidence has an invalid producer attempt")
        if manifest["shard_count"] != count or manifest["shard_index"] != index or manifest["exit_status"] != 0:
            raise ValueError(f"Shard {index} did not complete successfully with the expected identity")
        nodeids = manifest["selected_nodeids"]
        if not nodeids or any(shard_index(nodeid, count) != index for nodeid in nodeids):
            raise ValueError(f"Shard {index} has an empty or misassigned collection")
        selected.extend(nodeids)
        digests.add(manifest["collection_digest"])
        databases.append(_coverage_database(folder, manifest))
    if len(selected) != len(set(selected)) or digests != {collection_digest(selected)}:
        raise ValueError("Shard collections overlap, differ, or omit selected tests")
    _combine_coverage(databases, checkout)


def _coverage_database(folder: Path, manifest: dict[str, object]) -> str:
    database = folder / "coverage-data"
    if not database.is_file():
        raise FileNotFoundError(database)
    with database.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if manifest.get("coverage_sha256") != digest:
        raise ValueError(f"{folder.name} coverage database differs from its finalized test evidence")
    shard_data = coverage.CoverageData(basename=str(database))
    shard_data.read()
    if not any(shard_data.lines(filename) for filename in shard_data.measured_files()):
        raise ValueError(f"{folder.name} coverage database has no executed source lines")
    return str(database.resolve())


def _combine_coverage(databases: list[str], checkout: Path) -> None:
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


def validate_long_run_shards(artifact_dir: Path, count: int, checkout: Path, python_version: str) -> dict[str, str]:
    """Validate one interpreter's ordinary/proof union, combining covered 3.13.

    Each artifact name binds a producer and attempt. Select the highest observed
    attempt before reading its content; superseded content is irrelevant. Never
    fall back after a selected attempt's failure. The caller must also require
    native producer-job success: artifacts cannot establish that a rerun which
    uploaded no evidence succeeded.
    """
    if type(count) is not int:
        raise ValueError("Supply a literal integer ordinary shard count")
    if count < 1 or python_version not in {"3.12", "3.13"}:
        raise ValueError("Supply a positive shard count and Python 3.12 or 3.13")
    profile = load_duration_profile(checkout)
    identity = evidence_identity(checkout)
    if identity["github_run_id"] is None:
        raise ValueError("Partitioned artifact resolution requires a native Actions run identity")
    current_attempt = identity["github_run_attempt"]
    assert current_attempt is not None
    cases = LONG_RUN_CASES
    producers: dict[str, tuple[str, int, int, str, str | None]] = {
        f"ordinary-shard-{python_version}-{index}": ("ordinary", index, count, "test", None) for index in range(count)
    }
    for proof_case in cases:
        producers[f"landscape-proof-{proof_case}-{python_version}"] = ("proof", 0, 1, "proof", proof_case)
    candidates: dict[str, dict[int, Path]] = {producer: {} for producer in producers}
    for folder in sorted(artifact_dir.iterdir()):
        producer, separator, raw_attempt = folder.name.rpartition("-attempt-")
        if (
            not folder.is_dir()
            or producer not in producers
            or not separator
            or not raw_attempt.isdecimal()
            or int(raw_attempt) < 1
            or str(int(raw_attempt)) != raw_attempt
            or int(raw_attempt) > int(current_attempt)
        ):
            raise ValueError(f"Unknown or invalid producer artifact: {folder.name}")
        attempt = int(raw_attempt)
        if attempt in candidates[producer]:
            raise ValueError(f"Duplicate producer attempt: {folder.name}")
        candidates[producer][attempt] = folder
    selected: list[str] = []
    digests: set[str] = set()
    databases: list[str] = []
    resolved: dict[str, str] = {}
    ordinary_groups: dict[int, list[str]] = {}
    for producer, attempts in candidates.items():
        if not attempts:
            raise ValueError(f"Missing producer evidence: {producer}")
        attempt = max(attempts)
        folder = attempts[attempt]
        partition, index, expected_count, job, case = producers[producer]
        manifest = json.loads((folder / "shard-manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError(f"Artifact {folder.name} lacks a manifest object")
        if (
            any(type(manifest.get(field)) is not int for field in ("schema_version", "shard_count", "shard_index"))
            or manifest.get("schema_version") != 4
            or manifest.get("assignment_algorithm") != ASSIGNMENT_ALGORITHM
            or manifest.get("duration_profile_sha256") != profile.sha256
            or manifest.get("tested_commit") != identity["tested_commit"]
            or manifest.get("github_run_id") != identity["github_run_id"]
            or manifest.get("github_run_attempt") != str(attempt)
            or manifest.get("python_version") != python_version
            or manifest.get("producer_job") != job
            or manifest.get("partition") != partition
            or manifest.get("shard_count") != expected_count
            or manifest.get("shard_index") != index
            or "proof_case" not in manifest
            or manifest["proof_case"] != case
            or (partition == "ordinary" and "proof_call_passed" in manifest)
        ):
            raise ValueError(f"Artifact {folder.name} differs from its expected producer identity")
        nodeids = manifest.get("selected_nodeids")
        if (
            type(manifest.get("exit_status")) is not int
            or manifest["exit_status"] != 0
            or not isinstance(nodeids, list)
            or not nodeids
            or not all(isinstance(n, str) for n in nodeids)
        ):
            raise ValueError(f"Newest artifact {folder.name} did not successfully finalize a selected collection")
        if partition == "proof":
            assert case is not None
            if nodeids != [cases[case]] or manifest.get("proof_call_passed") is not True:
                raise ValueError(f"Artifact {folder.name} lacks the singleton successful proof call")
        else:
            if any(nodeid in cases.values() for nodeid in nodeids):
                raise ValueError(f"Artifact {folder.name} contains a proof overlap or misassigned ordinary test")
            ordinary_groups[index] = nodeids
        digest = manifest.get("collection_digest")
        if not isinstance(digest, str):
            raise ValueError(f"Artifact {folder.name} lacks a collection digest")
        digests.add(digest)
        selected.extend(nodeids)
        resolved[producer] = folder.name
        if python_version == "3.13":
            databases.append(_coverage_database(folder, manifest))
    if len(selected) != len(set(selected)) or digests != {collection_digest(selected)}:
        raise ValueError("Ordinary and proof collections overlap, differ, or omit selected tests")
    assignment = ordinary_assignment([node for group in ordinary_groups.values() for node in group], count, profile)
    if any(assignment[node] != index for index, group in ordinary_groups.items() for node in group):
        raise ValueError("Artifact union contains a misassigned ordinary test")
    if python_version == "3.13":
        _combine_coverage(databases, checkout)
    return resolved


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--checkout", type=Path, default=Path.cwd())
    parser.add_argument("--partitioned", action="store_true")
    parser.add_argument("--python-version", choices=("3.12", "3.13"))
    args = parser.parse_args()
    if args.partitioned:
        if args.python_version is None:
            parser.error("--partitioned requires --python-version")
        resolved = validate_long_run_shards(args.artifact_dir, args.shard_count, args.checkout, args.python_version)
        print(json.dumps({"selected_artifacts": resolved}, sort_keys=True))
    else:
        if args.python_version is not None:
            parser.error("--python-version requires --partitioned")
        combine_shards(args.artifact_dir, args.shard_count, args.checkout)


if __name__ == "__main__":
    main()
