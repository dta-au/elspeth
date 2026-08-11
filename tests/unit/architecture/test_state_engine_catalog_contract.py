from __future__ import annotations

import hashlib
import json
import os
import re
import runpy
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
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
PROFILE_REPORTER_PLUGIN = "scripts.state_engine_profile_reporter"
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

    assert {"TS-19", "PB-10", "PB-11", "RM-14", "F-14"} <= set(required_leg_ids)
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
    assert leg_by_id["PB-11"]["required_cases"] == [
        "db-server-time",
        "row-lock-order",
        "transaction-isolation",
        "schema-admission-migration",
        "ambiguous-connection-loss",
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

    all_required = dict.fromkeys(case_ids, "required")
    followers_only = {
        case_id: (
            "required"
            if case_id
            in {
                "sqlite-wal-same-host-leader-plus-claim-only-followers",
                "sqlite-wal-web-hosted-leader-plus-same-host-cli-followers",
            }
            else "not_applicable"
        )
        for case_id in case_ids
    }
    postgresql_only = {
        case_id: ("required" if case_id == "postgresql-16-aws-single-leader-landscape" else "not_applicable") for case_id in case_ids
    }
    assert set(applicability_profiles) == {
        "all-required-v2",
        "sqlite-followers-only-v2",
        "postgresql-aws-only-v2",
    }
    assert applicability_profiles["all-required-v2"]["profile_case_applicability"] == all_required
    assert applicability_profiles["sqlite-followers-only-v2"]["profile_case_applicability"] == followers_only
    assert applicability_profiles["postgresql-aws-only-v2"]["profile_case_applicability"] == postgresql_only
    leg_by_id = {leg["id"]: leg for leg in legs}
    assert leg_by_id["RC-05"]["applicability_profile"] == "sqlite-followers-only-v2"
    assert leg_by_id["PB-08"]["applicability_profile"] == "sqlite-followers-only-v2"
    assert leg_by_id["PB-11"]["applicability_profile"] == "postgresql-aws-only-v2"
    assert {leg["applicability_profile"] for leg in legs if leg["id"] not in {"RC-05", "PB-08", "PB-11"}} == {"all-required-v2"}


def test_v2_catalog_plugin_cases_exhaust_live_inventory() -> None:
    catalog = _load_catalog(V2_CATALOG_PATH)
    legs = catalog["legs"]
    assert isinstance(legs, list)
    pb09 = next(leg for leg in legs if leg["id"] == "PB-09")
    discovered = discover_all_plugins()
    expected = [
        f"{kind.removesuffix('s')}:{name}"
        for kind in ("sources", "transforms", "sinks")
        for name in sorted(cast(Any, plugin).name for plugin in discovered[kind])
    ]

    assert pb09["required_cases"] == expected
    assert len(expected) == 51


def test_v1_catalog_remains_byte_identical_historical_evidence() -> None:
    assert hashlib.sha256(V1_CATALOG_PATH.read_bytes()).hexdigest() == V1_CATALOG_SHA256


def _run_assessment_cli(
    *arguments: str,
    cwd: Path = REPOSITORY_ROOT,
    environment_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "src")
    environment.update(environment_overrides or {})
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
    for profile in catalog["applicability_profiles"].values():
        if "profile_case_applicability" in profile:
            profile["profile_case_applicability"]["postgresql-16-multi-host-replicas"] = "required"
        else:
            profile["profile_case_ids"].append("postgresql-16-multi-host-replicas")


def _catalog_leg(catalog: dict[str, Any], leg_id: str) -> dict[str, Any]:
    return next(leg for leg in catalog["legs"] if leg["id"] == leg_id)


def _remove_required_case(catalog: dict[str, Any], leg_id: str) -> None:
    _catalog_leg(catalog, leg_id)["required_cases"].pop()


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


def _write_complete_proof_matrix(path: Path, catalog: dict[str, Any]) -> None:
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
    rows.extend(f"| {label} | {counts[family]} | {counts[family]} | 0 | 0 | None |" for family, label in labels.items())
    total = len(catalog["legs"])
    rows.append(f"| **Total** | **{total}** | **{total}** | **0** | **0** | None |")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _artifact_record(repository: Path, path: str, content: str) -> dict[str, str]:
    artifact_path = repository / path
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(content, encoding="utf-8")
    return {"path": path, "sha256": hashlib.sha256(content.encode()).hexdigest()}


def _existing_artifact_record(repository: Path, path: str, kind: str) -> dict[str, str]:
    artifact_path = repository / path
    return {"kind": kind, "path": path, "sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest()}


def _genuine_sqlite_evidence(repository: Path) -> dict[str, Any]:
    test_relative = "tests/test_state_engine_runtime_profile.py"
    test_path = repository / test_relative
    test_path.parent.mkdir(exist_ok=True)
    test_path.write_text(
        "import sqlite3\n\n"
        "def test_runtime_profile(state_engine_profile):\n"
        "    connection = sqlite3.connect(':memory:')\n"
        "    assert connection.execute('SELECT 1').fetchone() == (1,)\n"
        "    state_engine_profile.observe_sqlite(\n"
        "        connection, deployment='single-process-leader'\n"
        "    )\n",
        encoding="utf-8",
    )
    _git(repository, "add", test_relative)
    _git(repository, "commit", "-qm", "add genuine profile evidence node")
    stem = "docs/architecture/state_engine/assessments/minimal-assessment/evidence/ev-genuine"
    junit_relative = f"{stem}.junit.xml"
    profile_relative = f"{stem}.profile.json"
    stdout_relative = f"{stem}.stdout"
    stderr_relative = f"{stem}.stderr"
    node_index_relative = f"{stem}.nodes"
    selector = f"{test_relative}::test_runtime_profile"
    argv = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        PROFILE_REPORTER_PLUGIN,
        f"--state-engine-profile-report={profile_relative}",
        f"--junitxml={junit_relative}",
        selector,
    ]
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": str(REPOSITORY_ROOT),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    started_at = datetime.now(UTC)
    result = subprocess.run(
        argv,
        cwd=repository,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    ended_at = datetime.now(UTC)
    assert result.returncode == 0, result.stderr
    (repository / stdout_relative).write_text(result.stdout, encoding="utf-8")
    (repository / stderr_relative).write_text(result.stderr, encoding="utf-8")
    (repository / node_index_relative).write_text(f"{selector}\n", encoding="utf-8")
    return {
        "id": "EV-GENUINE",
        "kind": "pytest",
        "reproducibility_class": "deterministic",
        "argv": argv,
        "cwd_relative": ".",
        "timeout_seconds": 30,
        "safe_environment": {"PYTHONHASHSEED": "0"},
        "resources": [],
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "duration_seconds": (ended_at - started_at).total_seconds(),
        "exit_code": 0,
        "result_counts": {
            "passed": 1,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "xfailed": 0,
            "xpassed": 0,
            "warnings": 0,
        },
        "coverage": [],
        "retained_artifacts": [
            _existing_artifact_record(repository, junit_relative, "junit_xml"),
            _existing_artifact_record(repository, stdout_relative, "stdout"),
            _existing_artifact_record(repository, stderr_relative, "stderr"),
            _existing_artifact_record(repository, profile_relative, "profile_report"),
        ],
        "collected_node_index": _existing_artifact_record(repository, node_index_relative, "node_index"),
        "collected_nodes": 1,
        "establishes": ["The retained artifacts exercise the profile reporter contract."],
        "does_not_establish": ["This fixture does not establish a state-engine proof cell."],
    }


def _artifact_by_kind(record: dict[str, Any], kind: str) -> dict[str, str]:
    return next(artifact for artifact in record["retained_artifacts"] if artifact["kind"] == kind)


def _replace_genuine_node_identity(repository: Path, record: dict[str, Any], node_id: str) -> None:
    junit_record = _artifact_by_kind(record, "junit_xml")
    junit_path = repository / junit_record["path"]
    junit = ET.parse(junit_path)
    identity = next(element for element in junit.getroot().iter("property") if element.attrib.get("name") == "elspeth_node_id")
    identity.set("value", node_id)
    junit.write(junit_path, encoding="utf-8", xml_declaration=True)
    junit_record["sha256"] = hashlib.sha256(junit_path.read_bytes()).hexdigest()
    profile_record = _artifact_by_kind(record, "profile_report")
    profile_path = repository / profile_record["path"]
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["node_ids"] = [node_id]
    profile["probe_node_id"] = node_id
    profile["deployment_probe"]["node_id"] = node_id
    profile["outcomes"][0]["node_id"] = node_id
    for warning in profile["warnings"]:
        if warning["node_id"] is not None:
            warning["node_id"] = node_id
    _write_json(profile_path, profile)
    profile_record["sha256"] = hashlib.sha256(profile_path.read_bytes()).hexdigest()
    node_record = record["collected_node_index"]
    node_path = repository / node_record["path"]
    node_path.write_text(f"{node_id}\n", encoding="utf-8")
    node_record["sha256"] = hashlib.sha256(node_path.read_bytes()).hexdigest()


def _profile_provenance(
    profile_case: str,
    nodes: list[str],
    *,
    outcomes: list[str] | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if profile_case == "postgresql-16-aws-single-leader-landscape":
        state_store = "postgresql-16"
        deployment = "aws-single-leader-landscape"
        backend_version = "16.13"
        backend_probe = {
            "kind": "postgresql-server-query",
            "dialect": "postgresql",
            "query": "SHOW server_version",
        }
    else:
        state_store = "sqlite-wal"
        deployment = profile_case.removeprefix("sqlite-wal-")
        backend_version = "3.47.1"
        backend_probe = {
            "kind": "sqlite-connection-query",
            "dialect": "sqlite",
            "query": "SELECT sqlite_version()",
        }
    probe_node_id = nodes[0] if nodes else "tests/unit/test_state_engine_contract.py::missing_probe"
    return {
        "schema_version": 2,
        "producer": "elspeth-state-engine-profile-reporter-v2",
        "profile_case_id": profile_case,
        "state_store": state_store,
        "deployment": deployment,
        "backend_version": backend_version,
        "backend_probe": backend_probe,
        "deployment_probe": {"kind": "trusted-test-runtime-assertion", "node_id": probe_node_id},
        "probe_node_id": probe_node_id,
        "node_ids": nodes,
        "outcomes": [
            {"node_id": node_id, "outcome": outcome} for node_id, outcome in zip(nodes, outcomes or ["passed"] * len(nodes), strict=True)
        ],
        "warnings": warnings or [],
    }


def _pytest_evidence(
    repository: Path,
    *,
    evidence_id: str,
    profile_case: str,
    coverage: list[dict[str, Any]],
    nodes: list[str],
    counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    result_counts = counts or {
        "passed": len(nodes),
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
        "warnings": 0,
    }
    outcome_values = [
        *(["failed"] * result_counts["failed"]),
        *(["error"] * result_counts["errors"]),
        *(["skipped"] * result_counts["skipped"]),
        *(["xfailed"] * result_counts["xfailed"]),
        *(["xpassed"] * result_counts["xpassed"]),
        *(["passed"] * result_counts["passed"]),
    ]
    warning_provenance = [
        {
            "node_id": nodes[index % len(nodes)] if nodes else None,
            "when": "runtest",
            "category": "builtins.UserWarning",
            "message": f"synthetic warning {index + 1}",
            "filename": "tests/unit/test_state_engine_contract.py",
            "line": 1,
        }
        for index in range(result_counts["warnings"])
    ]
    stem = f"docs/architecture/state_engine/assessments/minimal-assessment/evidence/{evidence_id.lower()}"
    junit_tests = sum(result_counts[key] for key in ("passed", "failed", "errors", "skipped", "xfailed", "xpassed"))
    testcase_fragments: list[str] = []
    for index, node in enumerate(nodes):
        if index < result_counts["failed"]:
            outcome = "<failure />"
        elif index < result_counts["failed"] + result_counts["errors"]:
            outcome = "<error />"
        elif index < result_counts["failed"] + result_counts["errors"] + result_counts["skipped"] + result_counts["xfailed"]:
            outcome = "<skipped />"
        else:
            outcome = ""
        testcase_fragments.append(
            f'<testcase classname="fixture" name="node-{index}"><properties>'
            f'<property name="elspeth_node_id" value="{node}" /></properties>{outcome}</testcase>'
        )
    junit_relative = f"{stem}.junit.xml"
    profile_relative = f"{stem}.profile.json"
    stdout_relative = f"{stem}.stdout"
    stderr_relative = f"{stem}.stderr"
    node_relative = f"{stem}.nodes"

    def typed_artifact(path: str, content: str, kind: str) -> dict[str, str]:
        artifact = _artifact_record(repository, path, content)
        return {"kind": kind, **artifact}

    artifacts = [
        typed_artifact(
            junit_relative,
            (
                f'<testsuite tests="{junit_tests}" failures="{result_counts["failed"]}" '
                f'errors="{result_counts["errors"]}" '
                f'skipped="{result_counts["skipped"] + result_counts["xfailed"]}">'
                f"{''.join(testcase_fragments)}</testsuite>\n"
            ),
            "junit_xml",
        ),
        typed_artifact(stdout_relative, "pytest fixture output\n", "stdout"),
        typed_artifact(stderr_relative, "", "stderr"),
        typed_artifact(
            profile_relative,
            json.dumps(
                _profile_provenance(
                    profile_case,
                    nodes,
                    outcomes=outcome_values,
                    warnings=warning_provenance,
                ),
                indent=2,
            )
            + "\n",
            "profile_report",
        ),
    ]
    node_index_content = "".join(f"{node}\n" for node in nodes)
    node_index = typed_artifact(node_relative, node_index_content, "node_index")
    selector_path = repository / "tests/unit/test_state_engine_contract.py"
    selector_path.parent.mkdir(parents=True, exist_ok=True)
    if not selector_path.exists():
        selector_path.write_text("# Synthetic validator fixture selectors.\n", encoding="utf-8")
    return {
        "id": evidence_id,
        "kind": "pytest",
        "reproducibility_class": "deterministic",
        "argv": [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            PROFILE_REPORTER_PLUGIN,
            f"--state-engine-profile-report={profile_relative}",
            f"--junitxml={junit_relative}",
            "tests/unit/test_state_engine_contract.py",
        ],
        "cwd_relative": ".",
        "timeout_seconds": 30,
        "safe_environment": {"PYTHONHASHSEED": "0"},
        "resources": [],
        "started_at": "2026-08-11T00:00:00+10:00",
        "ended_at": "2026-08-11T00:00:01+10:00",
        "duration_seconds": 1.0,
        "exit_code": 0,
        "result_counts": result_counts,
        "coverage": coverage,
        "retained_artifacts": artifacts,
        "collected_node_index": node_index,
        "collected_nodes": len(nodes),
        "establishes": ["Executable state-engine behavior for the declared proof cells."],
        "does_not_establish": [],
    }


def _required_cells(catalog: dict[str, Any]) -> list[tuple[dict[str, Any], str, str, str]]:
    cells: list[tuple[dict[str, Any], str, str, str]] = []
    for leg in catalog["legs"]:
        applicability = catalog["applicability_profiles"][leg["applicability_profile"]]
        cases = leg.get("required_cases", [applicability["default_case_id"]])
        for profile_case, policy in applicability["profile_case_applicability"].items():
            if policy != "required":
                continue
            for case_id in cases:
                for dimension in catalog["dimensions"]:
                    if applicability[dimension] == "required":
                        cells.append((leg, dimension, case_id, profile_case))
    return cells


def _materialize_complete_assessment(
    repository: Path,
    assessment_path: Path,
    *,
    one_node_per_profile: bool,
) -> None:
    catalog = json.loads((repository / "docs/architecture/state_engine/proof-catalog/v2/catalog.json").read_text())
    assessment = json.loads(assessment_path.read_text())
    cells = _required_cells(catalog)
    evidence_by_profile: dict[str, str] = {}
    records: list[dict[str, Any]] = []
    for profile_index, profile_case in enumerate(
        [profile["id"] for profile in catalog["execution_profiles"]["profile_cases"]],
        start=1,
    ):
        profile_cells = [cell for cell in cells if cell[3] == profile_case]
        subjects = list(dict.fromkeys((cell[0]["id"], cell[2], profile_case) for cell in profile_cells))
        if one_node_per_profile:
            nodes = [f"tests/unit/test_state_engine_contract.py::test_profile_{profile_index}"]
            node_by_subject = dict.fromkeys(subjects, nodes[0])
        else:
            node_by_subject = {
                subject: (
                    "tests/unit/test_state_engine_contract.py::test_"
                    + re.sub(r"[^a-z0-9]+", "_", f"{subject[0]}_{subject[1]}_{profile_index}".lower()).strip("_")
                )
                for subject in subjects
            }
            nodes = list(node_by_subject.values())
        coverage = [
            {
                "leg_id": leg["id"],
                "dimension_id": dimension,
                "case_id": case_id,
                "profile_case": profile_case,
                "node_ids": [node_by_subject[(leg["id"], case_id, profile_case)]],
            }
            for leg, dimension, case_id, profile_case in profile_cells
        ]
        evidence_id = f"EV-PROFILE-{profile_index:02d}"
        evidence_by_profile[profile_case] = evidence_id
        records.append(
            _pytest_evidence(
                repository,
                evidence_id=evidence_id,
                profile_case=profile_case,
                coverage=coverage,
                nodes=nodes,
            )
        )
    assessment["evidence"] = records
    cells_by_leg: dict[str, list[tuple[dict[str, Any], str, str, str]]] = {}
    for cell in cells:
        cells_by_leg.setdefault(cell[0]["id"], []).append(cell)
    for leg in assessment["legs"]:
        leg["derived_verdict"] = "confirmed"
        leg["reason"] = "Every required cell has executable passing evidence."
        leg["owner_issue"] = None
        leg["exit_gate"] = "Retain the passing executable selectors."
        leg["overrides"] = [
            {
                "dimension": dimension,
                "case": case_id,
                "profile_case": profile_case,
                "status": "pass",
                "evidence": [evidence_by_profile[profile_case]],
            }
            for _catalog_leg, dimension, case_id, profile_case in cells_by_leg[leg["id"]]
        ]
    evidence_ids = list(evidence_by_profile.values())
    for gate in assessment["hard_gates"]:
        gate["status"] = "closed"
        gate["support"] = evidence_ids
        gate["affected_leg_ids"] = []
        gate["reason"] = "All mapped mandatory proof cells have executable passing evidence."
    family_counts = {
        family: {
            "confirmed": sum(leg["family"] == family for leg in catalog["legs"]),
            "gap": 0,
            "unknown": 0,
        }
        for family in (
            "transition",
            "auxiliary",
            "run_coordination",
            "production_boundary",
            "read_model",
            "forbidden",
        )
    }
    assessment["derived"] = {
        "family_counts": family_counts,
        "total": {"confirmed": 73, "gap": 0, "unknown": 0},
        "overall_verdict": "complete",
        "reason": "All mandatory proof cells and hard gates are resolved.",
    }
    _write_json(assessment_path, assessment)
    _write_complete_proof_matrix(repository / "docs/architecture/state_engine/proof-matrix.md", catalog)


def _attach_ts00_pass(
    assessment: dict[str, Any],
    record: dict[str, Any],
    *,
    profile_case: str,
) -> None:
    assessment["evidence"] = [record]
    ts00 = next(leg for leg in assessment["legs"] if leg["id"] == "TS-00")
    ts00["overrides"] = [
        {
            "dimension": "production_entry",
            "case": "leg-contract",
            "profile_case": profile_case,
            "status": "pass",
            "evidence": [record["id"]],
        }
    ]


def _ts00_coverage(profile_case: str, node_ids: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "leg_id": "TS-00",
            "dimension_id": "production_entry",
            "case_id": "leg-contract",
            "profile_case": profile_case,
            "node_ids": node_ids,
        }
    ]


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
    (repository / "docs/README.md").write_text("# Documentation\n")
    synthetic_selector = repository / "tests/unit/test_state_engine_contract.py"
    synthetic_selector.parent.mkdir(parents=True)
    synthetic_selector.write_text("# Synthetic validator fixture selectors.\n", encoding="utf-8")
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "State Engine Test")
    _git(repository, "config", "user.email", "state-engine-test@example.invalid")
    _git(repository, "remote", "add", "origin", "fixture://state-engine")
    _git(repository, "add", "docs", "tests")
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
        "mode": "current",
        "repository_root": str(repository.resolve()),
        "remote": "fixture://state-engine",
        "branch": _git(repository, "branch", "--show-current"),
        "commit": commit,
        "tree": tree,
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


def test_profile_reporter_emits_sqlite_runtime_observation_and_exact_junit_node(
    tmp_path: Path,
) -> None:
    test_path = tmp_path / "tests/test_runtime_profile.py"
    test_path.parent.mkdir()
    test_path.write_text(
        "import sqlite3\n\n"
        "def test_runtime_profile(state_engine_profile):\n"
        "    connection = sqlite3.connect(':memory:')\n"
        "    assert connection.execute('SELECT 1').fetchone() == (1,)\n"
        "    state_engine_profile.observe_sqlite(\n"
        "        connection, deployment='single-process-leader'\n"
        "    )\n",
        encoding="utf-8",
    )
    profile_path = tmp_path / "profile.json"
    junit_path = tmp_path / "result.junit.xml"
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": str(REPOSITORY_ROOT),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            PROFILE_REPORTER_PLUGIN,
            f"--state-engine-profile-report={profile_path}",
            f"--junitxml={junit_path}",
            "tests/test_runtime_profile.py::test_runtime_profile",
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    node_id = "tests/test_runtime_profile.py::test_runtime_profile"
    assert profile == {
        "schema_version": 2,
        "producer": "elspeth-state-engine-profile-reporter-v2",
        "profile_case_id": "sqlite-wal-single-process-leader",
        "state_store": "sqlite-wal",
        "deployment": "single-process-leader",
        "backend_version": profile["backend_version"],
        "backend_probe": {
            "kind": "sqlite-connection-query",
            "dialect": "sqlite",
            "query": "SELECT sqlite_version()",
        },
        "deployment_probe": {
            "kind": "trusted-test-runtime-assertion",
            "node_id": node_id,
        },
        "probe_node_id": node_id,
        "node_ids": [node_id],
        "outcomes": [{"node_id": node_id, "outcome": "passed"}],
        "warnings": [],
    }
    assert re.fullmatch(r"3\.\d+(?:\.\d+)?", profile["backend_version"])
    junit = junit_path.read_text(encoding="utf-8")
    assert f'name="elspeth_node_id" value="{node_id}"' in junit


def test_profile_reporter_emits_exact_outcomes_and_warning_provenance(
    tmp_path: Path,
) -> None:
    test_path = tmp_path / "tests/test_runtime_outcomes.py"
    test_path.parent.mkdir()
    test_path.write_text(
        "import sqlite3\n"
        "import warnings\n\n"
        "import pytest\n\n"
        "def test_pass_profile(state_engine_profile):\n"
        "    connection = sqlite3.connect(':memory:')\n"
        "    state_engine_profile.observe_sqlite(\n"
        "        connection, deployment='single-process-leader'\n"
        "    )\n\n"
        "@pytest.mark.xfail(reason='expected failure')\n"
        "def test_xfail():\n"
        "    assert False\n\n"
        "@pytest.mark.xfail(reason='unexpected pass')\n"
        "def test_xpass():\n"
        "    pass\n\n"
        "@pytest.mark.skip(reason='expected skip')\n"
        "def test_skip():\n"
        "    pass\n\n"
        "def test_fail():\n"
        "    assert False\n\n"
        "@pytest.fixture\n"
        "def broken_fixture():\n"
        "    raise RuntimeError('setup exploded')\n\n"
        "def test_error(broken_fixture):\n"
        "    pass\n\n"
        "def test_warning():\n"
        "    warnings.warn('profile warning', UserWarning)\n",
        encoding="utf-8",
    )
    profile_path = tmp_path / "profile.json"
    junit_path = tmp_path / "result.junit.xml"
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": str(REPOSITORY_ROOT),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            PROFILE_REPORTER_PLUGIN,
            f"--state-engine-profile-report={profile_path}",
            f"--junitxml={junit_path}",
            "tests/test_runtime_outcomes.py",
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    prefix = "tests/test_runtime_outcomes.py::"
    assert profile["outcomes"] == [
        {"node_id": f"{prefix}test_pass_profile", "outcome": "passed"},
        {"node_id": f"{prefix}test_xfail", "outcome": "xfailed"},
        {"node_id": f"{prefix}test_xpass", "outcome": "xpassed"},
        {"node_id": f"{prefix}test_skip", "outcome": "skipped"},
        {"node_id": f"{prefix}test_fail", "outcome": "failed"},
        {"node_id": f"{prefix}test_error", "outcome": "error"},
        {"node_id": f"{prefix}test_warning", "outcome": "passed"},
    ]
    assert len(profile["warnings"]) == 1
    warning = profile["warnings"][0]
    assert warning == {
        "node_id": f"{prefix}test_warning",
        "when": "runtest",
        "category": "builtins.UserWarning",
        "message": "profile warning",
        "filename": warning["filename"],
        "line": warning["line"],
    }
    assert warning["filename"].endswith("tests/test_runtime_outcomes.py")
    assert type(warning["line"]) is int and warning["line"] > 0


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
        pytest.param(
            lambda catalog: catalog["applicability_profiles"].update(
                {"unused-extra": dict(catalog["applicability_profiles"]["all-required-v2"])}
            ),
            "applicability profile namespace",
            id="unused-applicability-profile",
        ),
        pytest.param(
            lambda catalog: _remove_required_case(catalog, "PB-06"),
            "canonical v2 leg manifest",
            id="reduced-pb06-cases",
        ),
        pytest.param(
            lambda catalog: _remove_required_case(catalog, "PB-07"),
            "canonical v2 leg manifest",
            id="reduced-pb07-cases",
        ),
        pytest.param(
            lambda catalog: _catalog_leg(catalog, "TS-00").pop("contract"),
            "canonical v2 leg manifest",
            id="missing-leg-contract",
        ),
        pytest.param(
            lambda catalog: catalog["applicability_profiles"]["all-required-v2"].update({"default_case_id": "ignored-drift"}),
            "canonical v2 applicability",
            id="changed-default-case",
        ),
        pytest.param(
            lambda catalog: catalog["family_dimension_acceptance"]["transition"].update({"production_entry": 7}),
            "canonical v2 family acceptance",
            id="non-string-acceptance",
        ),
        pytest.param(
            lambda catalog: catalog["hard_gates"][0].update({"title": ""}),
            "canonical v2 hard gates",
            id="empty-hard-gate-title",
        ),
        pytest.param(
            lambda catalog: _catalog_leg(catalog, "TS-00").update({"ignored_extra": True}),
            "canonical v2 leg manifest",
            id="extra-leg-field",
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


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda catalog: catalog.update({"schema_version": True}), id="boolean-schema-version"),
        pytest.param(
            lambda catalog: catalog["execution_profiles"].update({"unversioned_extension": []}),
            id="extra-profile-block-field",
        ),
        pytest.param(
            lambda catalog: catalog["execution_profiles"]["state_store"][0].update({"unversioned_extension": True}),
            id="extra-state-store-field",
        ),
        pytest.param(
            lambda catalog: catalog["execution_profiles"]["first_party_plugins"].update({"inventory_rule": True}),
            id="invalid-inventory-rule-type",
        ),
    ],
)
def test_validate_catalog_cli_rejects_unversioned_execution_profile_schema_drift(
    tmp_path: Path,
    mutation: Any,
) -> None:
    catalog = json.loads(V2_CATALOG_PATH.read_text(encoding="utf-8"))
    mutation(catalog)
    catalog_path = tmp_path / "catalog.json"
    _write_json(catalog_path, catalog)

    result = _run_assessment_cli("validate-catalog", str(catalog_path))

    assert result.returncode == 1


