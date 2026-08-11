"""Atomic package initialization and assessment validation."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from .catalog import validate_catalog
from .common import (
    CELL_STATUSES,
    DIMENSIONS,
    FAMILY_LABELS,
    GIT_OBJECT_PATTERN,
    HARD_GATE_IDS,
    PLACEHOLDER_PATTERN,
    SHA256_PATTERN,
    VERDICTS,
    _assessment_schema_version,
    _dict,
    _expected_leg_ids,
    _fail,
    _git_root,
    _list,
    _repository_path,
    _require,
    _sha256,
    _string,
    _strings,
    _unique,
    load_unique_json,
)

V2_ASSESSMENT_TOP_LEVEL_KEYS = {
    "schema_version",
    "assessment_id",
    "mode",
    "parent_assessment",
    "changed_tuples",
    "changed_gate_ids",
    "catalog",
    "baseline",
    "environment",
    "structure_snapshot",
    "tracker_snapshot",
    "evidence",
    "legs",
    "hard_gates",
    "derived",
    "limitations",
    "review_record",
}
ASSESSMENT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
EVIDENCE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
PYTEST_REPORTER_PLUGIN = "scripts.state_engine_profile_reporter"
PYTEST_SAFE_ENVIRONMENT = {"PYTHONHASHSEED", "PYTHONPATH", "TZ", "LANG", "LC_ALL"}
PYTEST_FLAGS = {
    "-q",
    "--quiet",
    "-x",
    "--exitfirst",
    "--strict-config",
    "--strict-markers",
    "--disable-warnings",
}
COMPLETION_MARKER_NAME = ".state-engine-assessment.ready"


def _initial_derived(catalog: dict[str, Any]) -> dict[str, Any]:
    family_counts: dict[str, dict[str, int]] = {}
    for family in FAMILY_LABELS:
        count = sum(leg["family"] == family for leg in catalog["legs"])
        family_counts[family] = {"confirmed": 0, "gap": 0, "unknown": count}
    count = len(catalog["legs"])
    return {
        "family_counts": family_counts,
        "total": {"confirmed": 0, "gap": 0, "unknown": count},
        "overall_verdict": "insufficient_evidence",
        "reason": "Assessment initialization only.",
    }


def initialize_full(assessment_id: str, output_directory: Path, catalog_argument: Path) -> Path:
    root = _git_root(output_directory)
    _require(not catalog_argument.is_absolute(), "--catalog must be repository-relative")
    catalog_path = _repository_path(root, catalog_argument.as_posix(), "--catalog")
    _require(catalog_path.is_file(), f"catalog file does not exist: {catalog_path}")
    catalog = load_unique_json(catalog_path)
    validate_catalog(catalog, catalog_path)
    assessment_schema_version = _assessment_schema_version(catalog["catalog_id"])
    requested_parent = Path(os.path.abspath(output_directory.parent))
    _require(requested_parent.is_relative_to(root), "assessment destination must be inside the repository")
    parent = root
    for part in requested_parent.relative_to(root).parts:
        parent = parent / part
        try:
            parent.lstat()
        except FileNotFoundError:
            parent.mkdir()
        _require(not parent.is_symlink(), "assessment destination parent must not contain symlinks")
        _require(parent.is_dir(), f"assessment destination parent is not a directory: {parent}")
    _require(output_directory.name not in {"", ".", ".."}, "assessment destination must name a directory")
    destination = parent / output_directory.name
    try:
        destination.lstat()
    except FileNotFoundError:
        pass
    else:
        _fail(f"assessment destination already exists: {destination}")
    relative_directory = destination.relative_to(root).as_posix()
    document = {
        "schema_version": assessment_schema_version,
        "assessment_id": assessment_id,
        "mode": "full",
        "parent_assessment": None,
        "changed_tuples": [],
        "changed_gate_ids": [],
        "catalog": {
            "path": catalog_path.relative_to(root).as_posix(),
            "catalog_id": catalog["catalog_id"],
            "schema_version": catalog["schema_version"],
            "sha256_at_evidence_capture": _sha256(catalog_path),
        },
        "baseline": {},
        "environment": {},
        "structure_snapshot": {},
        "tracker_snapshot": {},
        "evidence": [],
        "legs": [
            {
                "id": leg["id"],
                "derived_verdict": "unknown",
                "default_status": "unknown",
                "reason": "No current evidence is attached.",
                "owner_issue": None,
                "exit_gate": "Execute and attach every required catalog cell.",
                "overrides": [],
            }
            for leg in catalog["legs"]
        ],
        "hard_gates": [
            {
                "id": gate["id"],
                "status": "open" if gate["id"] == "HG-09-mandatory-leg-unresolved" else "unknown",
                "support": [],
                "affected_leg_ids": [leg["id"] for leg in catalog["legs"]],
                "reason": "Not yet evaluated.",
            }
            for gate in catalog["hard_gates"]
        ],
        "derived": _initial_derived(catalog),
        "limitations": [],
        "review_record": f"{relative_directory}/review.md",
    }
    templates = {
        "README.md": "# State Engine Assessment\n\nSee `assessment.json` for the proof manifest.\n",
        "evidence.md": "# Verification Runs\n\nRecord exact commands and retained artifacts here.\n",
        "review.md": "# Assessment Review\n\nReview outcome: pending\n",
    }
    staged = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=parent))
    reserved = False
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        (staged / "assessment.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        for name, content in templates.items():
            (staged / name).write_text(content, encoding="utf-8")
        for staged_file in staged.iterdir():
            with staged_file.open("rb") as handle:
                os.fsync(handle.fileno())
        staged_fd = os.open(staged, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(staged_fd)
        finally:
            os.close(staged_fd)
        try:
            destination.lstat()
        except FileNotFoundError:
            pass
        else:
            _fail(f"assessment destination already exists: {destination}")
        try:
            os.mkdir(destination, mode=0o700)
        except FileExistsError:
            _fail(f"assessment destination already exists: {destination}")
        reserved = True
        os.fsync(parent_fd)
        destination_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for staged_file in staged.iterdir():
                os.rename(staged_file, destination / staged_file.name)
            os.chmod(destination, 0o755)
            os.fsync(destination_fd)
            marker_fd = os.open(
                COMPLETION_MARKER_NAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o644,
                dir_fd=destination_fd,
            )
            try:
                os.write(marker_fd, f"state-engine-assessment-package-v{assessment_schema_version}\n".encode())
                os.fsync(marker_fd)
            finally:
                os.close(marker_fd)
            os.fsync(destination_fd)
            os.fsync(parent_fd)
        finally:
            os.close(destination_fd)
    except Exception:
        if reserved:
            shutil.rmtree(destination)
            os.fsync(parent_fd)
        raise
    finally:
        os.close(parent_fd)
        if staged.exists():
            shutil.rmtree(staged)
    return destination / "assessment.json"


def _validate_completion_marker(assessment_path: Path, assessment_schema_version: int) -> None:
    marker = assessment_path.parent / COMPLETION_MARKER_NAME
    _require(marker.is_file(), f"assessment package completion marker is missing: {marker}")
    _require(not marker.is_symlink(), f"assessment package completion marker cannot be a symlink: {marker}")
    expected = f"state-engine-assessment-package-v{assessment_schema_version}\n".encode()
    _require(marker.read_bytes() == expected, f"assessment package completion marker is invalid: {marker}")


def _validate_baseline(assessment: dict[str, Any], root: Path) -> None:
    baseline = _dict(assessment.get("baseline"), "baseline")
    mode = baseline.get("mode")
    _require(mode in {"current", "historical"}, "baseline mode must be current or historical")
    identity_fields = {"mode", "commit", "tree"}
    current_fields = {
        *identity_fields,
        "repository_root",
        "remote",
        "branch",
        "worktree_status_at_evidence_capture",
        "submodules",
        "worktrees_at_capture",
    }
    expected_fields = current_fields if mode == "current" else identity_fields
    _require(set(baseline) == expected_fields, f"baseline {mode} schema has unexpected fields")
    for field in expected_fields:
        _require(field in baseline, f"baseline is missing {field}")
    for field in ("commit", "tree"):
        _require(
            isinstance(baseline[field], str) and bool(GIT_OBJECT_PATTERN.fullmatch(baseline[field])),
            f"baseline {field} is not a full Git object ID",
        )
    tree = subprocess.run(
        ["git", "-C", str(root), "rev-parse", f"{baseline['commit']}^{{tree}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    _require(tree.returncode == 0 and tree.stdout.strip() == baseline["tree"], "baseline commit/tree identity does not resolve")
    if mode == "historical":
        return
    repository_root = baseline["repository_root"]
    _require(isinstance(repository_root, str) and Path(repository_root).is_absolute(), "baseline repository_root must be absolute")
    for field in ("remote", "branch"):
        value = baseline[field]
        _require(isinstance(value, str) and bool(value.strip()), f"baseline {field} must be a non-empty string")
    for field in ("worktree_status_at_evidence_capture", "submodules", "worktrees_at_capture"):
        _strings(baseline[field], f"baseline {field}")
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _require(baseline["commit"] == head, "baseline current commit does not match live HEAD")
    live_tree = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _require(baseline["tree"] == live_tree, "baseline current tree does not match live HEAD")
    _require(Path(repository_root).resolve() == root, "baseline current repository_root does not match the checkout")
    branch = subprocess.run(
        ["git", "-C", str(root), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _require(baseline["branch"] == branch, "baseline current branch does not match the checkout")
    remote = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        check=False,
        capture_output=True,
        text=True,
    )
    _require(
        remote.returncode == 0 and baseline["remote"] == remote.stdout.strip(),
        "baseline current remote does not match origin",
    )
    committed_diff = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "diff",
            "--quiet",
            f"{baseline['commit']}..HEAD",
            "--",
            ".",
            ":(exclude)docs/**",
        ],
        check=False,
    )
    _require(committed_diff.returncode == 0, "execution checkout differs from baseline outside docs/")
    status = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            ".",
            ":(exclude)docs/**",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _require(not status, f"non-document overlay is not clean: {status}")


def _offset_datetime(value: Any, context: str) -> datetime:
    _require(isinstance(value, str), f"{context} must be an offset-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _fail(f"{context} must be an offset-aware timestamp")
    _require(parsed.utcoffset() is not None, f"{context} must be an offset-aware timestamp")
    return parsed


def _validate_environment(assessment: dict[str, Any]) -> None:
    environment = _dict(assessment.get("environment"), "environment")
    fields = (
        "captured_at",
        "timezone",
        "locale",
        "kernel",
        "python",
        "python_executable",
        "python_build",
        "pytest",
        "uv",
        "git",
        "sqlite",
        "sqlalchemy",
        "multiprocessing_start_method_before_tests",
        "pythonhashseed",
        "dotenv",
        "pyproject_sha256",
        "uv_lock_sha256",
        "database_profile",
        "sensitive_environment_captured",
    )
    _require(set(environment) == set(fields), "environment has unexpected fields")
    for field in fields:
        _require(field in environment, f"environment is missing {field}")
    _offset_datetime(environment["captured_at"], "environment captured_at")
    for field in (
        "timezone",
        "locale",
        "kernel",
        "python",
        "python_executable",
        "python_build",
        "pytest",
        "uv",
        "git",
        "sqlite",
        "sqlalchemy",
        "pythonhashseed",
        "dotenv",
        "database_profile",
    ):
        _require(isinstance(environment[field], str) and bool(environment[field].strip()), f"environment {field} must be text")
    _require(
        environment["multiprocessing_start_method_before_tests"] is None
        or isinstance(environment["multiprocessing_start_method_before_tests"], str),
        "environment multiprocessing start method must be null or text",
    )
    for field in ("pyproject_sha256", "uv_lock_sha256"):
        _require(
            isinstance(environment[field], str) and bool(SHA256_PATTERN.fullmatch(environment[field])),
            f"environment {field} is not SHA-256",
        )
    _require(environment["sensitive_environment_captured"] is False, "sensitive environment values must not be captured")
    for name in ("structure_snapshot", "tracker_snapshot"):
        snapshot = _dict(assessment.get(name), name)
        _require(set(snapshot) == {"provider", "captured_at", "limitation"}, f"{name} has unexpected fields")
        for field in ("provider", "captured_at", "limitation"):
            _require(field in snapshot, f"{name} is missing {field}")
        _require(
            all(isinstance(snapshot[field], str) and bool(snapshot[field].strip()) for field in ("provider", "limitation")),
            f"{name} provider and limitation must be non-empty strings",
        )
        _offset_datetime(snapshot["captured_at"], f"{name} captured_at")


def _semantic_cases(leg: dict[str, Any]) -> list[str]:
    return _strings(leg.get("required_cases", ["leg-contract"]), f"catalog leg {leg['id']} cases")


def _profile_cases(catalog: dict[str, Any], leg: dict[str, Any]) -> list[str]:
    profile = catalog["applicability_profiles"][leg["applicability_profile"]]
    if catalog["catalog_id"] == "elspeth-state-engine-v2":
        applicability = _dict(profile["profile_case_applicability"], "profile-case applicability")
        return list(applicability)
    return ["historical-v1"]


def _cell_is_applicable(
    catalog: dict[str, Any],
    leg: dict[str, Any],
    dimension: str,
    profile_case: str,
) -> bool:
    profile = catalog["applicability_profiles"][leg["applicability_profile"]]
    if profile[dimension] != "required":
        return False
    if catalog["catalog_id"] == "elspeth-state-engine-v2":
        applicability = _dict(profile["profile_case_applicability"], "profile-case applicability")
        return _string(applicability[profile_case], "profile-case policy") == "required"
    return True


def _coverage_key(item: dict[str, Any], v2: bool) -> tuple[str, str, str, str]:
    profile_case = _string(
        item.get("profile_case") if v2 else item.get("profile_case", "historical-v1"),
        "coverage profile_case",
    )
    return (
        _string(item.get("leg_id"), "coverage leg_id"),
        _string(item.get("dimension_id"), "coverage dimension_id"),
        _string(item.get("case_id"), "coverage case_id"),
        profile_case,
    )


def _validate_pytest_command(
    record: dict[str, Any],
    assessment: dict[str, Any],
    root: Path,
    *,
    junit_relative: str,
    profile_relative: str,
) -> list[str]:
    evidence_id = record["id"]
    argv = _strings(record.get("argv"), f"evidence {evidence_id} argv")
    environment = _dict(assessment.get("environment"), "environment")
    python_executable = _string(environment.get("python_executable"), "environment python_executable")
    _require(
        argv[:3] == [python_executable, "-m", "pytest"],
        f"pytest evidence {evidence_id} must use the trusted pytest invocation recorded by the environment",
    )
    selectors: list[str] = []
    reporter_plugins = 0
    junit_outputs = 0
    profile_outputs = 0
    index = 3
    while index < len(argv):
        item = argv[index]
        if item == "-p":
            _require(index + 1 < len(argv), "pytest -p requires a plugin name")
            _require(argv[index + 1] == PYTEST_REPORTER_PLUGIN, f"pytest evidence {evidence_id} uses an untrusted plugin")
            reporter_plugins += 1
            index += 2
            continue
        if item == "--junitxml":
            _require(index + 1 < len(argv), "pytest --junitxml requires a path")
            _require(argv[index + 1] == junit_relative, f"pytest evidence {evidence_id} JUnit output is not retained")
            junit_outputs += 1
            index += 2
            continue
        if item.startswith("--junitxml="):
            _require(item.removeprefix("--junitxml=") == junit_relative, f"pytest evidence {evidence_id} JUnit output is not retained")
            junit_outputs += 1
            index += 1
            continue
        if item == "--state-engine-profile-report":
            _require(index + 1 < len(argv), "pytest --state-engine-profile-report requires a path")
            _require(argv[index + 1] == profile_relative, f"pytest evidence {evidence_id} profile output is not retained")
            profile_outputs += 1
            index += 2
            continue
        if item.startswith("--state-engine-profile-report="):
            _require(
                item.removeprefix("--state-engine-profile-report=") == profile_relative,
                f"pytest evidence {evidence_id} profile output is not retained",
            )
            profile_outputs += 1
            index += 1
            continue
        if item in {"-n", "--numprocesses"}:
            _require(index + 1 < len(argv) and argv[index + 1] == "0", "pytest evidence permits only -n 0")
            index += 2
            continue
        if item in {"-m", "-k"}:
            _require(index + 1 < len(argv) and bool(argv[index + 1]), f"pytest {item} requires an expression")
            index += 2
            continue
        if (
            item in PYTEST_FLAGS
            or re.fullmatch(r"--maxfail=[1-9][0-9]*", item)
            or re.fullmatch(r"--tb=(?:auto|long|short|line|native|no)", item)
        ):
            index += 1
            continue
        _require(not item.startswith("-"), f"pytest evidence option is not permitted: {item}")
        path_text = item.split("::", 1)[0]
        _require(bool(path_text), f"pytest selector is invalid: {item}")
        selector_path = Path(path_text)
        _require(not selector_path.is_absolute(), f"pytest selector must be repository-relative: {item}")
        resolved = _repository_path(root, path_text, f"pytest selector {item}")
        _require(resolved.is_relative_to(root / "tests"), f"pytest selector must be under tests/: {item}")
        if assessment["baseline"]["mode"] == "current":
            _require(resolved.is_file(), f"pytest selector does not exist in the current checkout: {item}")
        if assessment["derived"]["overall_verdict"] == "complete":
            _require(
                _selector_names_declared_test(resolved, item),
                f"pytest evidence {evidence_id} selector must name a declared test: {item}",
            )
        selectors.append(item)
        index += 1
    _require(bool(selectors), f"pytest evidence {evidence_id} requires at least one explicit tests/ selector")
    _require(reporter_plugins == 1, f"pytest evidence {evidence_id} must load the trusted profile reporter exactly once")
    _require(junit_outputs == 1, f"pytest evidence {evidence_id} must declare one retained JUnit output")
    _require(profile_outputs == 1, f"pytest evidence {evidence_id} must declare one retained profile output")
    safe_environment = _dict(record.get("safe_environment"), f"evidence {evidence_id} safe_environment")
    for name, value in safe_environment.items():
        _require(name in PYTEST_SAFE_ENVIRONMENT, f"evidence {evidence_id} safe_environment name is not permitted: {name}")
        _require(value is None or isinstance(value, str), f"evidence {evidence_id} safe_environment value must be text or null")
        if name == "PYTHONPATH":
            _require(
                value == str(root / "src"),
                f"evidence {evidence_id} PYTHONPATH must name the assessed checkout src directory",
            )
    return selectors


def _selector_names_declared_test(path: Path, selector: str) -> bool:
    parts = selector.split("::")
    if len(parts) not in {2, 3}:
        return False
    target_name = re.sub(r"\[[^]]*\]$", "", parts[-1])
    if not target_name:
        return False
    try:
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError):
        return False
    if len(parts) == 2:
        return any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == target_name for node in module.body)
    class_name = parts[1]
    return any(
        isinstance(node, ast.ClassDef)
        and node.name == class_name
        and any(isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == target_name for child in node.body)
        for node in module.body
    )


def _node_matches_selector(node_id: str, selector: str) -> bool:
    if "::" not in selector:
        return node_id == selector or node_id.startswith(f"{selector}::")
    return node_id == selector or node_id.startswith(f"{selector}::") or node_id.startswith(f"{selector}[")


def _junit_node_ids(junit_path: Path, evidence_id: str) -> tuple[list[str], list[str]]:
    junit_root = ET.parse(junit_path).getroot()
    testcases = [element for element in junit_root.iter() if element.tag.rsplit("}", 1)[-1] == "testcase"]
    node_ids: list[str] = []
    outcomes: list[str] = []
    for testcase in testcases:
        properties = [element for element in testcase.iter() if element.tag.rsplit("}", 1)[-1] == "property"]
        identity = [element.attrib.get("value") for element in properties if element.attrib.get("name") == "elspeth_node_id"]
        _require(
            len(identity) == 1 and isinstance(identity[0], str) and bool(identity[0]),
            f"pytest evidence {evidence_id} JUnit testcase must carry one exact elspeth_node_id",
        )
        node_ids.append(cast(str, identity[0]))
        child_tags = {child.tag.rsplit("}", 1)[-1] for child in testcase}
        result_tags = child_tags & {"failure", "error", "skipped"}
        _require(len(result_tags) <= 1, f"pytest evidence {evidence_id} JUnit testcase has conflicting outcomes")
        outcomes.append(next(iter(result_tags), "passed"))
    _require(bool(node_ids), f"pytest evidence {evidence_id} JUnit XML has no testcases")
    _unique(node_ids, f"pytest evidence {evidence_id} JUnit node identities")
    leaf_suites = [
        element
        for element in junit_root.iter()
        if element.tag.rsplit("}", 1)[-1] == "testsuite" and not any(child.tag.rsplit("}", 1)[-1] == "testsuite" for child in element)
    ]
    _require(bool(leaf_suites), f"pytest evidence {evidence_id} JUnit XML has no test suites")
    try:
        aggregate = {
            key: sum(int(suite.attrib.get(key, "0")) for suite in leaf_suites) for key in ("tests", "failures", "errors", "skipped")
        }
    except ValueError:
        _fail(f"pytest evidence {evidence_id} JUnit aggregate counts must be integers")
    outcome_counts = Counter(outcomes)
    _require(
        aggregate
        == {
            "tests": len(testcases),
            "failures": outcome_counts["failure"],
            "errors": outcome_counts["error"],
            "skipped": outcome_counts["skipped"],
        },
        f"pytest evidence {evidence_id} JUnit aggregate counts disagree with testcase evidence",
    )
    return node_ids, outcomes


def _validate_profile_report(
    profile_path: Path,
    evidence_id: str,
    profile_case_by_id: dict[str, dict[str, Any]],
    node_ids: list[str],
    junit_outcomes: list[str],
) -> tuple[dict[str, Any], dict[str, int]]:
    profile = load_unique_json(profile_path)
    expected_fields = {
        "schema_version",
        "producer",
        "profile_case_id",
        "state_store",
        "deployment",
        "backend_version",
        "backend_probe",
        "deployment_probe",
        "probe_node_id",
        "node_ids",
        "outcomes",
        "warnings",
    }
    _require(set(profile) == expected_fields, f"evidence {evidence_id} profile report has unexpected fields")
    _require(
        type(profile.get("schema_version")) is int and profile["schema_version"] == 2,
        f"evidence {evidence_id} profile report schema_version must be integer 2",
    )
    _require(
        profile.get("producer") == "elspeth-state-engine-profile-reporter-v2",
        f"evidence {evidence_id} profile report has an unknown producer",
    )
    profile_case_id = _string(profile.get("profile_case_id"), f"evidence {evidence_id} profile_case_id")
    _require(profile_case_id in profile_case_by_id, f"evidence {evidence_id} has unknown execution profile")
    expected_profile = profile_case_by_id[profile_case_id]
    _require(
        profile.get("state_store") == expected_profile["state_store"] and profile.get("deployment") == expected_profile["deployment"],
        f"evidence {evidence_id} runtime profile does not match the catalog",
    )
    reported_nodes = _strings(profile.get("node_ids"), f"evidence {evidence_id} profile node_ids")
    _unique(reported_nodes, f"evidence {evidence_id} profile node_ids")
    _require(reported_nodes == node_ids, f"evidence {evidence_id} profile node identities do not match JUnit/index evidence")
    raw_outcomes = _list(profile.get("outcomes"), f"evidence {evidence_id} profile outcomes")
    _require(len(raw_outcomes) == len(node_ids), f"evidence {evidence_id} profile must report one outcome per node")
    outcome_counts: Counter[str] = Counter()
    junit_compatibility = {
        "passed": {"passed"},
        "failed": {"failure"},
        "error": {"error"},
        "skipped": {"skipped"},
        "xfailed": {"skipped"},
        "xpassed": {"passed", "failure"},
    }
    for index, (raw_outcome, node_id, junit_outcome) in enumerate(zip(raw_outcomes, node_ids, junit_outcomes, strict=True)):
        outcome_record = _dict(raw_outcome, f"evidence {evidence_id} profile outcome {index}")
        _require(
            set(outcome_record) == {"node_id", "outcome"},
            f"evidence {evidence_id} profile outcome has unexpected fields",
        )
        _require(
            outcome_record.get("node_id") == node_id,
            f"evidence {evidence_id} profile outcome node identities do not match JUnit/index evidence",
        )
        outcome = _string(outcome_record.get("outcome"), f"evidence {evidence_id} profile outcome")
        _require(outcome in junit_compatibility, f"evidence {evidence_id} profile has an unknown pytest outcome")
        _require(
            junit_outcome in junit_compatibility[outcome],
            f"evidence {evidence_id} profile outcome disagrees with JUnit testcase evidence",
        )
        outcome_counts[outcome] += 1
    raw_warnings = _list(profile.get("warnings"), f"evidence {evidence_id} profile warnings")
    for index, raw_warning in enumerate(raw_warnings):
        warning = _dict(raw_warning, f"evidence {evidence_id} profile warning {index}")
        _require(
            set(warning) == {"node_id", "when", "category", "message", "filename", "line"},
            f"evidence {evidence_id} profile warning has unexpected fields",
        )
        warning_node = warning.get("node_id")
        _require(
            warning_node is None or warning_node in node_ids,
            f"evidence {evidence_id} profile warning names an unknown node",
        )
        _require(
            warning.get("when") in {"config", "collect", "runtest"},
            f"evidence {evidence_id} profile warning has an invalid phase",
        )
        for field in ("category", "filename"):
            _require(
                isinstance(warning.get(field), str) and bool(warning[field]),
                f"evidence {evidence_id} profile warning {field} must be non-empty text",
            )
        _require(
            isinstance(warning.get("message"), str),
            f"evidence {evidence_id} profile warning message must be text",
        )
        _require(
            type(warning.get("line")) is int and warning["line"] > 0,
            f"evidence {evidence_id} profile warning line must be a positive integer",
        )
    probe_node_id = _string(profile.get("probe_node_id"), f"evidence {evidence_id} profile probe_node_id")
    _require(probe_node_id in node_ids, f"evidence {evidence_id} profile probe node is not retained")
    deployment_probe = _dict(profile.get("deployment_probe"), f"evidence {evidence_id} deployment_probe")
    _require(
        deployment_probe == {"kind": "trusted-test-runtime-assertion", "node_id": probe_node_id},
        f"evidence {evidence_id} deployment provenance is not a trusted runtime assertion",
    )
    backend_probe = _dict(profile.get("backend_probe"), f"evidence {evidence_id} backend_probe")
    backend_version = _string(profile.get("backend_version"), f"evidence {evidence_id} backend_version")
    if profile["state_store"] == "postgresql-16":
        _require(
            backend_probe == {"kind": "postgresql-server-query", "dialect": "postgresql", "query": "SHOW server_version"},
            f"evidence {evidence_id} PostgreSQL profile lacks server-query provenance",
        )
        _require(
            re.fullmatch(r"16(?:\.\d+){0,2}", backend_version) is not None,
            f"evidence {evidence_id} PostgreSQL backend version must be 16.x",
        )
    else:
        _require(
            backend_probe == {"kind": "sqlite-connection-query", "dialect": "sqlite", "query": "SELECT sqlite_version()"},
            f"evidence {evidence_id} SQLite profile lacks connection-query provenance",
        )
        _require(
            re.fullmatch(r"3\.\d+(?:\.\d+)?", backend_version) is not None,
            f"evidence {evidence_id} SQLite backend version must be 3.x",
        )
    return profile, {
        "passed": outcome_counts["passed"],
        "failed": outcome_counts["failed"],
        "errors": outcome_counts["error"],
        "skipped": outcome_counts["skipped"],
        "xfailed": outcome_counts["xfailed"],
        "xpassed": outcome_counts["xpassed"],
        "warnings": len(raw_warnings),
    }


def _validate_evidence(
    assessment: dict[str, Any],
    catalog: dict[str, Any],
    root: Path,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, set[tuple[str, str, str, str]]],
    set[str],
]:
    catalog_by_id = {leg["id"]: leg for leg in catalog["legs"]}
    v2 = catalog["catalog_id"] == "elspeth-state-engine-v2"
    records = _list(assessment.get("evidence"), "evidence")
    evidence_by_id: dict[str, dict[str, Any]] = {}
    coverage_by_evidence: dict[str, set[tuple[str, str, str, str]]] = {}
    promotable_evidence: set[str] = set()
    required_fields = (
        "kind",
        "reproducibility_class",
        "argv",
        "cwd_relative",
        "timeout_seconds",
        "safe_environment",
        "resources",
        "started_at",
        "ended_at",
        "duration_seconds",
        "exit_code",
        "result_counts",
        "coverage",
        "retained_artifacts",
        "establishes",
        "does_not_establish",
    )
    pytest_only_fields = ("collected_node_index", "collected_nodes")
    pytest_result_keys = ["passed", "failed", "errors", "skipped", "xfailed", "xpassed", "warnings"]
    allowed_kinds = {"pytest", "documentation"} if v2 else {"pytest"}
    profile_case_by_id = {
        profile["id"]: profile for profile in _dict(catalog.get("execution_profiles"), "execution_profiles").get("profile_cases", [])
    }
    for index, raw_record in enumerate(records):
        record = _dict(raw_record, f"evidence[{index}]")
        evidence_id = _string(record.get("id"), f"evidence[{index}].id")
        _require(
            bool(EVIDENCE_ID_PATTERN.fullmatch(evidence_id)),
            f"evidence ID has invalid characters or length: {evidence_id!r}",
        )
        _require(evidence_id not in evidence_by_id, f"duplicate evidence ID: {evidence_id}")
        kind = _string(record.get("kind"), f"evidence {evidence_id} kind")
        _require(kind in allowed_kinds, f"evidence {evidence_id} has unsupported kind: {kind}")
        for field in required_fields:
            _require(field in record, f"evidence {evidence_id} is missing {field}")
        if kind == "pytest" and v2:
            _require(
                set(record) == {"id", *required_fields, *pytest_only_fields},
                f"pytest evidence {evidence_id} has unexpected fields",
            )
            _require(
                type(record.get("collected_nodes")) is int and record["collected_nodes"] > 0,
                f"pytest evidence {evidence_id} must have a positive collected-node count",
            )
        elif kind == "documentation" and v2:
            _require(
                set(record) == {"id", *required_fields},
                f"documentation evidence {evidence_id} has unexpected fields",
            )
            _require(
                record["result_counts"] is None,
                f"documentation evidence {evidence_id} result_counts must be null",
            )
        _require(
            record["reproducibility_class"] in {"deterministic", "semantic_comparison", "external_observation"},
            f"evidence {evidence_id} has invalid reproducibility_class",
        )
        _strings(record["argv"], f"evidence {evidence_id} argv")
        _repository_path(root, record["cwd_relative"], f"evidence {evidence_id} cwd_relative")
        _require(
            type(record["timeout_seconds"]) is int and record["timeout_seconds"] > 0,
            f"evidence {evidence_id} timeout_seconds must be a positive integer",
        )
        _strings(record["resources"], f"evidence {evidence_id} resources")
        started_at = _offset_datetime(record["started_at"], f"evidence {evidence_id} started_at")
        ended_at = _offset_datetime(record["ended_at"], f"evidence {evidence_id} ended_at")
        duration = record["duration_seconds"]
        _require(
            type(duration) in {int, float} and math.isfinite(duration) and duration >= 0,
            f"evidence {evidence_id} duration_seconds must be a finite non-negative number",
        )
        _require(ended_at >= started_at, f"evidence {evidence_id} ended_at precedes started_at")
        _require(
            abs((ended_at - started_at).total_seconds() - duration) <= 0.001,
            f"evidence {evidence_id} duration_seconds does not match its timestamps",
        )
        _require(type(record["exit_code"]) is int, f"evidence {evidence_id} exit_code must be an integer")
        _strings(record["establishes"], f"evidence {evidence_id} establishes")
        _strings(record["does_not_establish"], f"evidence {evidence_id} does_not_establish")
        safe_environment = _dict(record["safe_environment"], f"evidence {evidence_id} safe_environment")
        _require(
            all(
                re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) and (value is None or isinstance(value, str))
                for name, value in safe_environment.items()
            ),
            f"evidence {evidence_id} has invalid safe_environment",
        )
        raw_coverage = _list(record["coverage"], f"evidence {evidence_id} coverage")
        coverage: set[tuple[str, str, str, str]] = set()
        node_subject: dict[str, tuple[str, str, str]] = {}
        for raw_item in raw_coverage:
            item = _dict(raw_item, f"evidence {evidence_id} coverage item")
            if v2:
                _require(
                    set(item) == {"leg_id", "dimension_id", "case_id", "profile_case", "node_ids"},
                    f"evidence {evidence_id} coverage item has unexpected fields",
                )
            coverage_key = _coverage_key(item, v2)
            coverage.add(coverage_key)
            if kind == "pytest" and v2:
                node_ids = _strings(item.get("node_ids"), f"evidence {evidence_id} coverage node_ids")
                _require(bool(node_ids), f"pytest evidence {evidence_id} coverage must cite node_ids")
                _unique(node_ids, f"evidence {evidence_id} coverage node_ids")
                subject = (coverage_key[0], coverage_key[2], coverage_key[3])
                for node_id in node_ids:
                    prior = node_subject.setdefault(node_id, subject)
                    _require(
                        prior == subject,
                        f"pytest evidence {evidence_id} node {node_id} must establish only one proof subject",
                    )
        _require(len(coverage) == len(raw_coverage), f"evidence {evidence_id} coverage has duplicates")
        for leg_id, dimension, case_id, profile_case in coverage:
            _require(leg_id in catalog_by_id, f"evidence {evidence_id} covers unknown leg")
            _require(dimension in DIMENSIONS, f"evidence {evidence_id} covers unknown dimension")
            _require(case_id in _semantic_cases(catalog_by_id[leg_id]), f"evidence {evidence_id} covers unknown semantic case")
            _require(profile_case in _profile_cases(catalog, catalog_by_id[leg_id]), f"evidence {evidence_id} covers unknown profile case")
        coverage_by_evidence[evidence_id] = coverage
        evidence_by_id[evidence_id] = record

        artifacts = _list(record["retained_artifacts"], f"evidence {evidence_id} artifacts")
        artifacts_by_kind: dict[str, tuple[dict[str, Any], Path]] = {}
        for raw_artifact in artifacts:
            artifact = _dict(raw_artifact, f"evidence {evidence_id} artifact")
            expected_artifact_fields = {"kind", "path", "sha256"} if v2 else {"path", "sha256"}
            _require(set(artifact) == expected_artifact_fields, f"evidence {evidence_id} artifact has unexpected fields")
            artifact_path = _repository_path(root, artifact.get("path"), f"evidence {evidence_id} artifact path")
            _require(artifact_path.is_file(), f"evidence artifact does not exist: {artifact_path}")
            _require(not artifact_path.is_symlink(), f"evidence artifact cannot be a symlink: {artifact_path}")
            _require(_sha256(artifact_path) == artifact.get("sha256"), f"evidence artifact digest mismatch: {artifact_path}")
            if v2:
                artifact_kind = _string(artifact.get("kind"), f"evidence {evidence_id} artifact kind")
                _require(artifact_kind not in artifacts_by_kind, f"evidence {evidence_id} has duplicate artifact kind")
                artifacts_by_kind[artifact_kind] = (artifact, artifact_path)
        if kind == "pytest":
            counts = _dict(record["result_counts"], f"evidence {evidence_id} result_counts")
            _require(list(counts) == pytest_result_keys, f"pytest evidence {evidence_id} result_counts has wrong keys")
            _require(
                all(type(counts[key]) is int and counts[key] >= 0 for key in pytest_result_keys),
                f"pytest evidence {evidence_id} result_counts must be non-negative integers",
            )
            _require(type(record.get("collected_nodes")) is int, f"pytest evidence {evidence_id} collected_nodes must be an integer")
            index_record = _dict(record.get("collected_node_index"), f"evidence {evidence_id} collected_node_index")
            _require(
                set(index_record) == ({"kind", "path", "sha256"} if v2 else {"path", "sha256"}),
                f"pytest evidence {evidence_id} collected_node_index has unexpected fields",
            )
            if v2:
                _require(index_record.get("kind") == "node_index", f"pytest evidence {evidence_id} node index has wrong kind")
            node_path = _repository_path(root, index_record.get("path"), f"evidence {evidence_id} node index")
            _require(node_path.is_file(), f"pytest node index does not exist: {node_path}")
            _require(not node_path.is_symlink(), f"pytest node index cannot be a symlink: {node_path}")
            _require(_sha256(node_path) == index_record.get("sha256"), f"pytest node-index digest mismatch: {node_path}")
            nodes = node_path.read_text(encoding="utf-8").splitlines()
            _require(all(node.strip() for node in nodes), f"pytest node index contains an empty node: {evidence_id}")
            _unique(nodes, f"pytest node index {evidence_id}")
            node_count = len(nodes)
            _require(node_count == record.get("collected_nodes"), f"pytest collected-node count mismatch: {evidence_id}")
            _require(
                set(node_subject) <= set(nodes),
                f"pytest evidence {evidence_id} coverage cites a node outside its collected-node index",
            )
            if v2:
                expected_artifact_kinds = {"junit_xml", "stdout", "stderr", "profile_report"}
                _require(
                    set(artifacts_by_kind) == expected_artifact_kinds and len(artifacts) == len(expected_artifact_kinds),
                    f"pytest evidence {evidence_id} must retain exactly the four typed result artifacts",
                )
                junit_record, junit_path = artifacts_by_kind["junit_xml"]
                profile_record, profile_path = artifacts_by_kind["profile_report"]
                selectors = _validate_pytest_command(
                    record,
                    assessment,
                    root,
                    junit_relative=_string(junit_record.get("path"), "JUnit artifact path"),
                    profile_relative=_string(profile_record.get("path"), "profile artifact path"),
                )
                junit_nodes, junit_outcomes = _junit_node_ids(junit_path, evidence_id)
                _require(junit_nodes == nodes, f"pytest evidence {evidence_id} JUnit node identities do not match the retained index")
                _require(
                    all(any(_node_matches_selector(node_id, selector) for selector in selectors) for node_id in junit_nodes),
                    f"pytest evidence {evidence_id} JUnit node identity is outside the recorded selectors",
                )
                profile, machine_counts = _validate_profile_report(
                    profile_path,
                    evidence_id,
                    profile_case_by_id,
                    nodes,
                    junit_outcomes,
                )
                _require(
                    machine_counts == counts,
                    f"pytest evidence {evidence_id} machine-reported outcome counts do not match result_counts",
                )
                _require(
                    {key[3] for key in coverage} <= {profile["profile_case_id"]},
                    f"evidence {evidence_id} runtime profile does not match coverage",
                )
            else:
                names = [Path(str(_dict(item, "artifact").get("path"))).name for item in artifacts]
                _require(
                    sum(name.endswith(".junit.xml") for name in names) == 1,
                    f"pytest evidence {evidence_id} must retain one JUnit XML",
                )
                _require(len(artifacts) == 3, f"pytest evidence {evidence_id} must retain exactly three result artifacts")
                junit_paths = [
                    _repository_path(root, _dict(item, "artifact").get("path"), "JUnit artifact")
                    for item in artifacts
                    if str(_dict(item, "artifact").get("path", "")).endswith(".junit.xml")
                ]
                _require(len(junit_paths) == 1, f"pytest evidence {evidence_id} must retain one JUnit XML")
                junit_root = ET.parse(junit_paths[0]).getroot()
                suites = (
                    [junit_root]
                    if junit_root.tag.endswith("testsuite")
                    else [child for child in junit_root if child.tag.endswith("testsuite")]
                )
                junit_counts = {
                    key: sum(int(suite.attrib.get(key, 0)) for suite in suites) for key in ("tests", "failures", "errors", "skipped")
                }
                expected_tests = sum(int(counts.get(key, 0)) for key in ("passed", "failed", "errors", "skipped", "xfailed", "xpassed"))
                _require(
                    junit_counts["tests"] == expected_tests == record["collected_nodes"],
                    f"pytest result total mismatch: {evidence_id}",
                )
                _require(junit_counts["failures"] == counts.get("failed", 0), f"pytest failure count mismatch: {evidence_id}")
                _require(junit_counts["errors"] == counts.get("errors", 0), f"pytest error count mismatch: {evidence_id}")
                _require(
                    junit_counts["skipped"] == counts.get("skipped", 0) + counts.get("xfailed", 0),
                    f"pytest skipped count mismatch: {evidence_id}",
                )
            if (
                record["exit_code"] == 0
                and counts["passed"] > 0
                and counts["passed"] == record["collected_nodes"]
                and all(counts[key] == 0 for key in ("failed", "errors", "skipped", "xfailed", "xpassed"))
            ):
                promotable_evidence.add(evidence_id)
    return evidence_by_id, coverage_by_evidence, promotable_evidence


def validate_evidence_artifacts(assessment_path: Path) -> int:
    """Statically validate retained evidence without executing project-controlled code."""

    assessment_path = assessment_path.resolve()
    root = _git_root(assessment_path)
    assessment = load_unique_json(assessment_path)
    catalog_record = _dict(assessment.get("catalog"), "assessment catalog")
    catalog_path = _repository_path(root, catalog_record.get("path"), "catalog.path")
    catalog = load_unique_json(catalog_path)
    validate_catalog(catalog, catalog_path)
    _validate_assessment_schema(assessment, catalog, assessment_path)
    _validate_baseline(assessment, root)
    _validate_environment(assessment)
    evidence_by_id, _coverage, _promotable = _validate_evidence(assessment, catalog, root)
    return sum(record["kind"] == "pytest" for record in evidence_by_id.values())


def _validate_review(assessment: dict[str, Any], assessment_path: Path, root: Path) -> None:
    review_path = _repository_path(root, assessment.get("review_record"), "review_record")
    _require(review_path.is_file(), f"review record does not exist: {review_path}")
    _require(review_path == (assessment_path.parent / "review.md").resolve(), "review_record must be the assessment's sibling review.md")
    text = review_path.read_text(encoding="utf-8")
    completed = re.findall(r"^Review outcome: complete$", text, re.MULTILINE)
    _require(len(completed) == 1, f"review is not complete: {review_path}")
    _require(not re.search(r"(?i)\boutcome:\s*pending\b", text), f"review remains pending: {review_path}")


def _validate_placeholders(assessment_path: Path, root: Path) -> None:
    state_engine_root = root / "docs/architecture/state_engine"
    documents = [
        state_engine_root / "README.md",
        state_engine_root / "architecture.md",
        state_engine_root / "proof-matrix.md",
        state_engine_root / "completeness-criteria.md",
        state_engine_root / "assessment-framework.md",
        assessment_path.parent / "README.md",
        assessment_path.parent / "evidence.md",
        assessment_path.parent / "review.md",
    ]
    findings: list[str] = []
    for document in documents:
        if not document.is_file():
            continue
        for line_number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), start=1):
            for match in PLACEHOLDER_PATTERN.finditer(line):
                findings.append(f"{document.relative_to(root)}:{line_number}:{match.group(0)}")
    _require(not findings, "unresolved placeholder found:\n" + "\n".join(findings))


def _validate_versioned_assessment_schema(
    assessment: dict[str, Any],
    catalog_id: str,
    expected_schema_version: int,
) -> None:
    _require(
        type(assessment.get("schema_version")) is int and assessment["schema_version"] == expected_schema_version,
        f"unsupported assessment version pair (schema_version): {catalog_id}/schema-{assessment.get('schema_version')}",
    )
    _require(set(assessment) == V2_ASSESSMENT_TOP_LEVEL_KEYS, "assessment top-level schema has unexpected fields")
    assessment_id = _string(assessment.get("assessment_id"), "assessment_id")
    _require(bool(ASSESSMENT_ID_PATTERN.fullmatch(assessment_id)), "assessment_id has invalid characters or length")
    catalog_record = _dict(assessment.get("catalog"), "assessment catalog")
    _require(
        set(catalog_record) == {"path", "catalog_id", "schema_version", "sha256_at_evidence_capture"},
        "assessment catalog has unexpected fields",
    )
    _require(
        type(catalog_record.get("schema_version")) is int,
        "assessment catalog schema_version must be an integer",
    )
    limitations = _strings(assessment.get("limitations"), "limitations")
    _unique(limitations, "limitations")
    _string(assessment.get("review_record"), "review_record")
    legs = _list(assessment.get("legs"), "assessment legs")
    for index, raw_leg in enumerate(legs):
        leg = _dict(raw_leg, f"assessment leg {index}")
        _require(
            set(leg) == {"id", "derived_verdict", "default_status", "reason", "owner_issue", "exit_gate", "overrides"},
            f"assessment leg {index} has unexpected fields",
        )


def _validate_assessment_schema(
    assessment: dict[str, Any],
    catalog: dict[str, Any],
    assessment_path: Path,
) -> None:
    catalog_id = _string(catalog.get("catalog_id"), "catalog_id")
    expected = _assessment_schema_version(catalog_id)
    if expected == 1:
        _require(
            type(assessment.get("schema_version")) is int and assessment["schema_version"] == 1,
            f"unsupported assessment version pair (schema_version): {catalog_id}/schema-{assessment.get('schema_version')}",
        )
        return
    _validate_versioned_assessment_schema(assessment, catalog_id, expected)
    _validate_completion_marker(assessment_path, expected)
    derived = _dict(assessment.get("derived"), "derived")
    _require(
        set(derived) == {"family_counts", "total", "overall_verdict", "reason"},
        "derived has unexpected fields",
    )
    reason = _string(derived.get("reason"), "derived reason")
    _require(bool(reason.strip()), "derived reason must be non-empty")
    for context, raw_counts in [
        *((f"derived family {family}", value) for family, value in _dict(derived.get("family_counts"), "derived family_counts").items()),
        ("derived total", derived.get("total")),
    ]:
        counts = _dict(raw_counts, context)
        _require(set(counts) == VERDICTS, f"{context} has wrong verdict keys")
        _require(
            all(type(count) is int and count >= 0 for count in counts.values()),
            f"{context} counts must be non-negative integers",
        )


def _validate_unresolved_metadata(value: dict[str, Any], context: str) -> None:
    for field in ("reason", "exit_gate"):
        field_value = value.get(field)
        _require(
            isinstance(field_value, str) and bool(field_value.strip()),
            f"{context} {field} must be a non-empty string",
        )
    owner_issue = value.get("owner_issue")
    _require(
        owner_issue is None or (isinstance(owner_issue, str) and bool(owner_issue.strip())),
        f"{context} owner_issue must be null or a non-empty string",
    )


def _changed_key(item: dict[str, Any], v2: bool) -> tuple[str, str, str, str]:
    return _coverage_key(item, v2)


def _normalized_cells(
    document: dict[str, Any], catalog: dict[str, Any]
) -> dict[tuple[str, str, str, str], tuple[str, tuple[tuple[str, str], ...]]]:
    digests = {
        record["id"]: hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        for record in document["evidence"]
    }
    legs = {leg["id"]: leg for leg in document["legs"]}
    result: dict[tuple[str, str, str, str], tuple[str, tuple[tuple[str, str], ...]]] = {}
    for catalog_leg in catalog["legs"]:
        leg = legs[catalog_leg["id"]]
        for dimension in DIMENSIONS:
            for case_id in _semantic_cases(catalog_leg):
                for profile_case in _profile_cases(catalog, catalog_leg):
                    status = (
                        leg["default_status"] if _cell_is_applicable(catalog, catalog_leg, dimension, profile_case) else "not_applicable"
                    )
                    result[(leg["id"], dimension, case_id, profile_case)] = (status, ())
        for override in leg.get("overrides", []):
            profile_case = override.get("profile_case", "historical-v1")
            evidence_payload = tuple((evidence_id, digests[evidence_id]) for evidence_id in override.get("evidence", []))
            result[(leg["id"], override["dimension"], override["case"], profile_case)] = (
                override["status"],
                evidence_payload,
            )
    return result


def _validate_proof_matrix(
    assessment: dict[str, Any],
    catalog: dict[str, Any],
    actual: dict[str, Counter[str]],
    total: Counter[str],
    root: Path,
) -> None:
    matrix_path = root / "docs/architecture/state_engine/proof-matrix.md"
    _require(matrix_path.is_file(), f"proof matrix does not exist: {matrix_path}")
    matrix = matrix_path.read_text(encoding="utf-8")
    for family, label in FAMILY_LABELS.items():
        match = re.search(
            rf"^\| {re.escape(label)} \| (\d+) \| (\d+) \| (\d+) \| (\d+) \|",
            matrix,
            re.MULTILINE,
        )
        if match is None:
            _fail(f"proof matrix lacks family row: {label}")
        stated = tuple(map(int, match.groups()))
        expected = (
            sum(actual[family].values()),
            actual[family]["confirmed"],
            actual[family]["gap"],
            actual[family]["unknown"],
        )
        _require(stated == expected, f"proof matrix family totals are false: {label}")
    total_match = re.search(
        r"^\| \*\*Total\*\* \| \*\*(\d+)\*\* \| \*\*(\d+)\*\* \| \*\*(\d+)\*\* \| \*\*(\d+)\*\* \|",
        matrix,
        re.MULTILINE,
    )
    if total_match is None:
        _fail("proof matrix lacks total row")
    stated_total = tuple(map(int, total_match.groups()))
    expected_total = (
        len(assessment["legs"]),
        total["confirmed"],
        total["gap"],
        total["unknown"],
    )
    _require(stated_total == expected_total, "proof matrix total is false")


def validate_package(assessment_path: Path) -> tuple[int, str]:
    _require(__debug__, "assessment validation requires Python assertions enabled")
    assessment_path = assessment_path.resolve()
    root = _git_root(assessment_path)
    assessment = load_unique_json(assessment_path)
    catalog_record = _dict(assessment.get("catalog"), "assessment catalog")
    catalog_path = _repository_path(root, catalog_record.get("path"), "catalog.path")
    catalog = load_unique_json(catalog_path)
    validate_catalog(catalog, catalog_path)
    _validate_assessment_schema(assessment, catalog, assessment_path)
    expected_ids = _expected_leg_ids(catalog["catalog_id"])
    _require(catalog_record.get("catalog_id") == catalog["catalog_id"], "assessment catalog_id mismatch")
    _require(catalog_record.get("schema_version") == catalog["schema_version"], "assessment catalog schema mismatch")
    _require(catalog_record.get("sha256_at_evidence_capture") == _sha256(catalog_path), "assessment catalog digest mismatch")

    legs = _list(assessment.get("legs"), "assessment legs")
    leg_ids = [_dict(leg, f"assessment leg {index}").get("id") for index, leg in enumerate(legs)]
    _require(leg_ids == expected_ids, "assessment leg identity or ordering mismatch")
    mode = assessment.get("mode")
    _require(mode in {"full", "delta"}, "assessment mode must be full or delta")
    if mode == "full":
        _require(assessment.get("parent_assessment") is None, "full assessment cannot name a parent")
        _require(not assessment.get("changed_tuples"), "full assessment cannot declare changed_tuples")
        _require(not assessment.get("changed_gate_ids"), "full assessment cannot declare changed_gate_ids")
    else:
        _require(bool(assessment.get("parent_assessment")), "delta assessment must name a parent")
        _require(bool(assessment.get("changed_tuples")), "delta assessment must declare changed_tuples")
        _require(assessment.get("derived", {}).get("overall_verdict") != "complete", "delta assessment cannot declare completeness")

    _validate_baseline(assessment, root)
    _validate_environment(assessment)
    _validate_review(assessment, assessment_path, root)
    _validate_placeholders(assessment_path, root)
    evidence_by_id, coverage_by_evidence, promotable_evidence = _validate_evidence(assessment, catalog, root)
    evidence_ids = set(evidence_by_id)
    catalog_by_id = {leg["id"]: leg for leg in catalog["legs"]}
    v2 = catalog["catalog_id"] == "elspeth-state-engine-v2"

    raw_changed = _list(assessment.get("changed_tuples", []), "changed_tuples")
    changed_keys: set[tuple[str, str, str, str]] = set()
    for raw_item in raw_changed:
        item = _dict(raw_item, "changed tuple")
        if v2:
            _require(
                set(item) == {"leg_id", "dimension_id", "case_id", "profile_case"},
                "changed tuple has unexpected fields",
            )
        changed_keys.add(_changed_key(item, v2))
    _require(len(changed_keys) == len(raw_changed), "changed_tuples contains duplicates")
    for leg_id, dimension, case_id, profile_case in changed_keys:
        _require(leg_id in catalog_by_id, "changed_tuples names an unknown leg")
        _require(dimension in DIMENSIONS, "changed_tuples names an unknown dimension")
        _require(case_id in _semantic_cases(catalog_by_id[leg_id]), "changed_tuples names an unknown semantic case")
        _require(profile_case in _profile_cases(catalog, catalog_by_id[leg_id]), "changed_tuples names an unknown profile case")

    gates = _list(assessment.get("hard_gates"), "hard_gates")
    gate_ids = [_dict(gate, f"hard gate {index}").get("id") for index, gate in enumerate(gates)]
    _require(gate_ids == HARD_GATE_IDS, "assessment hard-gate identity or ordering mismatch")
    changed_gate_ids = _strings(assessment.get("changed_gate_ids", []), "changed_gate_ids")
    _unique(changed_gate_ids, "changed_gate_ids")
    _require(set(changed_gate_ids) <= set(HARD_GATE_IDS), "changed_gate_ids names an unknown gate")
    for gate in gates:
        _require(
            set(gate) == {"id", "status", "support", "affected_leg_ids", "reason"},
            f"hard gate {gate.get('id')} has unexpected fields",
        )
        _require(gate.get("status") in {"open", "closed", "unknown"}, f"hard gate {gate['id']} has invalid status")
        reason = gate.get("reason")
        _require(
            isinstance(reason, str) and bool(reason.strip()),
            f"hard gate {gate['id']} reason must be a non-empty string",
        )
        support = _strings(gate.get("support"), f"hard gate {gate['id']} support")
        _unique(support, f"hard gate {gate['id']} support")
        _require(set(support) <= evidence_ids, f"hard gate {gate['id']} cites unknown evidence")
        affected = _strings(gate.get("affected_leg_ids"), f"hard gate {gate['id']} affected legs")
        _unique(affected, f"hard gate {gate['id']} affected legs")
        _require(set(affected) <= set(expected_ids), f"hard gate {gate['id']} affects unknown leg")

    derived_by_id: dict[str, str] = {}
    cell_status_by_leg: dict[str, dict[tuple[str, str, str], str]] = {}
    cell_evidence_by_leg: dict[str, dict[tuple[str, str, str], list[str]]] = {}
    for leg in legs:
        leg_id = leg["id"]
        _require(leg.get("derived_verdict") in VERDICTS, f"leg {leg_id} has invalid derived_verdict")
        _require(leg.get("default_status") == "unknown", f"leg {leg_id} default_status must be unknown")
        for field in ("reason", "owner_issue", "exit_gate"):
            _require(field in leg, f"leg {leg_id} is missing {field}")
        if leg["derived_verdict"] in {"gap", "unknown"}:
            _validate_unresolved_metadata(leg, f"leg {leg_id} unresolved")
        catalog_leg = catalog_by_id[leg_id]
        cell_status: dict[tuple[str, str, str], str] = {}
        cell_evidence: dict[tuple[str, str, str], list[str]] = {}
        for dimension in DIMENSIONS:
            for case_id in _semantic_cases(catalog_leg):
                for profile_case in _profile_cases(catalog, catalog_leg):
                    status = (
                        leg["default_status"] if _cell_is_applicable(catalog, catalog_leg, dimension, profile_case) else "not_applicable"
                    )
                    cell_status[(dimension, case_id, profile_case)] = status
                    cell_evidence[(dimension, case_id, profile_case)] = []
        seen: set[tuple[str, str, str]] = set()
        for raw_override in _list(leg.get("overrides", []), f"leg {leg_id} overrides"):
            override = _dict(raw_override, f"leg {leg_id} override")
            profile_case = _string(override.get("profile_case", "historical-v1"), "override profile_case")
            key = (
                _string(override.get("dimension"), "override dimension"),
                _string(override.get("case"), "override case"),
                profile_case,
            )
            _require(key not in seen, f"leg {leg_id} has duplicate override")
            seen.add(key)
            _require(key in cell_status, f"leg {leg_id} override names an unknown proof cell")
            status = _string(override.get("status"), "override status")
            _require(status in CELL_STATUSES, f"leg {leg_id} override has invalid status")
            if v2:
                base_fields = {"dimension", "case", "profile_case", "status", "evidence"}
                unresolved_fields = {"reason", "owner_issue", "exit_gate"} if status in {"partial", "fail", "unknown"} else set()
                _require(
                    set(override) == base_fields | unresolved_fields,
                    f"leg {leg_id} {status} override has unexpected fields",
                )
            cited = _strings(override.get("evidence", []), f"leg {leg_id} override evidence")
            cell_is_applicable = _cell_is_applicable(catalog, catalog_leg, key[0], key[2])
            if not cell_is_applicable:
                _require(
                    status == "not_applicable",
                    f"leg {leg_id} catalog not_applicable cell must remain not_applicable",
                )
                _require(not cited, f"leg {leg_id} not_applicable override cannot cite evidence")
            if status in {"pass", "partial", "fail"}:
                _require(bool(cited), f"leg {leg_id} {status} override needs evidence")
            if status in {"partial", "fail", "unknown"}:
                for field in ("reason", "owner_issue", "exit_gate"):
                    _require(field in override, f"leg {leg_id} {status} override is missing {field}")
                _validate_unresolved_metadata(override, f"leg {leg_id} {status} override")
            if status == "not_applicable":
                _require(
                    not cell_is_applicable,
                    f"leg {leg_id} cannot waive a required profile cell",
                )
                _require(not cited, f"leg {leg_id} not_applicable override cannot cite evidence")
            _require(set(cited) <= evidence_ids, f"leg {leg_id} override cites unknown evidence")
            for evidence_id in cited:
                coverage_key = (leg_id, key[0], key[1], key[2])
                _require(coverage_key in coverage_by_evidence[evidence_id], f"leg {leg_id} evidence does not cover its override")
                record = evidence_by_id[evidence_id]
                if record["kind"] == "documentation" and status == "fail":
                    _fail(f"leg {leg_id} documentation evidence cannot establish behavioral fail")
                if status in {"pass", "partial"}:
                    if record["kind"] == "documentation":
                        _fail(f"leg {leg_id} documentation evidence cannot promote a behavioral proof cell")
                    counts = _dict(record["result_counts"], f"evidence {evidence_id} result_counts")
                    if any(counts.get(key, 0) for key in ("skipped", "xfailed", "xpassed")):
                        _fail(f"leg {leg_id} skipped pytest evidence cannot promote a behavioral proof cell")
                    _require(
                        evidence_id in promotable_evidence,
                        f"leg {leg_id} relies on non-promotable pytest evidence",
                    )
            cell_status[key] = status
            cell_evidence[key] = cited
        values = set(cell_status.values())
        if "fail" in values:
            verdict = "gap"
        elif values <= {"pass", "not_applicable"}:
            verdict = "confirmed"
        else:
            verdict = "unknown"
        _require(leg["derived_verdict"] == verdict, f"leg {leg_id} derived verdict is false")
        derived_by_id[leg_id] = verdict
        cell_status_by_leg[leg_id] = cell_status
        cell_evidence_by_leg[leg_id] = cell_evidence

    catalog_gate_by_id = {gate["id"]: gate for gate in catalog["hard_gates"]}
    for gate in gates:
        gate_id = gate["id"]
        mapped_dimensions = set(catalog_gate_by_id[gate_id].get("dimensions", DIMENSIONS))
        mapped_statuses: list[str] = []
        expected_affected: list[str] = []
        expected_support: list[str] = []
        for leg_id in expected_ids:
            leg_has_unresolved = False
            for key, status in cell_status_by_leg[leg_id].items():
                if key[0] not in mapped_dimensions or status == "not_applicable":
                    continue
                mapped_statuses.append(status)
                if status != "pass":
                    leg_has_unresolved = True
                for evidence_id in cell_evidence_by_leg[leg_id][key]:
                    if evidence_id not in expected_support:
                        expected_support.append(evidence_id)
            if leg_has_unresolved:
                expected_affected.append(leg_id)
        if "fail" in mapped_statuses:
            expected_status = "open"
        elif any(status in {"unknown", "partial"} for status in mapped_statuses):
            expected_status = "open" if gate_id == "HG-09-mandatory-leg-unresolved" else "unknown"
        else:
            expected_status = "closed"
        _require(
            gate["status"] == expected_status,
            f"hard gate {gate_id} does not match derived proof cells",
        )
        _require(
            gate["affected_leg_ids"] == expected_affected,
            f"hard gate {gate_id} affected legs do not match derived proof cells",
        )
        _require(
            gate["support"] == expected_support,
            f"hard gate {gate_id} support does not match derived proof cells",
        )
        if expected_status == "closed":
            _require(bool(expected_support), f"hard gate {gate_id} cannot close without executable support")

    if mode == "delta":
        parent_record = _dict(assessment["parent_assessment"], "parent_assessment")
        _require(set(parent_record) == {"path", "sha256"}, "parent_assessment has unexpected fields")
        parent_path = _repository_path(root, parent_record["path"], "parent_assessment.path")
        _require(parent_path.is_file(), f"parent assessment does not exist: {parent_path}")
        _require(_sha256(parent_path) == parent_record["sha256"], "parent assessment digest mismatch")
        parent = load_unique_json(parent_path)
        _require(parent.get("catalog") == assessment.get("catalog"), "delta parent uses a different catalog")
        _require([leg["id"] for leg in parent.get("legs", [])] == expected_ids, "delta parent leg identity mismatch")
        parent_cells = _normalized_cells(parent, catalog)
        current_cells = _normalized_cells(assessment, catalog)
        actual_changes = {key for key in current_cells if current_cells[key] != parent_cells[key]}
        _require(actual_changes == changed_keys, "delta changed_tuples do not match actual cell changes")
        parent_gates = {gate["id"]: gate for gate in parent["hard_gates"]}
        current_gates = {gate["id"]: gate for gate in gates}
        actual_gate_changes = {gate_id for gate_id in current_gates if current_gates[gate_id] != parent_gates[gate_id]}
        _require(actual_gate_changes == set(changed_gate_ids), "delta changed_gate_ids do not match actual gate changes")

    family_for = {leg["id"]: leg["family"] for leg in catalog["legs"]}
    actual: dict[str, Counter[str]] = defaultdict(Counter)
    for leg_id, verdict in derived_by_id.items():
        actual[family_for[leg_id]][verdict] += 1
    derived = _dict(assessment.get("derived"), "derived")
    family_counts = _dict(derived.get("family_counts"), "derived family_counts")
    _require(set(family_counts) == set(FAMILY_LABELS), "derived family_counts has wrong families")
    for family, raw_expected in family_counts.items():
        expected = _dict(raw_expected, f"derived family {family}")
        _require(set(expected) == VERDICTS, f"derived family {family} has wrong verdict keys")
        calculated = {verdict: actual[family][verdict] for verdict in VERDICTS}
        _require(expected == calculated, f"derived family total is false: {family}")
    total = Counter(derived_by_id.values())
    stated_total = _dict(derived.get("total"), "derived total")
    _require(set(stated_total) == VERDICTS, "derived total has wrong verdict keys")
    calculated_total = {verdict: total[verdict] for verdict in VERDICTS}
    _require(stated_total == calculated_total, "derived total is false")
    gate_statuses = {gate["status"] for gate in gates}
    if total["gap"] or "open" in gate_statuses:
        overall = "not_complete"
    elif total["unknown"] or "unknown" in gate_statuses:
        overall = "insufficient_evidence"
    else:
        overall = "complete"
    _require(derived.get("overall_verdict") == overall, "derived overall verdict is false")
    _validate_proof_matrix(assessment, catalog, actual, total, root)
    return len(legs), overall
