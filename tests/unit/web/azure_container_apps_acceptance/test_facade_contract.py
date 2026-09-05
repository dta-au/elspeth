"""Permanent contract tests for the Azure Container Apps acceptance facade (mirrors the ECS ``test_facade_contract.py``).

Closed command set, public exports importable, no provider SDK anywhere in the
package or facade, one-way private layering, static failure envelopes, and the
module executable through ``python -m`` with an explicit environment.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from elspeth.web import azure_container_apps_acceptance as facade
from elspeth.web._acceptance_common.errors import AcceptanceCheckError, AcceptanceHttpError, AcceptanceInputError
from elspeth.web._acceptance_common.schema_facts import _expected_schema_facts

from .test_receipt_contracts import APP_ID, BINDING, CANDIDATE, REPLICA, REVISION, SHA, VALID, _envelope

REPO_ROOT = Path(__file__).resolve().parents[4]
FACADE = REPO_ROOT / "src" / "elspeth" / "web" / "azure_container_apps_acceptance.py"
PACKAGE = REPO_ROOT / "src" / "elspeth" / "web" / "_azure_container_apps_acceptance"

EXPECTED_COMMANDS = {
    "bundle-validate",
    "compatibility-record-gate",
    "compatibility-record-validate",
    "extract-exec-receipt",
    "partition-owner",
    "receipt-store",
    "replica-probes",
    "resource-graph-cleanup-validate",
    "restore-owner",
    "revision-rollout",
    "verify-blob-managed-identity",
    "verify-connection-budget",
    "verify-doctor-job",
    "verify-log-analytics",
    "verify-storage-job",
}

EXPECTED_PUBLIC_EXPORTS = {
    "AcceptanceErrorEnvelope",
    "BundleVerdict",
    "CHECK_KINDS",
    "COMPATIBILITY_RECEIPT_SCHEMA",
    "CONNECTION_BUDGET_SCHEMA",
    "INGRESS_REQUEST_TIMEOUT_SECONDS",
    "MECHANISMS",
    "PLATFORM_COMMAND_TIMEOUT_SECONDS",
    "PROBE_ROLES",
    "PROBE_STATUS_PATH",
    "PROBE_TOPOLOGY",
    "acceptance_error_envelope",
    "build_parser",
    "main",
    "probe_topology_check",
}

FORBIDDEN_IMPORT_PREFIXES = ("azure", "msal", "boto3", "botocore", "elspeth.web._aws_ecs_acceptance", "elspeth.web.aws_ecs_acceptance")
BINDING_ARGS = ["--candidate-sha", CANDIDATE, "--container-app-id", APP_ID, "--revision", REVISION, "--replica", REPLICA]


def _imports(path: Path) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
            imported.add(node.module)
    return imported


def test_command_surface_is_the_exact_reviewed_set() -> None:
    parser = facade.build_parser()
    subparsers = next(action for action in parser._actions if action.dest == "command")
    assert isinstance(subparsers, argparse._SubParsersAction)
    assert set(subparsers.choices) == EXPECTED_COMMANDS


def test_public_exports_are_the_exact_reviewed_set() -> None:
    assert set(facade.__all__) == EXPECTED_PUBLIC_EXPORTS
    assert not EXPECTED_PUBLIC_EXPORTS.difference(vars(facade))


def test_no_provider_sdk_and_no_ecs_import_in_the_facade_or_the_package() -> None:
    for path in (FACADE, *sorted(PACKAGE.glob("*.py"))):
        offending = {
            name
            for name in _imports(path)
            if name.split(".")[0] in {"azure", "msal", "boto3", "botocore"} or name.startswith(FORBIDDEN_IMPORT_PREFIXES[4:])
        }
        assert not offending, f"{path.name} imports {sorted(offending)}"


def test_private_package_never_imports_the_facade() -> None:
    for path in sorted(PACKAGE.glob("*.py")):
        assert "elspeth.web.azure_container_apps_acceptance" not in _imports(path), path.name


def test_static_failure_envelopes_never_carry_content() -> None:
    check = facade.acceptance_error_envelope(AcceptanceCheckError("exec_receipt_schema", missing=("b", "a")))
    assert check == {"error_class": "AcceptanceCheckError", "check": "exec_receipt_schema", "missing": ["a", "b"], "step": None}
    http = facade.acceptance_error_envelope(AcceptanceHttpError("body: secret", error_code="unexpected_http_status", status=502))
    assert http == {"error_class": "AcceptanceHttpError", "error_code": "unexpected_http_status", "status": 502, "step": None}
    unknown = facade.acceptance_error_envelope(AcceptanceInputError("x"))
    assert unknown["error_code"] == "input_invalid"
    assert facade.acceptance_error_envelope(RuntimeError("password=hunter2")) == {
        "error_class": "RuntimeError",
        "error_code": "acceptance_internal",
        "step": None,
    }


def _protected(path: Path, document: object) -> str:
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    return str(path)


def test_verify_commands_emit_a_bound_receipt_line_and_extract_round_trips(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    execution = _protected(tmp_path / "execution.json", {"name": "doctor-runtime-a-abc", "properties": {"status": "Succeeded"}})
    report = tmp_path / "report.json"
    report.write_bytes(
        json.dumps(
            [
                {"name": name, "ok": True, "detail": ""}
                for name in ("session_schema", "landscape_schema", "data_dir_writable", "payload_store_writable", "blob_writable")
            ]
        ).encode()
    )
    assert (
        facade.main(
            ["verify-doctor-job", *BINDING_ARGS, "--job-name", "doctor-runtime-a", "--execution", execution, "--report", str(report)]
        )
        == 0
    )
    line = capsys.readouterr().out
    assert line.startswith("ELSPETH_ACCEPTANCE_RECEIPT_V1:") and "detail" not in line
    monkeypatch.setattr("sys.stdin", _Stdin(line))
    assert facade.main(["extract-exec-receipt", *BINDING_ARGS, "--check", "verify-doctor-job"]) == 0
    extracted = json.loads(capsys.readouterr().out)
    assert extracted["replica_binding_sha256"] == BINDING.sha256 and extracted["details"]["job_name"] == "doctor-runtime-a"
    monkeypatch.setattr("sys.stdin", _Stdin(line))
    assert facade.main(["extract-exec-receipt", *BINDING_ARGS, "--check", "verify-storage-job"]) == 1
    assert json.loads(capsys.readouterr().err)["check"] == "check_binding"


class _Stdin:
    def __init__(self, text: str) -> None:
        self._text = text
        self.buffer = _Buffer(text.encode())

    def read(self, limit: int) -> str:
        return self._text[:limit]


class _Buffer:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def read(self, limit: int) -> bytes:
        return self._content[:limit]


def test_compatibility_record_validate_binds_the_record_and_the_gate_delegates_to_the_shared_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    record = {
        "schema": "elspeth.azure-container-apps-compatibility-receipt.v1",
        "record_id": "change-1",
        "acceptance_run_id": "run-1",
        "scenario_id": "A",
        "candidate_sha": CANDIDATE,
        "candidate_image_digest": f"sha256:{SHA}",
        "candidate_revision_sha256": "1" * 64,
        "candidate_doctor_job_sha256": "2" * 64,
        "candidate_package_version": "0.8.0",
        "previous_source_sha": "",
        "previous_image_digest": "",
        "previous_revision_sha256": "",
        "rollback_doctor_job_sha256": "",
        "previous_package_version": "",
        "schema_facts": _expected_schema_facts("A"),
        "forward_compatible": True,
        "backward_compatible": False,
        "rollback_permitted": False,
        "decision": "approved",
        "approver_identity": "database-operator",
        "countersigner_identity": "release-operator",
        "approved_at": "2026-09-05T09:00:00Z",
        "countersigned_at": "2026-09-05T09:30:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
    }
    path = _protected(tmp_path / "record.json", record)
    bindings = [
        "--acceptance-run-id",
        "run-1",
        "--candidate-image-digest",
        f"sha256:{SHA}",
        "--candidate-revision-sha256",
        "1" * 64,
        "--candidate-doctor-job-sha256",
        "2" * 64,
    ]
    assert facade.main(["compatibility-record-validate", *BINDING_ARGS, "--record", path, *bindings]) == 0
    assert capsys.readouterr().out.startswith("ELSPETH_ACCEPTANCE_RECEIPT_V1:")
    assert facade.main(["compatibility-record-gate", "--record", path, "--scenario-id", "A"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "gate": "compatibility-record-gate",
        "scenario_id": "A",
        "passed": True,
        "failed_clauses": [],
    }
    assert facade.main(["compatibility-record-gate", "--record", path, "--scenario-id", "B"]) == 1
    assert json.loads(capsys.readouterr().out)["failed_clauses"] == ["previous_landscape_epoch"]
    assert (
        facade.main(["compatibility-record-validate", *BINDING_ARGS, "--record", path, "--acceptance-run-id", "run-2", *bindings[2:]]) == 1
    )
    assert json.loads(capsys.readouterr().err)["check"] == "compatibility_record_binding"


def test_receipt_store_and_bundle_validate_round_trip_through_the_facade(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = tmp_path / "receipts"
    receipt = _protected(tmp_path / "receipt.json", _envelope("replica-run-start", VALID["replica-run-start"]()))
    assert (
        facade.main(
            [
                "receipt-store",
                "--store-dir",
                str(store),
                "--kind",
                "replica-run-start",
                "--subject-id",
                BINDING.subject,
                "--candidate-sha",
                CANDIDATE,
                "--receipt-file",
                receipt,
            ]
        )
        == 0
    )
    receipt_sha256 = capsys.readouterr().out.strip()
    assert (store / f"{receipt_sha256}.json").exists()
    assert facade.main(["bundle-validate", "--store-dir", str(store), "--candidate-sha", CANDIDATE]) == 1
    verdict = json.loads(capsys.readouterr().out)
    assert (
        verdict["passed"] is False
        and "verify-doctor-job" in verdict["missing_kinds"]
        and verdict["testcontainer_reason"] == "testcontainer_run_missing"
    )


def test_replica_probes_from_observation_documents_record_p3_and_p4(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    takeover = _protected(
        tmp_path / "p3.json",
        {
            "primitive": "role_revocation",
            "owner_instance_id": "postgresql-a",
            "survivor_instance_id": "postgresql-b",
            "owner_row": {"instance_id": "postgresql-a", "state": "stopped", "lease_expires_at": "2026-09-05T10:00:30Z"},
            "before_expiry": {
                "addressed_to": "b",
                "status": 409,
                "instance_id": "postgresql-b",
                "body": {"detail": "Session operation is already active"},
            },
            "after_expiry": {"addressed_to": "b", "status": 200, "instance_id": "postgresql-b", "body": {}},
            "takeover_observed_at": "2026-09-05T10:01:05Z",
            "cancelled_run_reason": "Orphaned by periodic cleanup — no active executor thread",
            "fence_owner_after": "postgresql-b",
            "duplicate_sink_effects": 0,
        },
    )
    assert facade.main(["replica-probes", *BINDING_ARGS, "--probe", "lease-takeover", "--observation", takeover]) == 1, (
        "graceful_stop is recorded, never passed"
    )
    line = capsys.readouterr().out
    assert line.startswith("ELSPETH_ACCEPTANCE_RECEIPT_V1:")
    progress = _protected(
        tmp_path / "p4.json",
        {
            "owner_instance_id": "postgresql-a",
            "reader_instance_id": "postgresql-b",
            "poll_interval_seconds": 2.0,
            "status_visible_after_seconds": 0.4,
            "outputs_visible_after_seconds": 0.9,
            "messages_visible_after_seconds": 1.1,
            "blob_sha256_via_owner": SHA,
            "blob_sha256_via_reader": SHA,
            "terminal_status_on_reader": "completed",
        },
    )
    assert facade.main(["replica-probes", *BINDING_ARGS, "--probe", "progress", "--observation", progress]) == 0
    assert capsys.readouterr().out.startswith("ELSPETH_ACCEPTANCE_RECEIPT_V1:")
    assert facade.main(["replica-probes", *BINDING_ARGS, "--probe", "fence-conflict"]) == 1
    assert json.loads(capsys.readouterr().err)["error_code"] == "input_invalid"


def test_module_help_remains_executable_in_an_explicit_environment(tmp_path: Path) -> None:
    source_roots = os.pathsep.join(str(REPO_ROOT / part) for part in ("src", "elspeth-lints/src"))
    completed = subprocess.run(
        [sys.executable, "-m", "elspeth.web.azure_container_apps_acceptance", "--help"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env={"PYTHONPATH": source_roots, "PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert completed.returncode == 0, completed.stderr
    assert "replica-probes" in completed.stdout