def test_validate_package_accepts_complete_minimal_manifest(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 0, result.stderr
    assert "state-engine assessment contract: valid" in result.stdout
    assert "73 legs" in result.stdout
    assert "not_complete" in result.stdout


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        pytest.param(lambda value: value.update({"schema_version": True}), "schema_version", id="boolean-schema-version"),
        pytest.param(lambda value: value.update({"assessment_id": "../escape"}), "assessment_id", id="bad-assessment-id"),
        pytest.param(lambda value: value.update({"unversioned_extension": True}), "top-level", id="extra-top-level-field"),
        pytest.param(lambda value: value.pop("limitations"), "top-level", id="missing-top-level-field"),
        pytest.param(
            lambda value: value["environment"].update({"unversioned_extension": True}),
            "environment",
            id="extra-environment-field",
        ),
        pytest.param(
            lambda value: [
                counts.update({key: False for key, count in counts.items() if count == 0})
                for counts in [*value["derived"]["family_counts"].values(), value["derived"]["total"]]
            ],
            "derived",
            id="boolean-derived-counts",
        ),
    ],
)
def test_validate_package_rejects_assessment_schema_drift(
    assessment_repository: tuple[Path, Path],
    mutation: Any,
    expected_error: str,
) -> None:
    repository, assessment_path = assessment_repository
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    mutation(assessment)
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert expected_error in result.stderr


