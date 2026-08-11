"""Shared constants, types, parsing, hashing, Git, and path helpers."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
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
APPLICABILITY_PROFILE_IDS = {
    "elspeth-state-engine-v1": {"all-required-v1"},
    "elspeth-state-engine-v2": {
        "all-required-v2",
        "sqlite-followers-only-v2",
        "postgresql-aws-only-v2",
    },
}
PLACEHOLDER_PATTERN = re.compile(r"TBD|TODO|FIXME|<timestamp>|<full SHA>")
CANONICAL_V2_LEGS_SHA256 = "ab579d510d9ed1e0becec9b290de2d598725b25e41d43ac4fe0427be6bf8ff01"
CANONICAL_V2_ACCEPTANCE_SHA256 = "f56bd4d8bb68b1bc4bc95f718a426bdc5e22d71d12198307337d41c905e29e93"
CANONICAL_V2_HARD_GATES_SHA256 = "4fe26992d0000a02e5a102849e85dcd15ac11fb05a0229362bd2117d74b8af87"
CANONICAL_V2_APPLICABILITY_SHA256 = "e9d4565ecf7eb489f90d4472b1d93315fd9d50161b4a4f922e1fb4fd1c010596"
CANONICAL_V2_EVIDENCE_CONTRACT_SHA256 = "e95c84c9067a7bdd9eb29af6c5acc20e4cf67e924518628dd400196aa132fb35"
CANONICAL_V2_EXECUTION_PROFILES_SHA256 = "b4d34163391b1007082aa2f5ddf28d3b3ff295c5f010d2c84e0a93afe767a5fb"
CANONICAL_V2_TOP_LEVEL_KEYS = {
    "schema_version",
    "catalog_id",
    "criteria_ref",
    "architecture_ref",
    "dimensions",
    "vocabularies",
    "execution_profiles",
    "applicability_profiles",
    "family_dimension_acceptance",
    "evidence_contract",
    "hard_gates",
    "required_leg_ids",
    "legs",
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


def _semantic_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


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
        + [f"PB-{number:02d}" for number in range(1, 12 if v2 else 10)]
        + [f"RM-{number:02d}" for number in range(1, 15 if v2 else 14)]
        + [f"F-{number:02d}" for number in range(1, 15 if v2 else 14)]
    )
