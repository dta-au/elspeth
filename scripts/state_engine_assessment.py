#!/usr/bin/env python3
"""Create and validate reproducible state-engine proof packages."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, NoReturn, cast

DIMENSIONS = [
    "production_entry",
    "precondition_image",
    "success_effects",
    "guard_refusal",
    "zero_mutation_rollback",
    "concurrency",
    "crash_restart",
    "boundary_composition",
    "read_model_truth_table",
    "maintenance",
]
FAMILY_LABELS = {
    "transition": "Token transitions",
    "auxiliary": "Auxiliary state",
    "run_coordination": "Run coordination",
    "production_boundary": "Production boundaries",
    "read_model": "Read models",
    "forbidden": "Forbidden paths",
}
HARD_GATE_IDS = [
    "HG-01-invalid-subtype",
    "HG-02-authority-downgrade",
    "HG-03-double-winner",
    "HG-04-refusal-mutation",
    "HG-05-state-evidence-atomicity",
    "HG-06-restart-loss-or-duplicate",
    "HG-07-read-model-unproved-arm",
    "HG-08-plugin-lifecycle-durability",
    "HG-09-mandatory-leg-unresolved",
    "HG-10-normative-contract-drift",
]
VERDICTS = {"confirmed", "gap", "unknown"}
CELL_STATUSES = {"pass", "partial", "fail", "unknown", "not_applicable"}
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
GIT_OBJECT_PATTERN = re.compile(r"[0-9a-f]{40}")
PB10_CASES = [
    "all-branches-arrive",
    "branch-loss-before-first-arrival",
    "branch-loss-after-partial-arrival",
    "timeout",
    "late-arrival-after-release",
    "restart-before-release",
    "restart-after-release-before-sink",
]
SUPPORTED_DEPLOYMENTS = [
    "single-process-leader",
    "same-host-leader-plus-claim-only-followers",
    "web-hosted-leader-plus-same-host-cli-followers",
    "aws-single-leader-landscape",
]
SUPPORTED_LIFECYCLE_MODES = [
    "fresh",
    "resume",
    "follower",
    "partial-start-failure",
    "normal-teardown",
    "exceptional-teardown",
    "leader-takeover",
]
SUPPORTED_STATE_STORES = {
    "sqlite-wal": SUPPORTED_DEPLOYMENTS[:3],
    "postgresql-16": ["aws-single-leader-landscape"],
}
SUPPORTED_PROFILE_CASES = {
    "sqlite-wal-single-process-leader": (
        "sqlite-wal",
        "single-process-leader",
        [mode for mode in SUPPORTED_LIFECYCLE_MODES if mode != "follower"],
    ),
    "sqlite-wal-same-host-leader-plus-claim-only-followers": (
        "sqlite-wal",
        "same-host-leader-plus-claim-only-followers",
        SUPPORTED_LIFECYCLE_MODES,
    ),
    "sqlite-wal-web-hosted-leader-plus-same-host-cli-followers": (
        "sqlite-wal",
        "web-hosted-leader-plus-same-host-cli-followers",
        SUPPORTED_LIFECYCLE_MODES,
    ),
    "postgresql-16-aws-single-leader-landscape": (
        "postgresql-16",
        "aws-single-leader-landscape",
        [mode for mode in SUPPORTED_LIFECYCLE_MODES if mode != "follower"],
    ),
}


class ContractError(ValueError):
    """Raised when a catalog or assessment violates the proof contract."""


def load_unique_json(path: Path) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key in {path}: {key}")
            result[key] = value
        return result

    with path.open(encoding="utf-8") as handle:
        value = json.load(handle, object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _fail(message: str) -> NoReturn:
    raise ContractError(message)


def _require(condition: bool, message: str) -> None:
    if not condition:
        _fail(message)


def _list(value: Any, context: str) -> list[Any]:
    _require(isinstance(value, list), f"{context} must be a list")
    return cast(list[Any], value)


def _dict(value: Any, context: str) -> dict[str, Any]:
    _require(isinstance(value, dict), f"{context} must be an object")
    return cast(dict[str, Any], value)


def _string(value: Any, context: str) -> str:
    _require(isinstance(value, str), f"{context} must be a string")
    return cast(str, value)


def _strings(value: Any, context: str) -> list[str]:
    items = _list(value, context)
    _require(all(isinstance(item, str) for item in items), f"{context} must contain strings")
    return cast(list[str], items)


def _unique(values: list[str], context: str) -> None:
    _require(len(values) == len(set(values)), f"{context} contains duplicates")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_root(path: Path) -> Path:
    candidate = path if path.is_dir() else path.parent
    while not candidate.exists():
        candidate = candidate.parent
    result = subprocess.run(
        ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    _require(result.returncode == 0, f"not inside a Git repository: {path}")
    return Path(result.stdout.strip()).resolve()


def _repository_path(root: Path, value: Any, context: str) -> Path:
    _require(isinstance(value, str), f"{context} must be a path string")
    path = Path(value)
    _require(not path.is_absolute(), f"{context} must be repository-relative")
    resolved = (root / path).resolve()
    _require(resolved.is_relative_to(root), f"{context} escapes the repository")
    return resolved


def _expected_leg_ids(catalog_id: Any) -> list[str]:
    _require(
        catalog_id in {"elspeth-state-engine-v1", "elspeth-state-engine-v2"},
        f"unsupported catalog_id: {catalog_id}",
    )
    v2 = catalog_id == "elspeth-state-engine-v2"
    return (
        [f"TS-{number:02d}" for number in range(20 if v2 else 19)]
        + [f"AUX-{number:02d}" for number in range(1, 8)]
        + [f"RC-{number:02d}" for number in range(1, 8)]
        + [f"PB-{number:02d}" for number in range(1, 11 if v2 else 10)]
        + [f"RM-{number:02d}" for number in range(1, 15 if v2 else 14)]
        + [f"F-{number:02d}" for number in range(1, 15 if v2 else 14)]
    )


def _live_plugin_inventory() -> dict[str, list[str]]:
    from elspeth.plugins.infrastructure.discovery import discover_all_plugins

    return {kind: sorted(cast(Any, plugin).name for plugin in plugin_classes) for kind, plugin_classes in discover_all_plugins().items()}


def _validate_profile_cases(catalog: dict[str, Any]) -> list[str]:
    profiles = _dict(catalog["execution_profiles"], "execution_profiles")
    stores = _list(profiles.get("state_store"), "execution_profiles.state_store")
    store_by_id: dict[str, dict[str, Any]] = {}
    for index, raw_store in enumerate(stores):
        store = _dict(raw_store, f"state_store[{index}]")
        store_id = _string(store.get("id"), f"state_store[{index}].id")
        _require(store_id not in store_by_id, f"duplicate state-store profile: {store_id}")
        store_by_id[store_id] = store

    actual_store_deployments = {
        store_id: _strings(store.get("deployments"), f"state-store profile {store_id} deployments")
        for store_id, store in store_by_id.items()
    }
    _require(
        actual_store_deployments == SUPPORTED_STATE_STORES and all(store.get("required") is True for store in store_by_id.values()),
        "catalog supported state-store profiles do not match the v2 contract",
    )

    deployments = _strings(profiles.get("deployments"), "execution_profiles.deployments")
    lifecycle_modes = _strings(profiles.get("lifecycle_modes"), "execution_profiles.lifecycle_modes")
    _unique(deployments, "execution_profiles.deployments")
    _unique(lifecycle_modes, "execution_profiles.lifecycle_modes")
    _require(deployments == SUPPORTED_DEPLOYMENTS, "catalog supported state-store profiles have deployment drift")
    _require(
        lifecycle_modes == SUPPORTED_LIFECYCLE_MODES,
        "catalog supported state-store profiles have lifecycle drift",
    )

    cases = _list(profiles.get("profile_cases"), "execution_profiles.profile_cases")
    case_ids: list[str] = []
    declared_pairs: set[tuple[str, str]] = set()
    for index, raw_case in enumerate(cases):
        case = _dict(raw_case, f"profile case {index}")
        _require(
            set(case) == {"id", "state_store", "deployment", "lifecycle_modes"},
            f"profile case {index} has unexpected fields",
        )
        case_id = _string(case.get("id"), f"profile case {index} id")
        store_id = _string(case.get("state_store"), f"profile case {case_id} state_store")
        deployment = _string(case.get("deployment"), f"profile case {case_id} deployment")
        _require(store_id in store_by_id, f"profile case {case_id} references unknown state store")
        _require(deployment in deployments, f"profile case {case_id} references unknown deployment")
        allowed_deployments = _strings(
            store_by_id[store_id].get("deployments"),
            f"state-store profile {store_id} deployments",
        )
        _require(
            deployment in allowed_deployments,
            f"profile case {case_id} is not supported by state store {store_id}",
        )
        modes = _strings(case.get("lifecycle_modes"), f"profile case {case_id} lifecycle_modes")
        _unique(modes, f"profile case {case_id} lifecycle_modes")
        _require(
            set(modes) <= set(lifecycle_modes),
            f"profile case {case_id} references unknown lifecycle mode",
        )
        case_ids.append(case_id)
        declared_pairs.add((store_id, deployment))
    _unique(case_ids, "execution_profiles.profile_cases")

    actual_cases = {case["id"]: (case["state_store"], case["deployment"], case["lifecycle_modes"]) for case in cases}
    _require(
        actual_cases == SUPPORTED_PROFILE_CASES,
        "catalog supported state-store profiles do not match the four v2 profile cases",
    )

    expected_pairs = {
        (store_id, deployment)
        for store_id, store in store_by_id.items()
        for deployment in _strings(store.get("deployments"), f"state-store profile {store_id} deployments")
    }
    _require(
        declared_pairs == expected_pairs and len(case_ids) == len(expected_pairs),
        "profile cases must cover every supported state-store/deployment pair exactly once",
    )
    return case_ids


def validate_catalog(catalog: dict[str, Any], catalog_path: Path) -> None:
    _require(catalog.get("schema_version") == 1, "catalog schema_version must be 1")
    catalog_id = catalog.get("catalog_id")
    expected_ids = _expected_leg_ids(catalog_id)
    _require(catalog.get("dimensions") == DIMENSIONS, "catalog dimensions do not match the contract")
    _require(
        catalog.get("required_leg_ids") == expected_ids,
        "catalog required_leg_ids do not match the catalog version",
    )

    legs = _list(catalog.get("legs"), "catalog legs")
    leg_ids = [_dict(leg, f"catalog leg {index}").get("id") for index, leg in enumerate(legs)]
    _require(leg_ids == expected_ids, "catalog leg ordering does not match required_leg_ids")
    _require(len(set(leg_ids)) == len(leg_ids), "catalog leg IDs contain duplicates")
    catalog_by_id = {leg["id"]: leg for leg in legs}
    for leg_id, leg in catalog_by_id.items():
        _require(leg.get("family") in FAMILY_LABELS, f"catalog leg {leg_id} has unknown family")
        cases = _strings(leg.get("required_cases", ["leg-contract"]), f"catalog leg {leg_id} cases")
        _require(bool(cases), f"catalog leg {leg_id} has no required cases")
        _unique(cases, f"catalog leg {leg_id} required_cases")
    if catalog_id == "elspeth-state-engine-v2":
        _require(
            catalog_by_id["PB-10"].get("required_cases") == PB10_CASES,
            "PB-10 required_cases do not match the row-union contract",
        )

    families = {leg["family"] for leg in legs}
    acceptance = _dict(catalog.get("family_dimension_acceptance"), "family acceptance")
    _require(set(acceptance) == families, "family acceptance does not cover catalog families")
    for family, raw_dimensions in acceptance.items():
        dimensions = _dict(raw_dimensions, f"family acceptance {family}")
        _require(list(dimensions) == DIMENSIONS, f"family acceptance {family} is out of order")

    profiles = _dict(catalog.get("execution_profiles"), "execution_profiles")
    plugin_inventory = _dict(profiles.get("first_party_plugins"), "first-party plugin inventory")
    _require(
        set(plugin_inventory) == {"sources", "transforms", "sinks", "inventory_rule"},
        "first-party plugin inventory has unexpected fields",
    )
    live_plugins = _live_plugin_inventory()
    for kind in ("sources", "transforms", "sinks"):
        names = _strings(plugin_inventory.get(kind), f"first-party plugin inventory {kind}")
        _require(names == sorted(set(names)), f"first-party plugin inventory {kind} is not sorted/unique")
        _require(names == live_plugins[kind], f"first-party plugin inventory {kind} is stale")

    applicability = _dict(catalog.get("applicability_profiles"), "applicability_profiles")
    if catalog_id == "elspeth-state-engine-v2":
        from elspeth.contracts.enums import TerminalPath
        from elspeth.contracts.scheduler import TokenWorkStatus

        _require(
            catalog.get("vocabularies")
            == {
                "token_work_status": [item.value for item in TokenWorkStatus],
                "terminal_path": [item.value for item in TerminalPath],
            },
            "catalog vocabularies do not match owned runtime enums",
        )
        profile_case_ids = _validate_profile_cases(catalog)
        expected_profile_fields = {"default_case_id", "profile_case_ids", *DIMENSIONS}
    else:
        profile_case_ids = []
        expected_profile_fields = {"default_case_id", *DIMENSIONS}
    for profile_id, raw_profile in applicability.items():
        profile = _dict(raw_profile, f"applicability profile {profile_id}")
        _require(
            set(profile) == expected_profile_fields,
            f"applicability profile {profile_id} has unexpected fields",
        )
        _require(
            all(profile[dimension] in {"required", "not_applicable"} for dimension in DIMENSIONS),
            f"applicability profile {profile_id} has an invalid dimension policy",
        )
        if profile_case_ids:
            _require(
                profile.get("profile_case_ids") == profile_case_ids,
                f"applicability profile {profile_id} must preserve profile-case ordering",
            )
    for leg_id, leg in catalog_by_id.items():
        _require(
            leg.get("applicability_profile") in applicability,
            f"catalog leg {leg_id} references unknown applicability profile",
        )

    gates = _list(catalog.get("hard_gates"), "catalog hard_gates")
    gate_ids = [_dict(gate, f"hard gate {index}").get("id") for index, gate in enumerate(gates)]
    _require(gate_ids == HARD_GATE_IDS, "catalog hard-gate identity or ordering changed")
    _require(catalog_path.is_file(), f"catalog file does not exist: {catalog_path}")


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


def initialize_full(assessment_id: str, output_directory: Path) -> Path:
    root = _git_root(output_directory)
    catalog_path = root / "docs/architecture/state_engine/proof-catalog/v2/catalog.json"
    catalog = load_unique_json(catalog_path)
    validate_catalog(catalog, catalog_path)
    output_directory.mkdir(parents=True, exist_ok=True)
    assessment_path = output_directory / "assessment.json"
    _require(not assessment_path.exists(), f"assessment already exists: {assessment_path}")
    relative_directory = output_directory.resolve().relative_to(root).as_posix()
    document = {
        "schema_version": 1,
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
                "status": "unknown",
                "support": [],
                "affected_leg_ids": [],
                "reason": "Not yet evaluated.",
            }
            for gate in catalog["hard_gates"]
        ],
        "derived": _initial_derived(catalog),
        "limitations": [],
        "review_record": f"{relative_directory}/review.md",
    }
    assessment_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    templates = {
        "README.md": "# State Engine Assessment\n\nSee `assessment.json` for the proof manifest.\n",
        "evidence.md": "# Verification Runs\n\nRecord exact commands and retained artifacts here.\n",
        "review.md": "# Assessment Review\n\nReview outcome: pending\n",
    }
    for name, content in templates.items():
        target = output_directory / name
        if not target.exists():
            target.write_text(content, encoding="utf-8")
    return assessment_path


def _validate_baseline(assessment: dict[str, Any], root: Path) -> None:
    baseline = _dict(assessment.get("baseline"), "baseline")
    fields = (
        "repository_root",
        "remote",
        "branch",
        "commit",
        "tree",
        "behavioral_overlay",
        "worktree_status_at_evidence_capture",
        "submodules",
        "worktrees_at_capture",
    )
    for field in fields:
        _require(field in baseline, f"baseline is missing {field}")
    repository_root = baseline["repository_root"]
    _require(isinstance(repository_root, str) and Path(repository_root).is_absolute(), "baseline repository_root must be absolute")
    _require(bool(baseline["remote"]), "baseline remote must be non-empty")
    _require(bool(GIT_OBJECT_PATTERN.fullmatch(str(baseline["commit"]))), "baseline commit is not a full Git object ID")
    _require(bool(GIT_OBJECT_PATTERN.fullmatch(str(baseline["tree"]))), "baseline tree is not a full Git object ID")
    _require(isinstance(baseline["worktree_status_at_evidence_capture"], list), "baseline worktree status must be a list")
    _require(
        baseline["behavioral_overlay"] is None or isinstance(baseline["behavioral_overlay"], dict),
        "baseline behavioral_overlay must be null or an object",
    )
    tree = subprocess.run(
        ["git", "-C", str(root), "rev-parse", f"{baseline['commit']}^{{tree}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    _require(tree.returncode == 0 and tree.stdout.strip() == baseline["tree"], "baseline commit/tree identity does not resolve")
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
    for field in fields:
        _require(field in environment, f"environment is missing {field}")
    for field in ("pyproject_sha256", "uv_lock_sha256"):
        _require(bool(SHA256_PATTERN.fullmatch(str(environment[field]))), f"environment {field} is not SHA-256")
    _require(environment["sensitive_environment_captured"] is False, "sensitive environment values must not be captured")
    for name in ("structure_snapshot", "tracker_snapshot"):
        snapshot = _dict(assessment.get(name), name)
        for field in ("provider", "captured_at", "limitation"):
            _require(field in snapshot, f"{name} is missing {field}")


def _semantic_cases(leg: dict[str, Any]) -> list[str]:
    return _strings(leg.get("required_cases", ["leg-contract"]), f"catalog leg {leg['id']} cases")


def _profile_cases(catalog: dict[str, Any], leg: dict[str, Any]) -> list[str]:
    profile = catalog["applicability_profiles"][leg["applicability_profile"]]
    return _strings(profile.get("profile_case_ids", ["historical-v1"]), "profile-case IDs")


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


def _validate_evidence(
    assessment: dict[str, Any],
    catalog: dict[str, Any],
    root: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, set[tuple[str, str, str, str]]]]:
    catalog_by_id = {leg["id"]: leg for leg in catalog["legs"]}
    v2 = catalog["catalog_id"] == "elspeth-state-engine-v2"
    records = _list(assessment.get("evidence"), "evidence")
    evidence_by_id: dict[str, dict[str, Any]] = {}
    coverage_by_evidence: dict[str, set[tuple[str, str, str, str]]] = {}
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
    for index, raw_record in enumerate(records):
        record = _dict(raw_record, f"evidence[{index}]")
        evidence_id = _string(record.get("id"), f"evidence[{index}].id")
        _require(evidence_id not in evidence_by_id, f"duplicate evidence ID: {evidence_id}")
        for field in required_fields:
            _require(field in record, f"evidence {evidence_id} is missing {field}")
        _require(
            record["reproducibility_class"] in {"deterministic", "semantic_comparison", "external_observation"},
            f"evidence {evidence_id} has invalid reproducibility_class",
        )
        _repository_path(root, record["cwd_relative"], f"evidence {evidence_id} cwd_relative")
        safe_environment = _dict(record["safe_environment"], f"evidence {evidence_id} safe_environment")
        _require(
            all(
                re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) and (value is None or isinstance(value, str))
                for name, value in safe_environment.items()
            ),
            f"evidence {evidence_id} has invalid safe_environment",
        )
        raw_coverage = _list(record["coverage"], f"evidence {evidence_id} coverage")
        coverage = {_coverage_key(_dict(item, f"evidence {evidence_id} coverage item"), v2) for item in raw_coverage}
        _require(len(coverage) == len(raw_coverage), f"evidence {evidence_id} coverage has duplicates")
        for leg_id, dimension, case_id, profile_case in coverage:
            _require(leg_id in catalog_by_id, f"evidence {evidence_id} covers unknown leg")
            _require(dimension in DIMENSIONS, f"evidence {evidence_id} covers unknown dimension")
            _require(case_id in _semantic_cases(catalog_by_id[leg_id]), f"evidence {evidence_id} covers unknown semantic case")
            _require(profile_case in _profile_cases(catalog, catalog_by_id[leg_id]), f"evidence {evidence_id} covers unknown profile case")
        coverage_by_evidence[evidence_id] = coverage
        evidence_by_id[evidence_id] = record

        artifacts = _list(record["retained_artifacts"], f"evidence {evidence_id} artifacts")
        for raw_artifact in artifacts:
            artifact = _dict(raw_artifact, f"evidence {evidence_id} artifact")
            artifact_path = _repository_path(root, artifact.get("path"), f"evidence {evidence_id} artifact path")
            _require(artifact_path.is_file(), f"evidence artifact does not exist: {artifact_path}")
            _require(_sha256(artifact_path) == artifact.get("sha256"), f"evidence artifact digest mismatch: {artifact_path}")
        if record["kind"] == "pytest":
            index_record = _dict(record.get("collected_node_index"), f"evidence {evidence_id} collected_node_index")
            node_path = _repository_path(root, index_record.get("path"), f"evidence {evidence_id} node index")
            _require(node_path.is_file(), f"pytest node index does not exist: {node_path}")
            _require(_sha256(node_path) == index_record.get("sha256"), f"pytest node-index digest mismatch: {node_path}")
            node_count = len(node_path.read_text(encoding="utf-8").splitlines())
            _require(node_count == record.get("collected_nodes"), f"pytest collected-node count mismatch: {evidence_id}")
            names = [Path(str(_dict(item, "artifact").get("path"))).name for item in artifacts]
            _require(any(name.endswith(".junit.xml") for name in names), f"pytest evidence {evidence_id} lacks JUnit XML")
            _require(any(name.endswith(".stdout") for name in names), f"pytest evidence {evidence_id} lacks stdout")
            _require(any(name.endswith(".stderr") for name in names), f"pytest evidence {evidence_id} lacks stderr")
            junit_paths = [
                _repository_path(root, _dict(item, "artifact").get("path"), "JUnit artifact")
                for item in artifacts
                if str(_dict(item, "artifact").get("path", "")).endswith(".junit.xml")
            ]
            _require(len(junit_paths) == 1, f"pytest evidence {evidence_id} must retain one JUnit XML")
            junit_root = ET.parse(junit_paths[0]).getroot()
            suites = (
                [junit_root] if junit_root.tag.endswith("testsuite") else [child for child in junit_root if child.tag.endswith("testsuite")]
            )
            junit_counts = {
                key: sum(int(suite.attrib.get(key, 0)) for suite in suites) for key in ("tests", "failures", "errors", "skipped")
            }
            counts = _dict(record["result_counts"], f"evidence {evidence_id} result_counts")
            expected_tests = sum(int(counts.get(key, 0)) for key in ("passed", "failed", "errors", "skipped", "xfailed", "xpassed"))
            _require(junit_counts["tests"] == expected_tests == record["collected_nodes"], f"pytest result total mismatch: {evidence_id}")
            _require(junit_counts["failures"] == counts.get("failed", 0), f"pytest failure count mismatch: {evidence_id}")
            _require(junit_counts["errors"] == counts.get("errors", 0), f"pytest error count mismatch: {evidence_id}")
            _require(
                junit_counts["skipped"] == counts.get("skipped", 0) + counts.get("xfailed", 0),
                f"pytest skipped count mismatch: {evidence_id}",
            )
    return evidence_by_id, coverage_by_evidence


def _validate_review(assessment: dict[str, Any], assessment_path: Path, root: Path) -> None:
    review_path = _repository_path(root, assessment.get("review_record"), "review_record")
    _require(review_path.is_file(), f"review record does not exist: {review_path}")
    _require(review_path == (assessment_path.parent / "review.md").resolve(), "review_record must be the assessment's sibling review.md")
    text = review_path.read_text(encoding="utf-8")
    completed = re.findall(r"^Review outcome: complete$", text, re.MULTILINE)
    _require(len(completed) == 1, f"review is not complete: {review_path}")
    _require(not re.search(r"(?i)\boutcome:\s*pending\b", text), f"review remains pending: {review_path}")


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
        profile = catalog["applicability_profiles"][catalog_leg["applicability_profile"]]
        for dimension in DIMENSIONS:
            status = leg["default_status"] if profile[dimension] == "required" else "not_applicable"
            for case_id in _semantic_cases(catalog_leg):
                for profile_case in _profile_cases(catalog, catalog_leg):
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
    evidence_by_id, coverage_by_evidence = _validate_evidence(assessment, catalog, root)
    evidence_ids = set(evidence_by_id)
    catalog_by_id = {leg["id"]: leg for leg in catalog["legs"]}
    v2 = catalog["catalog_id"] == "elspeth-state-engine-v2"

    raw_changed = _list(assessment.get("changed_tuples", []), "changed_tuples")
    changed_keys = {_changed_key(_dict(item, "changed tuple"), v2) for item in raw_changed}
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
    open_affected: set[str] = set()
    for gate in gates:
        _require(gate.get("status") in {"open", "closed", "unknown"}, f"hard gate {gate['id']} has invalid status")
        _require(bool(gate.get("reason")), f"hard gate {gate['id']} needs a reason")
        support = _strings(gate.get("support"), f"hard gate {gate['id']} support")
        _require(set(support) <= evidence_ids, f"hard gate {gate['id']} cites unknown evidence")
        affected = _strings(gate.get("affected_leg_ids"), f"hard gate {gate['id']} affected legs")
        _unique(affected, f"hard gate {gate['id']} affected legs")
        _require(set(affected) <= set(expected_ids), f"hard gate {gate['id']} affects unknown leg")
        if gate["status"] == "open":
            open_affected.update(affected)

    derived_by_id: dict[str, str] = {}
    has_unresolved_cell = False
    for leg in legs:
        leg_id = leg["id"]
        _require(leg.get("derived_verdict") in VERDICTS, f"leg {leg_id} has invalid derived_verdict")
        _require(leg.get("default_status") == "unknown", f"leg {leg_id} default_status must be unknown")
        for field in ("reason", "owner_issue", "exit_gate"):
            _require(field in leg, f"leg {leg_id} is missing {field}")
        catalog_leg = catalog_by_id[leg_id]
        profile = catalog["applicability_profiles"][catalog_leg["applicability_profile"]]
        cell_status: dict[tuple[str, str, str], str] = {}
        for dimension in DIMENSIONS:
            status = leg["default_status"] if profile[dimension] == "required" else "not_applicable"
            for case_id in _semantic_cases(catalog_leg):
                for profile_case in _profile_cases(catalog, catalog_leg):
                    cell_status[(dimension, case_id, profile_case)] = status
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
            cited = _strings(override.get("evidence", []), f"leg {leg_id} override evidence")
            if status in {"pass", "partial", "fail"}:
                _require(bool(cited), f"leg {leg_id} {status} override needs evidence")
            if status in {"partial", "fail", "unknown"}:
                for field in ("reason", "owner_issue", "exit_gate"):
                    _require(field in override, f"leg {leg_id} {status} override is missing {field}")
            if status == "not_applicable":
                _require(profile[key[0]] == "not_applicable", f"leg {leg_id} cannot waive a required dimension")
                _require(not cited, f"leg {leg_id} not_applicable override cannot cite evidence")
            _require(set(cited) <= evidence_ids, f"leg {leg_id} override cites unknown evidence")
            for evidence_id in cited:
                coverage_key = (leg_id, key[0], key[1], key[2])
                _require(coverage_key in coverage_by_evidence[evidence_id], f"leg {leg_id} evidence does not cover its override")
                if status in {"pass", "partial"}:
                    record = evidence_by_id[evidence_id]
                    counts = _dict(record["result_counts"], f"evidence {evidence_id} result_counts")
                    _require(
                        record["exit_code"] == 0 and not counts.get("failed", 0) and not counts.get("errors", 0),
                        f"leg {leg_id} relies on failing evidence",
                    )
            cell_status[key] = status
        values = set(cell_status.values())
        has_unresolved_cell = has_unresolved_cell or bool({"unknown", "partial"} & values)
        if "fail" in values or leg_id in open_affected:
            verdict = "gap"
        elif values <= {"pass", "not_applicable"}:
            verdict = "confirmed"
        else:
            verdict = "unknown"
        _require(leg["derived_verdict"] == verdict, f"leg {leg_id} derived verdict is false")
        derived_by_id[leg_id] = verdict

    hg09 = next(gate for gate in gates if gate["id"] == "HG-09-mandatory-leg-unresolved")
    expected_hg09 = "open" if has_unresolved_cell else "closed"
    _require(hg09["status"] == expected_hg09, "HG-09 does not match unresolved mandatory cells")

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


def _collect_argv(argv: list[Any]) -> list[str]:
    source = iter(_strings(argv, "pytest argv"))
    result: list[str] = []
    for item in source:
        if item == "--junitxml":
            next(source, None)
        elif not item.startswith("--junitxml="):
            result.append(item)
    _require("pytest" in result, "pytest evidence argv does not invoke pytest")
    result.insert(result.index("pytest") + 1, "--collect-only")
    return result


def collect_evidence(assessment_path: Path) -> int:
    root = _git_root(assessment_path)
    assessment = load_unique_json(assessment_path)
    count = 0
    for raw_record in _list(assessment.get("evidence"), "evidence"):
        record = _dict(raw_record, "evidence record")
        if record.get("kind") != "pytest":
            continue
        count += 1
        argv = _collect_argv(_list(record.get("argv"), f"evidence {record.get('id')} argv"))
        cwd = _repository_path(root, record.get("cwd_relative"), "evidence cwd_relative")
        environment = os.environ.copy()
        for name, value in _dict(record.get("safe_environment"), "safe_environment").items():
            if value is None:
                environment.pop(name, None)
            else:
                environment[name] = value
        result = subprocess.run(argv, cwd=cwd, env=environment, check=False, capture_output=True, text=True)
        _require(result.returncode == 0, f"pytest collection failed for {record.get('id')}: {result.stderr}")
        actual = [line for line in result.stdout.splitlines() if "::" in line]
        index = _dict(record.get("collected_node_index"), "collected_node_index")
        node_path = _repository_path(root, index.get("path"), "collected_node_index.path")
        expected = node_path.read_text(encoding="utf-8").splitlines()
        _require(actual == expected, f"pytest collection drift for {record.get('id')}")
    return count


def check_links(root: Path) -> int:
    documents = sorted((root / "docs/architecture/state_engine").glob("**/*.md"))
    documents.append(root / "docs/README.md")
    pattern = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
    checked = 0
    missing: list[str] = []
    for document in documents:
        text = document.read_text(encoding="utf-8")
        for raw_target in pattern.findall(text):
            target = raw_target.split(maxsplit=1)[0].strip("<>")
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            checked += 1
            path_part = target.split("#", 1)[0]
            if path_part and not (document.parent / path_part).resolve().exists():
                missing.append(f"{document.relative_to(root)} -> {target}")
    _require(not missing, "missing documentation links:\n" + "\n".join(missing))
    return checked


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_catalog_parser = subparsers.add_parser("validate-catalog")
    validate_catalog_parser.add_argument("catalog", type=Path)
    init_parser = subparsers.add_parser("init-full")
    init_parser.add_argument("assessment_id")
    init_parser.add_argument("output_directory", type=Path)
    validate_package_parser = subparsers.add_parser("validate-package")
    validate_package_parser.add_argument("assessment", type=Path)
    collect_parser = subparsers.add_parser("collect-evidence")
    collect_parser.add_argument("assessment", type=Path)
    subparsers.add_parser("check-links")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "validate-catalog":
            catalog_path = arguments.catalog.resolve()
            validate_catalog(load_unique_json(catalog_path), catalog_path)
            print(f"state-engine catalog: valid ({catalog_path})")
        elif arguments.command == "init-full":
            output = initialize_full(arguments.assessment_id, arguments.output_directory)
            print(f"state-engine assessment initialized: {output}")
        elif arguments.command == "validate-package":
            leg_count, verdict = validate_package(arguments.assessment)
            print(f"state-engine assessment contract: valid ({leg_count} legs, {verdict})")
        elif arguments.command == "collect-evidence":
            count = collect_evidence(arguments.assessment)
            print(f"evidence collection: valid ({count} pytest records)")
        elif arguments.command == "check-links":
            count = check_links(_git_root(Path.cwd()))
            print(f"state-engine documentation links: valid ({count} links)")
        else:
            _fail(f"unknown command: {arguments.command}")
    except (ContractError, ValueError, KeyError, OSError, ET.ParseError) as error:
        print(f"state-engine assessment: invalid: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