def test_validate_package_accepts_historical_object_provenance(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    assessment["baseline"] = {
        "mode": "historical",
        "commit": _git(repository, "rev-parse", "HEAD"),
        "tree": _git(repository, "rev-parse", "HEAD^{tree}"),
    }
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 0, result.stderr


def test_validate_package_rejects_current_provenance_for_historical_checkout(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    historical_commit = _git(repository, "rev-parse", "HEAD")
    historical_tree = _git(repository, "rev-parse", "HEAD^{tree}")
    (repository / "docs/historical-drift.md").write_text("later documentation\n", encoding="utf-8")
    _git(repository, "add", "docs/historical-drift.md")
    _git(repository, "commit", "-qm", "later checkout")
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    assessment["baseline"].update({"mode": "current", "commit": historical_commit, "tree": historical_tree})
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert "current" in result.stderr


def test_validate_package_and_collect_evidence_accept_same_genuine_runtime_artifacts(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    record = _genuine_sqlite_evidence(repository)
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    assessment["baseline"]["commit"] = _git(repository, "rev-parse", "HEAD")
    assessment["baseline"]["tree"] = _git(repository, "rev-parse", "HEAD^{tree}")
    assessment["evidence"] = [record]
    _write_json(assessment_path, assessment)

    validation = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)
    collection = _run_assessment_cli("collect-evidence", str(assessment_path), cwd=repository)

    assert validation.returncode == 0, validation.stderr
    assert collection.returncode == 0, collection.stderr
    assert "1 pytest records" in collection.stdout


def test_validate_package_ignores_relabelled_human_stdout_when_machine_outcomes_are_unchanged(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    _materialize_complete_assessment(repository, assessment_path, one_node_per_profile=False)
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    record = assessment["evidence"][0]
    stdout_record = _artifact_by_kind(record, "stdout")
    stdout_path = repository / stdout_record["path"]
    stdout_path.write_text("1 xpassed in 0.01s\n", encoding="utf-8")
    stdout_record["sha256"] = hashlib.sha256(stdout_path.read_bytes()).hexdigest()
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 0, result.stderr
    assert "complete" in result.stdout


def test_both_validators_reject_complete_package_relabelled_as_xpass_in_machine_outcomes(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    _materialize_complete_assessment(repository, assessment_path, one_node_per_profile=False)
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    record = assessment["evidence"][0]
    stdout_record = _artifact_by_kind(record, "stdout")
    stdout_path = repository / stdout_record["path"]
    stdout_path.write_text("1 xpassed in 0.01s\n", encoding="utf-8")
    stdout_record["sha256"] = hashlib.sha256(stdout_path.read_bytes()).hexdigest()
    profile_record = _artifact_by_kind(record, "profile_report")
    profile_path = repository / profile_record["path"]
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["outcomes"][0]["outcome"] = "xpassed"
    _write_json(profile_path, profile)
    profile_record["sha256"] = hashlib.sha256(profile_path.read_bytes()).hexdigest()
    _write_json(assessment_path, assessment)

    for command in ("validate-package", "collect-evidence"):
        result = _run_assessment_cli(command, str(assessment_path), cwd=repository)

        assert result.returncode == 1
        assert "machine-reported outcome counts do not match result_counts" in result.stderr


@pytest.mark.parametrize("mismatch", ["xfailed", "warning"])
def test_both_validators_reject_machine_outcome_or_warning_count_mismatch(
    assessment_repository: tuple[Path, Path],
    mismatch: str,
) -> None:
    repository, assessment_path = assessment_repository
    node = "tests/unit/test_state_engine_contract.py::test_retained_outcome"
    counts = {
        "passed": int(mismatch == "warning"),
        "failed": 0,
        "errors": 0,
        "skipped": int(mismatch == "xfailed"),
        "xfailed": 0,
        "xpassed": 0,
        "warnings": 0,
    }
    record = _pytest_evidence(
        repository,
        evidence_id="EV-OUTCOME-MISMATCH",
        profile_case="sqlite-wal-single-process-leader",
        coverage=[],
        nodes=[node],
        counts=counts,
    )
    profile_record = _artifact_by_kind(record, "profile_report")
    profile_path = repository / profile_record["path"]
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    if mismatch == "xfailed":
        profile["outcomes"][0]["outcome"] = "xfailed"
    else:
        profile["warnings"].append(
            {
                "node_id": node,
                "when": "runtest",
                "category": "builtins.UserWarning",
                "message": "unreported warning",
                "filename": "tests/unit/test_state_engine_contract.py",
                "line": 1,
            }
        )
    _write_json(profile_path, profile)
    profile_record["sha256"] = hashlib.sha256(profile_path.read_bytes()).hexdigest()
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    assessment["evidence"] = [record]
    _write_json(assessment_path, assessment)

    for command in ("validate-package", "collect-evidence"):
        result = _run_assessment_cli(command, str(assessment_path), cwd=repository)

        assert result.returncode == 1
        assert "machine-reported outcome counts do not match result_counts" in result.stderr


def test_validate_package_and_collect_evidence_share_command_and_environment_contract(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    record = _genuine_sqlite_evidence(repository)
    base = json.loads(assessment_path.read_text(encoding="utf-8"))
    base["baseline"]["commit"] = _git(repository, "rev-parse", "HEAD")
    base["baseline"]["tree"] = _git(repository, "rev-parse", "HEAD^{tree}")
    cases = [
        ("command", lambda item: item.update({"argv": [sys.executable, "-c", "raise SystemExit(0)"]}), "trusted pytest"),
        (
            "environment",
            lambda item: item.update({"safe_environment": {"AWS_SECRET_ACCESS_KEY": "not-a-real-secret"}}),
            "safe_environment name is not permitted",
        ),
    ]
    for _case, mutation, expected_error in cases:
        assessment = json.loads(json.dumps(base))
        evidence = json.loads(json.dumps(record))
        mutation(evidence)
        assessment["evidence"] = [evidence]
        _write_json(assessment_path, assessment)

        validation = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)
        collection = _run_assessment_cli("collect-evidence", str(assessment_path), cwd=repository)

        assert validation.returncode == collection.returncode == 1
        assert expected_error in validation.stderr
        assert expected_error in collection.stderr


def test_validate_package_rejects_junit_node_identity_outside_recorded_selectors(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    record = _genuine_sqlite_evidence(repository)
    _replace_genuine_node_identity(repository, record, "tests/test_unrelated.py::test_fabricated")
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    assessment["baseline"]["commit"] = _git(repository, "rev-parse", "HEAD")
    assessment["baseline"]["tree"] = _git(repository, "rev-parse", "HEAD^{tree}")
    assessment["evidence"] = [record]
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert "JUnit node identity is outside the recorded selectors" in result.stderr


def test_validate_package_rejects_false_junit_aggregate_counts(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    record = _genuine_sqlite_evidence(repository)
    junit_record = _artifact_by_kind(record, "junit_xml")
    junit_path = repository / junit_record["path"]
    junit = ET.parse(junit_path)
    suite = next(element for element in junit.getroot().iter("testsuite"))
    suite.set("tests", "999")
    junit.write(junit_path, encoding="utf-8", xml_declaration=True)
    junit_record["sha256"] = hashlib.sha256(junit_path.read_bytes()).hexdigest()
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    assessment["baseline"]["commit"] = _git(repository, "rev-parse", "HEAD")
    assessment["baseline"]["tree"] = _git(repository, "rev-parse", "HEAD^{tree}")
    assessment["evidence"] = [record]
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert "JUnit aggregate counts disagree with testcase evidence" in result.stderr


def test_collect_evidence_is_static_and_does_not_import_recorded_test_modules(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    record = _genuine_sqlite_evidence(repository)
    marker = repository / "project-code-executed"
    test_relative = "tests/test_static_collection.py"
    node_id = f"{test_relative}::test_static_collection"
    (repository / test_relative).write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n\ndef test_static_collection():\n    pass\n",
        encoding="utf-8",
    )
    _git(repository, "add", test_relative)
    _git(repository, "commit", "-qm", "add static collection sentinel")
    record["argv"][-1] = node_id
    _replace_genuine_node_identity(repository, record, node_id)
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    assessment["baseline"]["commit"] = _git(repository, "rev-parse", "HEAD")
    assessment["baseline"]["tree"] = _git(repository, "rev-parse", "HEAD^{tree}")
    assessment["evidence"] = [record]
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("collect-evidence", str(assessment_path), cwd=repository)

    assert result.returncode == 0, result.stderr
    assert not marker.exists()


def test_validate_package_rejects_one_node_fabricated_complete(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    _materialize_complete_assessment(repository, assessment_path, one_node_per_profile=True)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert "one proof subject" in result.stderr


def test_validate_package_rejects_documentary_evidence_for_behavioral_pass(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    assessment = json.loads(assessment_path.read_text())
    profile_case = "sqlite-wal-single-process-leader"
    node = "tests/unit/test_state_engine_contract.py::test_documented_behavior"
    record = _pytest_evidence(
        repository,
        evidence_id="EV-DOCUMENT",
        profile_case=profile_case,
        coverage=_ts00_coverage(profile_case, [node]),
        nodes=[node],
    )
    record["kind"] = "documentation"
    record.pop("collected_node_index")
    record.pop("collected_nodes")
    _attach_ts00_pass(assessment, record, profile_case=profile_case)
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert "documentation evidence cannot promote" in result.stderr


def test_validate_package_rejects_documentary_evidence_for_behavioral_fail(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    assessment = json.loads(assessment_path.read_text())
    profile_case = "sqlite-wal-single-process-leader"
    node = "tests/unit/test_state_engine_contract.py::test_documented_failure"
    record = _pytest_evidence(
        repository,
        evidence_id="EV-DOCUMENT-FAIL",
        profile_case=profile_case,
        coverage=_ts00_coverage(profile_case, [node]),
        nodes=[node],
    )
    record["kind"] = "documentation"
    record.pop("collected_node_index")
    record.pop("collected_nodes")
    assessment["evidence"] = [record]
    ts00 = next(leg for leg in assessment["legs"] if leg["id"] == "TS-00")
    ts00["derived_verdict"] = "gap"
    ts00["overrides"] = [
        {
            "dimension": "production_entry",
            "case": "leg-contract",
            "profile_case": profile_case,
            "status": "fail",
            "evidence": [record["id"]],
            "reason": "A document claims the behavior fails.",
            "owner_issue": None,
            "exit_gate": "Execute the behavioral proof.",
        }
    ]
    hg09 = next(gate for gate in assessment["hard_gates"] if gate["id"] == "HG-09-mandatory-leg-unresolved")
    hg09["support"] = [record["id"]]
    assessment["derived"]["family_counts"]["transition"] = {"confirmed": 0, "gap": 1, "unknown": 19}
    assessment["derived"]["total"] = {"confirmed": 0, "gap": 1, "unknown": 72}
    _write_json(assessment_path, assessment)
    matrix_path = repository / "docs/architecture/state_engine/proof-matrix.md"
    matrix = matrix_path.read_text()
    matrix = matrix.replace("| Token transitions | 20 | 0 | 0 | 20 |", "| Token transitions | 20 | 0 | 1 | 19 |")
    matrix = matrix.replace("| **Total** | **73** | **0** | **0** | **73** |", "| **Total** | **73** | **0** | **1** | **72** |")
    matrix_path.write_text(matrix)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert "documentation evidence cannot establish behavioral fail" in result.stderr


def test_validate_package_rejects_zero_node_pytest_promotion(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    assessment = json.loads(assessment_path.read_text())
    profile_case = "sqlite-wal-single-process-leader"
    record = _pytest_evidence(
        repository,
        evidence_id="EV-ZERO-NODES",
        profile_case=profile_case,
        coverage=_ts00_coverage(profile_case, []),
        nodes=[],
    )
    _attach_ts00_pass(assessment, record, profile_case=profile_case)
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert "positive collected-node count" in result.stderr


def test_validate_package_rejects_skipped_only_pytest_promotion(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    assessment = json.loads(assessment_path.read_text())
    profile_case = "sqlite-wal-single-process-leader"
    node = "tests/unit/test_state_engine_contract.py::test_skipped_behavior"
    record = _pytest_evidence(
        repository,
        evidence_id="EV-SKIPPED",
        profile_case=profile_case,
        coverage=_ts00_coverage(profile_case, [node]),
        nodes=[node],
        counts={"passed": 0, "failed": 0, "errors": 0, "skipped": 1, "xfailed": 0, "xpassed": 0, "warnings": 0},
    )
    _attach_ts00_pass(assessment, record, profile_case=profile_case)
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert "skipped pytest evidence cannot promote" in result.stderr


def test_validate_package_rejects_profile_provenance_mismatch(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    assessment = json.loads(assessment_path.read_text())
    coverage_profile = "sqlite-wal-single-process-leader"
    node = "tests/unit/test_state_engine_contract.py::test_profile_mismatch"
    record = _pytest_evidence(
        repository,
        evidence_id="EV-PROFILE-MISMATCH",
        profile_case="postgresql-16-aws-single-leader-landscape",
        coverage=_ts00_coverage(coverage_profile, [node]),
        nodes=[node],
    )
    _attach_ts00_pass(assessment, record, profile_case=coverage_profile)
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert "runtime profile does not match coverage" in result.stderr


def test_validate_package_rejects_non_postgresql_16_provenance(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    assessment = json.loads(assessment_path.read_text())
    profile_case = "postgresql-16-aws-single-leader-landscape"
    node = "tests/unit/test_state_engine_contract.py::test_postgresql_behavior"
    record = _pytest_evidence(
        repository,
        evidence_id="EV-POSTGRES-15",
        profile_case=profile_case,
        coverage=_ts00_coverage(profile_case, [node]),
        nodes=[node],
    )
    profile_record = _artifact_by_kind(record, "profile_report")
    profile_path = repository / profile_record["path"]
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["backend_version"] = "15.9"
    _write_json(profile_path, profile)
    profile_record["sha256"] = hashlib.sha256(profile_path.read_bytes()).hexdigest()
    _attach_ts00_pass(assessment, record, profile_case=profile_case)
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert "PostgreSQL backend version must be 16.x" in result.stderr


def test_validate_package_rejects_bool_execution_scalars(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    assessment = json.loads(assessment_path.read_text())
    profile_case = "sqlite-wal-single-process-leader"
    node = "tests/unit/test_state_engine_contract.py::test_scalar_behavior"
    record = _pytest_evidence(
        repository,
        evidence_id="EV-BOOL-SCALAR",
        profile_case=profile_case,
        coverage=_ts00_coverage(profile_case, [node]),
        nodes=[node],
    )
    record["timeout_seconds"] = False
    _attach_ts00_pass(assessment, record, profile_case=profile_case)
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert "timeout_seconds must be a positive integer" in result.stderr


def test_validate_package_rejects_malformed_evidence_timestamps(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    assessment = json.loads(assessment_path.read_text())
    profile_case = "sqlite-wal-single-process-leader"
    node = "tests/unit/test_state_engine_contract.py::test_timestamp_behavior"
    record = _pytest_evidence(
        repository,
        evidence_id="EV-BAD-TIME",
        profile_case=profile_case,
        coverage=_ts00_coverage(profile_case, [node]),
        nodes=[node],
    )
    record["started_at"] = "not-a-timestamp"
    _attach_ts00_pass(assessment, record, profile_case=profile_case)
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert "started_at must be an offset-aware timestamp" in result.stderr


def test_validate_package_rejects_current_live_repository_root_mismatch(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    assessment = json.loads(assessment_path.read_text())
    assessment["baseline"]["repository_root"] = "/tmp/not-this-repository"
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert "current repository_root" in result.stderr


def test_validate_package_rejects_closed_gate_without_derived_support(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    assessment = json.loads(assessment_path.read_text())
    hg01 = next(gate for gate in assessment["hard_gates"] if gate["id"] == "HG-01-invalid-subtype")
    hg01["status"] = "closed"
    hg01["reason"] = "Claimed closed without cell evidence."
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert "hard gate HG-01-invalid-subtype does not match derived proof cells" in result.stderr


def test_validate_package_rejects_non_string_gate_reason(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    assessment = json.loads(assessment_path.read_text())
    assessment["hard_gates"][0]["reason"] = 7
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert "hard gate HG-01-invalid-subtype reason must be a non-empty string" in result.stderr


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


def test_validate_package_accepts_catalog_approved_follower_not_applicable(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    rc05 = next(leg for leg in assessment["legs"] if leg["id"] == "RC-05")
    rc05["overrides"] = [
        {
            "dimension": "production_entry",
            "case": "leg-contract",
            "profile_case": "postgresql-16-aws-single-leader-landscape",
            "status": "not_applicable",
            "evidence": [],
        }
    ]
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("status", ["pass", "partial", "fail", "unknown"])
def test_validate_package_rejects_status_promotion_of_catalog_not_applicable_cell(
    assessment_repository: tuple[Path, Path],
    status: str,
) -> None:
    repository, assessment_path = assessment_repository
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    rc05 = next(leg for leg in assessment["legs"] if leg["id"] == "RC-05")
    override: dict[str, Any] = {
        "dimension": "production_entry",
        "case": "leg-contract",
        "profile_case": "postgresql-16-aws-single-leader-landscape",
        "status": status,
        "evidence": [],
    }
    if status in {"partial", "fail", "unknown"}:
        override.update(
            {
                "reason": "An assessor cannot promote a catalog-N/A cell.",
                "owner_issue": None,
                "exit_gate": "Revise the versioned catalog if applicability changes.",
            }
        )
    rc05["overrides"] = [override]
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert "catalog not_applicable cell must remain not_applicable" in result.stderr


def test_validate_package_rejects_assessor_waiver_of_required_profile_cell(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    ts00 = next(leg for leg in assessment["legs"] if leg["id"] == "TS-00")
    ts00["overrides"] = [
        {
            "dimension": "production_entry",
            "case": "leg-contract",
            "profile_case": "postgresql-16-aws-single-leader-landscape",
            "status": "not_applicable",
            "evidence": [],
        }
    ]
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert "cannot waive a required profile cell" in result.stderr


def test_validate_package_rejects_false_derived_totals_and_verdict(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    assessment["derived"]["total"] = {"confirmed": 73, "gap": 0, "unknown": 0}
    assessment["derived"]["overall_verdict"] = "complete"
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert "derived total" in result.stderr


@pytest.mark.parametrize("field", ["reason", "owner_issue", "exit_gate"])
def test_validate_package_rejects_empty_unresolved_leg_metadata(
    assessment_repository: tuple[Path, Path],
    field: str,
) -> None:
    repository, assessment_path = assessment_repository
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    assessment["legs"][0][field] = ""
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert f"leg TS-00 unresolved {field}" in result.stderr


def test_validate_package_rejects_empty_unresolved_override_metadata(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    assessment["legs"][0]["overrides"] = [
        {
            "dimension": "production_entry",
            "case": "leg-contract",
            "profile_case": "sqlite-wal-single-process-leader",
            "status": "unknown",
            "evidence": [],
            "reason": "",
            "owner_issue": None,
            "exit_gate": "Collect production-entry evidence.",
        }
    ]
    _write_json(assessment_path, assessment)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert "leg TS-00 unknown override reason" in result.stderr


def test_validate_package_rejects_placeholder_in_assessment_readme(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    readme_path = assessment_path.parent / "README.md"
    readme_path.write_text(readme_path.read_text(encoding="utf-8") + "\nTODO: fill this in.\n", encoding="utf-8")

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert "unresolved placeholder found" in result.stderr
    assert "assessments/minimal-assessment/README.md:5:TODO" in result.stderr


def test_collect_evidence_cli_validates_empty_pytest_inventory(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository

    result = _run_assessment_cli("collect-evidence", str(assessment_path), cwd=repository)

    assert result.returncode == 0, result.stderr
    assert "retained evidence validation: valid (0 pytest records)" in result.stdout


def test_init_full_rejects_preexisting_destination(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, _assessment_path = assessment_repository
    destination = repository / "docs/architecture/state_engine/assessments/preexisting"
    destination.mkdir()

    result = _run_assessment_cli("init-full", "preexisting", str(destination), cwd=repository)

    assert result.returncode == 1
    assert "destination already exists" in result.stderr
    assert list(destination.iterdir()) == []


def test_init_full_publishes_completion_marker_last(
    assessment_repository: tuple[Path, Path],
) -> None:
    _repository, assessment_path = assessment_repository
    marker = assessment_path.parent / ".state-engine-assessment.ready"

    assert marker.read_text(encoding="utf-8") == "state-engine-assessment-package-v2\n"


def test_validate_package_rejects_incomplete_reserved_package(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, assessment_path = assessment_repository
    marker = assessment_path.parent / ".state-engine-assessment.ready"
    marker.unlink(missing_ok=True)

    result = _run_assessment_cli("validate-package", str(assessment_path), cwd=repository)

    assert result.returncode == 1
    assert "completion marker" in result.stderr


def test_init_full_does_not_replace_directory_created_after_advisory_check(
    assessment_repository: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _assessment_path = assessment_repository
    destination = repository / "docs/architecture/state_engine/assessments/concurrent-empty"
    initialize_full = _assessment_namespace()["initialize_full"]
    original_lstat = Path.lstat
    destination_checks = 0

    def create_destination_after_second_check(path: Path) -> os.stat_result:
        nonlocal destination_checks
        if path == destination:
            destination_checks += 1
            if destination_checks == 2:
                os.mkdir(destination)
                raise FileNotFoundError(destination)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", create_destination_after_second_check)

    with pytest.raises(ValueError, match="destination already exists"):
        initialize_full("concurrent-empty", destination)

    assert destination.is_dir()
    assert list(destination.iterdir()) == []


def test_init_full_rejects_symlink_destination_without_touching_target(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, _assessment_path = assessment_repository
    assessments = repository / "docs/architecture/state_engine/assessments"
    target = assessments / "symlink-target"
    target.mkdir()
    destination = assessments / "symlink-destination"
    destination.symlink_to(target, target_is_directory=True)

    result = _run_assessment_cli("init-full", "symlink", str(destination), cwd=repository)

    assert result.returncode == 1
    assert "destination already exists" in result.stderr
    assert list(target.iterdir()) == []


def test_init_full_cleans_staged_directory_after_write_failure(
    assessment_repository: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _assessment_path = assessment_repository
    destination = repository / "docs/architecture/state_engine/assessments/write-failure"
    initialize_full = _assessment_namespace()["initialize_full"]
    original_write_text = Path.write_text

    def fail_evidence_write(path: Path, data: str, *args: Any, **kwargs: Any) -> int:
        if path.name == "evidence.md":
            raise OSError("injected template write failure")
        return original_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_evidence_write)

    with pytest.raises(OSError, match="injected template write failure"):
        initialize_full("write-failure", destination)

    assert not destination.exists()
    assert list(destination.parent.glob(".write-failure.tmp-*")) == []


def test_check_links_rejects_absolute_local_target(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, _assessment_path = assessment_repository
    attack = repository / "docs/architecture/state_engine/absolute-link.md"
    attack.write_text("[host file](/etc/passwd)\n")

    result = _run_assessment_cli("check-links", cwd=repository)

    assert result.returncode == 1
    assert "link target must be repository-relative" in result.stderr


def test_check_links_rejects_missing_reference_definition_target(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, _assessment_path = assessment_repository
    attack = repository / "docs/architecture/state_engine/reference-link.md"
    attack.write_text("Use [the missing reference][missing].\n\n[missing]: absent.md\n", encoding="utf-8")

    result = _run_assessment_cli("check-links", cwd=repository)

    assert result.returncode == 1
    assert "reference-link.md -> absent.md" in result.stderr


def test_check_links_rejects_traversal_outside_repository(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, _assessment_path = assessment_repository
    outside = repository.parent / "outside.md"
    outside.write_text("outside\n")
    attack = repository / "docs/architecture/state_engine/traversal-link.md"
    attack.write_text("[outside](../../../../outside.md)\n")

    result = _run_assessment_cli("check-links", cwd=repository)

    assert result.returncode == 1
    assert "link target escapes the repository" in result.stderr


def test_check_links_rejects_symlink_escape(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, _assessment_path = assessment_repository
    outside = repository.parent / "external-docs"
    outside.mkdir()
    (outside / "target.md").write_text("outside\n")
    state_engine = repository / "docs/architecture/state_engine"
    (state_engine / "external").symlink_to(outside, target_is_directory=True)
    (state_engine / "symlink-link.md").write_text("[outside](external/target.md)\n")

    result = _run_assessment_cli("check-links", cwd=repository)

    assert result.returncode == 1
    assert "link target escapes through a symlink" in result.stderr


def test_check_links_rejects_symlink_document_escape(
    assessment_repository: tuple[Path, Path],
) -> None:
    repository, _assessment_path = assessment_repository
    outside = repository.parent / "external-document.md"
    outside.write_text("outside\n")
    attack = repository / "docs/architecture/state_engine/symlink-document.md"
    attack.symlink_to(outside)

    result = _run_assessment_cli("check-links", cwd=repository)

    assert result.returncode == 1
    assert "documentation input cannot be a symlink" in result.stderr


def test_main_reports_unexpected_failures_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    namespace = _assessment_namespace()
    main = namespace["main"]

    def fail_unexpectedly(_root: Path) -> int:
        raise RuntimeError("injected internal failure\nprivate detail")

    monkeypatch.setitem(main.__globals__, "check_links", fail_unexpectedly)

    result = main(["check-links"])
    captured = capsys.readouterr()

    assert result == 1
    assert captured.out == ""
    assert captured.err == "state-engine assessment: internal error (RuntimeError): injected internal failure\n"
    assert "Traceback" not in captured.err


def test_assessment_wrapper_precedes_ambient_pythonpath_packages(tmp_path: Path) -> None:
    fake_root = tmp_path / "ambient"
    fake_package = fake_root / "scripts/state_engine_assessment_lib"
    fake_package.mkdir(parents=True)
    (fake_root / "scripts/__init__.py").write_text("", encoding="utf-8")
    (fake_package / "__init__.py").write_text("", encoding="utf-8")
    marker = tmp_path / "ambient-package-imported"
    import_side_effect = f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n"
    (fake_package / "common.py").write_text(
        import_side_effect + "def load_unique_json(path):\n    return {}\n",
        encoding="utf-8",
    )
    (fake_package / "operations.py").write_text(import_side_effect + "def main(argv=None):\n    return 0\n", encoding="utf-8")
    (fake_package / "package.py").write_text(import_side_effect + "def initialize_full(*args):\n    return None\n", encoding="utf-8")
    pythonpath = os.pathsep.join((str(fake_root), str(REPOSITORY_ROOT), str(REPOSITORY_ROOT / "src")))

    result = _run_assessment_cli("--help", environment_overrides={"PYTHONPATH": pythonpath})

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    assert not marker.exists()
