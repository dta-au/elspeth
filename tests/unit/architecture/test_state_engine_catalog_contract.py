from __future__ import annotations

import hashlib
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from elspeth.contracts.enums import TerminalPath
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.plugins.infrastructure.discovery import discover_all_plugins

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
V1_CATALOG_PATH = REPOSITORY_ROOT / "docs/architecture/state_engine/proof-catalog/v1/catalog.json"
V2_CATALOG_PATH = REPOSITORY_ROOT / "docs/architecture/state_engine/proof-catalog/v2/catalog.json"
ASSESSMENT_SCRIPT_PATH = REPOSITORY_ROOT / "scripts/state_engine_assessment.py"
V1_CATALOG_SHA256 = "2e025df8fcb61869f4ac2575d2d1b0c5bba5aa63c88c0d059e630431062eef2e"


def _load_catalog(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_v2_catalog_uses_owned_state_and_path_vocabularies() -> None:
    catalog = _load_catalog(V2_CATALOG_PATH)

    assert catalog["catalog_id"] == "elspeth-state-engine-v2"
    assert catalog["vocabularies"] == {
        "token_work_status": [item.value for item in TokenWorkStatus],
        "terminal_path": [item.value for item in TerminalPath],
    }


def test_v2_catalog_first_party_plugin_inventory_matches_live_discovery() -> None:
    catalog = _load_catalog(V2_CATALOG_PATH)
    discovered = discover_all_plugins()
    execution_profiles = catalog["execution_profiles"]
    assert isinstance(execution_profiles, dict)
    inventory = execution_profiles["first_party_plugins"]
    assert isinstance(inventory, dict)

    assert set(inventory) == {*discovered, "inventory_rule"}
    for kind, plugin_classes in discovered.items():
        expected_names = sorted(cast(Any, plugin).name for plugin in plugin_classes)
        assert len(expected_names) == len(set(expected_names))
        assert inventory[kind] == expected_names


def test_v2_catalog_adds_abandonment_and_row_union_obligations() -> None:
    catalog = _load_catalog(V2_CATALOG_PATH)
    required_leg_ids = catalog["required_leg_ids"]
    legs = catalog["legs"]
    assert isinstance(required_leg_ids, list)
    assert isinstance(legs, list)
    leg_by_id = {leg["id"]: leg for leg in legs}

    assert {"TS-19", "PB-10", "RM-14", "F-14"} <= set(required_leg_ids)
    assert list(leg_by_id) == required_leg_ids
    assert leg_by_id["PB-10"]["required_cases"] == [
        "all-branches-arrive",
        "branch-loss-before-first-arrival",
        "branch-loss-after-partial-arrival",
        "timeout",
        "late-arrival-after-release",
        "restart-before-release",
        "restart-after-release-before-sink",
    ]


def test_v2_catalog_profile_cases_bind_backend_deployment_and_lifecycle() -> None:
    catalog = _load_catalog(V2_CATALOG_PATH)
    execution_profiles = catalog["execution_profiles"]
    applicability_profiles = catalog["applicability_profiles"]
    legs = catalog["legs"]
    assert isinstance(execution_profiles, dict)
    assert isinstance(applicability_profiles, dict)
    assert isinstance(legs, list)

    state_stores = {profile["id"]: profile for profile in execution_profiles["state_store"]}
    profile_cases = execution_profiles["profile_cases"]
    case_ids = [case["id"] for case in profile_cases]
    assert len(case_ids) == len(set(case_ids))
    assert set(case_ids) == {
        "sqlite-wal-single-process-leader",
        "sqlite-wal-same-host-leader-plus-claim-only-followers",
        "sqlite-wal-web-hosted-leader-plus-same-host-cli-followers",
        "postgresql-16-aws-single-leader-landscape",
    }
    for case in profile_cases:
        state_store = case["state_store"]
        deployment = case["deployment"]
        assert state_store in state_stores
        assert deployment in state_stores[state_store]["deployments"]
        assert set(case["lifecycle_modes"]) <= set(execution_profiles["lifecycle_modes"])

    assert set(applicability_profiles) == {"all-required-v2"}
    assert applicability_profiles["all-required-v2"]["profile_case_ids"] == case_ids
    assert {leg["applicability_profile"] for leg in legs} == {"all-required-v2"}


def test_v1_catalog_remains_byte_identical_historical_evidence() -> None:
    assert hashlib.sha256(V1_CATALOG_PATH.read_bytes()).hexdigest() == V1_CATALOG_SHA256


def _run_assessment_cli(
    *arguments: str,
    cwd: Path = REPOSITORY_ROOT,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "src")
    return subprocess.run(
        [sys.executable, str(ASSESSMENT_SCRIPT_PATH), *arguments],
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _assessment_namespace() -> dict[str, Any]:
    assert ASSESSMENT_SCRIPT_PATH.is_file(), "state-engine assessment script must exist"
    return runpy.run_path(str(ASSESSMENT_SCRIPT_PATH), run_name="state_engine_assessment_test")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _add_unsupported_postgresql_profile(catalog: dict[str, Any]) -> None:
    execution_profiles = catalog["execution_profiles"]
    postgresql = next(profile for profile in execution_profiles["state_store"] if profile["id"] == "postgresql-16")
    deployment = "multi-host-replicas"
    postgresql["deployments"].append(deployment)
    execution_profiles["deployments"].append(deployment)
    execution_profiles["profile_cases"].append(
        {
            "id": "postgresql-16-multi-host-replicas",
            "state_store": "postgresql-16",
            "deployment": deployment,
            "lifecycle_modes": ["fresh", "resume", "leader-takeover"],
        }
    )
    catalog["applicability_profiles"]["all-required-v2"]["profile_case_ids"].append("postgresql-16-multi-host-replicas")


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _write_unknown_proof_matrix(path: Path, catalog: dict[str, Any]) -> None:
    labels = {
        "transition": "Token transitions",
        "auxiliary": "Auxiliary state",
        "run_coordination": "Run coordination",
        "production_boundary": "Production boundaries",
        "read_model": "Read models",
        "forbidden": "Forbidden paths",
    }
    counts = {family: sum(leg["family"] == family for leg in catalog["legs"]) for family in labels}
    rows = [
        "# State Engine Proof Matrix",
        "",
        "| Family | Legs | Confirmed | Gap | Unknown | Main unresolved proof |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    rows.extend(f"| {label} | {counts[family]} | 0 | 0 | {counts[family]} | Fixture |" for family, label in labels.items())
    total = len(catalog["legs"])
    rows.append(f"| **Total** | **{total}** | **0** | **0** | **{total}** | Fixture |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


@pytest.fixture
def assessment_repository(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    catalog_path = repository / "docs/architecture/state_engine/proof-catalog/v2/catalog.json"
    catalog = json.loads(V2_CATALOG_PATH.read_text(encoding="utf-8"))
    _write_json(catalog_path, catalog)
    _write_unknown_proof_matrix(
        repository / "docs/architecture/state_engine/proof-matrix.md",
        catalog,
    )
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "State Engine Test")
    _git(repository, "config", "user.email", "state-engine-test@example.invalid")
    _git(repository, "add", "docs")
    _git(repository, "commit", "-qm", "fixture baseline")

    assessment_directory = repository / "docs/architecture/state_engine/assessments/minimal-assessment"
    result = _run_assessment_cli(
        "init-full",
        "minimal-assessment",
        str(assessment_directory),
        cwd=repository,
    )
    assert result.returncode == 0, result.stderr
    assessment_path = assessment_directory / "assessment.json"
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    commit = _git(repository, "rev-parse", "HEAD")
    tree = _git(repository, "rev-parse", "HEAD^{tree}")
    assessment["baseline"] = {
        "repository_root": str(repository.resolve()),
        "remote": "fixture://state-engine",
        "branch": _git(repository, "branch", "--show-current"),
        "commit": commit,
        "tree": tree,
        "behavioral_overlay": None,
        "worktree_status_at_evidence_capture": [],
        "submodules": [],
        "worktrees_at_capture": [],
    }
    assessment["environment"] = {
        "captured_at": "2026-08-11T00:00:00+10:00",
        "timezone": "Australia/Canberra",
        "locale": "C.UTF-8",
        "kernel": "fixture",
        "python": "3.13",
        "python_executable": sys.executable,
        "python_build": "fixture",
        "pytest": pytest.__version__,
        "uv": "fixture",
        "git": "fixture",
        "sqlite": "fixture",
        "sqlalchemy": "fixture",
        "multiprocessing_start_method_before_tests": None,
        "pythonhashseed": "unset",
        "dotenv": "absent",
        "pyproject_sha256": "0" * 64,
        "uv_lock_sha256": "1" * 64,
        "database_profile": "fixture",
        "sensitive_environment_captured": False,
    }
    assessment["structure_snapshot"] = {
        "provider": "fixture",
        "captured_at": "2026-08-11T00:00:00+10:00",
        "limitation": "fixture",
    }
    assessment["tracker_snapshot"] = {
        "provider": "fixture",
        "captured_at": "2026-08-11T00:00:00+10:00",
        "limitation": "fixture",
    }
    hg09 = next(gate for gate in assessment["hard_gates"] if gate["id"] == "HG-09-mandatory-leg-unresolved")
    hg09["status"] = "open"
    hg09["reason"] = "Every mandatory cell is unknown in the minimal package."
    assessment["derived"]["overall_verdict"] = "not_complete"
    assessment["derived"]["reason"] = "The mandatory-leg gate is open."
    _write_json(assessment_path, assessment)
    (assessment_directory / "review.md").write_text(
        "# Assessment Review\n\nReview outcome: complete\n",
        encoding="utf-8",
    )
    return repository, assessment_path


def test_load_unique_json_rejects_duplicate_keys(tmp_path: Path) -> None:
    duplicate_path = tmp_path / "duplicate.json"
    duplicate_path.write_text('{"catalog_id": "one", "catalog_id": "two"}\n', encoding="utf-8")
    load_unique_json = _assessment_namespace()["load_unique_json"]

    with pytest.raises(ValueError, match=r"duplicate JSON key in .*duplicate\.json: catalog_id"):
        load_unique_json(duplicate_path)


def test_validate_catalog_cli_accepts_current_v2_catalog() -> None:
    result = _run_assessment_cli("validate-catalog", str(V2_CATALOG_PATH))

    assert result.returncode == 0, result.stderr
    assert "state-engine catalog: valid" in result.stdout


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        pytest.param(
            lambda catalog: catalog["required_leg_ids"].pop(),
            "required_leg_ids",
            id="missing-leg",
        ),
        pytest.param(
            lambda catalog: catalog["execution_profiles"]["first_party_plugins"]["sources"].append("stale_source"),
            "first-party plugin inventory",
            id="stale-plugin-inventory",
        ),
        pytest.param(
            lambda catalog: catalog["execution_profiles"]["profile_cases"][0].update({"state_store": "missing-backend"}),
            "profile case",
            id="invalid-profile-reference",
        ),
        pytest.param(
            _add_unsupported_postgresql_profile,
            "supported state-store profiles",
            id="unsupported-postgresql-multi-host-profile",
        ),
    ],
)
def test_validate_catalog_cli_rejects_contract_drift(
    tmp_path: Path,
    mutation: Any,
    expected_error: str,
) -> None:
    catalog = json.loads(V2_CATALOG_PATH.read_text(encoding="utf-8"))
    mutation(catalog)
    catalog_path = tmp_path / "catalog.json"
    _write_json(catalog_path, catalog)

    result = _run_assessment_cli("validate-catalog", str(catalog_path))

    assert result.returncode == 1
    assert expected_error in result.stderr


def test_validate_catalog_cli_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text('{"catalog_id": "one", "catalog_id": "two"}\n', encoding="utf-8")

    result = _run_assessment_cli("validate-catalog", str(catalog_path))

    assert result.returncode == 1
    assert "duplicate JSON key" in result.stderr


def test_validate_package_accepts_complete_minimal_manifest(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 0, result.stderr
    assert "state-engine assessment contract: valid" in result.stdout
    assert "72 legs" in result.stdout
    assert "not_complete" in result.stdout


def test_validate_package_rejects_dangling_evidence(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    assessment["legs"][0]["overrides"] = [
        {
            "dimension": "production_entry",
            "case": "leg-contract",
            "profile_case": "sqlite-wal-single-process-leader",
            "status": "pass",
            "evidence": ["EV-MISSING"],
        }
    ]
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert "unknown evidence" in result.stderr


def test_validate_package_rejects_false_derived_totals_and_verdict(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    assessment["derived"]["total"] = {"confirmed": 72, "gap": 0, "unknown": 0}
    assessment["derived"]["overall_verdict"] = "complete"
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert "derived total" in result.stderr


def test_collect_evidence_cli_validates_empty_pytest_inventory(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository

    result = _run_assessment_cli("collect-evidence", str(assessment_path), cwd=repository)

    assert result.returncode == 0, result.stderr
    assert "evidence collection: valid (0 pytest records)" in result.stdout
