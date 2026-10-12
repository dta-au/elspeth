"""Deployment admission refuses existing stores while cleanup stays available."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH
from elspeth.web._acceptance_common.errors import AcceptanceCheckError
from elspeth.web._acceptance_common.schema_facts import _CANDIDATE_PACKAGE_VERSION, _expected_schema_facts
from elspeth.web._aws_ecs_acceptance import approvals, capture, control_service, receipt_contracts, receipt_store, secure_documents
from elspeth.web._azure_container_apps_acceptance import receipt_contracts as aca_receipts
from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH
from tests.unit.web.aws_ecs_acceptance.test_manifest_schema_inventory import _init_control_manifest
from tests.unit.web.aws_ecs_acceptance.test_receipt_contracts import _compatibility_receipt
from tests.unit.web.azure_container_apps_acceptance.test_receipt_contracts import BINDINGS, NOW, _record

CORPUS_PATH = Path(__file__).parent / "corpus" / "ecs_receipts.json"
NOW_ECS = datetime(2026, 7, 14, 1, 3, tzinfo=UTC)


def _ecs_record() -> dict[str, object]:
    return {
        "schema": "elspeth.aws-ecs-compatibility-record.v3",
        "record_id": "change-1",
        "acceptance_run_id": "acceptance-run-id",
        "scenario_id": "A",
        "candidate_sha": "c" * 40,
        "candidate_image_digest": "sha256:" + "d" * 64,
        "candidate_task_definition": "arn:aws:ecs:ap-southeast-2:123456789012:task-definition/candidate:1",
        "candidate_doctor_task_definition": "arn:aws:ecs:ap-southeast-2:123456789012:task-definition/doctor:1",
        "candidate_package_version": _CANDIDATE_PACKAGE_VERSION,
        "previous_source_sha": "",
        "previous_image_digest": "",
        "previous_task_definition": "",
        "rollback_doctor_task_definition": "",
        "previous_package_version": "",
        "schema_facts": _expected_schema_facts("A"),
        "new_store_compatible": True,
        "backward_compatible": False,
        "rollback_permitted": False,
        "decision": "approved",
        "approver_identity": "database-operator",
        "countersigner_identity": "release-operator",
        "approved_at": "2026-07-14T01:00:00Z",
        "countersigned_at": "2026-07-14T01:01:00Z",
        "expires_at": "2026-07-14T03:00:00Z",
    }


def _validate_authority_payload(payload: dict[str, object], *, validator: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    if validator == "ecs-stored":
        return receipt_contracts._validate_compatibility_receipt(payload, scenario_id="A", candidate_sha="c" * 40, subject_id="a" * 64)
    if validator == "aca":
        return aca_receipts.validate_compatibility_record(payload, bindings=BINDINGS, now=NOW)
    expected = _ecs_record()
    manifest = {
        "acceptance_run_id": expected["acceptance_run_id"],
        "candidate_sha": expected["candidate_sha"],
        "ecr": {"candidate_digest": expected["candidate_image_digest"]},
    }
    inventory = {
        "values": {
            "CANDIDATE_TASK_DEFINITION": expected["candidate_task_definition"],
            "DOCTOR_TASK_DEFINITION": expected["candidate_doctor_task_definition"],
        }
    }
    monkeypatch.setattr(control_service, "_read_control_manifest", lambda path: manifest)
    monkeypatch.setattr(control_service, "_load_bound_scenario_inventory", lambda *args, **kwargs: inventory)
    path = tmp_path / "record.json"
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    return control_service.validate_compatibility_record(
        path, manifest_path=tmp_path / "control.json", scenario_id="A", now=lambda: NOW_ECS
    )


@pytest.mark.parametrize("validator", ["ecs-producer", "ecs-stored", "aca"])
@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        *((field, value) for field in ("destructive_reset_required", "in_place_upgrade_supported") for value in (0, 1, 0.0, 1.0)),
        *(("candidate.run_web_plugin_policy_present", value) for value in (0, 1, 0.0, 1.0)),
        *((f"candidate.{field}", value) for field in ("session_epoch", "landscape_epoch") for value in (True, False)),
        ("candidate.session_epoch", float(SESSION_SCHEMA_EPOCH)),
        ("candidate.landscape_epoch", float(SQLITE_SCHEMA_EPOCH)),
        ("extra_top_key", "unreviewed"),
        ("missing_top_key", "store_policy"),
        ("extra_candidate_key", "unreviewed"),
        ("missing_candidate_key", "session_epoch"),
    ],
)
def test_compatibility_authorities_reject_non_strict_nested_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, validator: str, path: str, replacement: object
) -> None:
    payload = _compatibility_receipt("A") if validator == "ecs-stored" else (_record() if validator == "aca" else _ecs_record())
    facts = payload["schema_facts"]
    assert isinstance(facts, dict)
    candidate = facts["candidate"]
    assert isinstance(candidate, dict)
    if path == "extra_top_key":
        facts["unreviewed"] = replacement
    elif path == "missing_top_key":
        facts.pop("store_policy")
    elif path == "extra_candidate_key":
        candidate["unreviewed"] = replacement
    elif path == "missing_candidate_key":
        candidate.pop("session_epoch")
    elif path.startswith("candidate."):
        candidate[path.split(".")[1]] = replacement
    else:
        facts[path] = replacement

    with pytest.raises(AcceptanceCheckError, match=r"(?:receipt_store|compatibility_record)_(?:schema|binding)"):
        _validate_authority_payload(payload, validator=validator, tmp_path=tmp_path, monkeypatch=monkeypatch)


@pytest.mark.parametrize("validator", ["ecs-producer", "ecs-stored", "aca"])
def test_compatibility_authorities_accept_exact_nested_facts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, validator: str) -> None:
    payload = _compatibility_receipt("A") if validator == "ecs-stored" else (_record() if validator == "aca" else _ecs_record())
    _validate_authority_payload(payload, validator=validator, tmp_path=tmp_path, monkeypatch=monkeypatch)


@pytest.mark.parametrize("boundary", ["record", "approval", "store", "provision"])
@pytest.mark.parametrize("guard_enabled", [True, False], ids=["policy-enforced", "guard-removal-negative-control"])
def test_scenario_b_refuses_before_reads_locks_or_provisioning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str, guard_enabled: bool
) -> None:
    """Removing the admission guard must expose the instrumented first side effect."""
    corpus = json.loads(CORPUS_PATH.read_text())
    record_path = tmp_path / "historical-record.json"
    record_path.write_text(json.dumps(corpus["compatibility_records"]["B"]["record"]))
    original = record_path.read_bytes()
    manifest_path = tmp_path / "not-read-control.json"
    calls: list[str] = []

    def forbid_effect(*args: object, **kwargs: object) -> object:
        calls.append(boundary)
        raise AcceptanceCheckError("forbidden_side_effect")

    if boundary == "record":
        monkeypatch.setattr(control_service, "_read_control_manifest", forbid_effect)
        if not guard_enabled:
            monkeypatch.setattr(control_service, "_require_supported_deployment_scenario", lambda scenario: None)
    elif boundary == "approval":
        monkeypatch.setattr(approvals, "_read_control_manifest", forbid_effect)
        if not guard_enabled:
            monkeypatch.setattr(approvals, "_require_supported_deployment_scenario", lambda scenario: None)
    elif boundary == "store":
        monkeypatch.setattr(receipt_store, "_receipt_manifest_write_lock", forbid_effect)
        if not guard_enabled:
            monkeypatch.setattr(receipt_store, "_require_supported_deployment_scenario", lambda scenario: None)
    else:
        monkeypatch.setenv("ELSPETH_ACCEPTANCE_SCENARIO_ID", "B")
        monkeypatch.setattr(capture, "settings_from_env", forbid_effect)
        if not guard_enabled:
            monkeypatch.setattr(capture, "_require_supported_deployment_scenario", lambda scenario: None)

    expected = "compatibility_transition_unsupported" if guard_enabled else "forbidden_side_effect"
    with pytest.raises(AcceptanceCheckError, match=expected):
        if boundary == "record":
            control_service.validate_compatibility_record(record_path, manifest_path=manifest_path, scenario_id="B", now=lambda: NOW_ECS)
        elif boundary == "approval":
            approvals.approval_require_current(
                manifest_path, scenario_id="B", kind="terraform-plan", plan_receipt_hash="a" * 64, approval_hash="b" * 64
            )
        elif boundary == "store":
            receipt_store.receipt_store(
                manifest_path,
                scenario_id="B",
                kind="compatibility-record",
                subject_id="a" * 64,
                receipt_bytes=json.dumps(corpus["stored_receipts"]["compatibility-record:B"]["document"]).encode(),
            )
        else:
            capture.provision_storage()

    assert calls == ([] if guard_enabled else [boundary])
    assert record_path.read_bytes() == original
    assert not manifest_path.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == [record_path.name]


@pytest.mark.parametrize("payload", [None, {}, {"forward_compatible": True}])
def test_stored_scenario_b_is_refused_before_compatibility_fields(payload: object) -> None:
    with pytest.raises(AcceptanceCheckError, match="compatibility_transition_unsupported"):
        receipt_contracts._validate_compatibility_receipt(payload, scenario_id="B", candidate_sha="c" * 40, subject_id="a" * 64)


@pytest.mark.parametrize("provider", ["ecs", "aca"])
@pytest.mark.parametrize("mutation", ["old-schema", "old-forward-field", "false-new-store", "in-place-upgrade"])
def test_new_store_authority_refuses_obsolete_or_false_compatibility_claims(provider: str, mutation: str) -> None:
    payload = _compatibility_receipt("A") if provider == "ecs" else _record()
    if mutation == "old-schema":
        payload["schema"] = (
            "elspeth.aws-ecs-compatibility-receipt.v2" if provider == "ecs" else "elspeth.azure-container-apps-compatibility-receipt.v1"
        )
    elif mutation == "old-forward-field":
        payload["forward_compatible"] = payload.pop("new_store_compatible")
    elif mutation == "false-new-store":
        payload["new_store_compatible"] = False
    else:
        facts = payload["schema_facts"]
        assert isinstance(facts, dict)
        facts["in_place_upgrade_supported"] = True

    with pytest.raises(AcceptanceCheckError, match=r"(?:receipt_store|compatibility_record)_(?:schema|binding)"):
        if provider == "ecs":
            receipt_contracts._validate_compatibility_receipt(payload, scenario_id="A", candidate_sha="c" * 40, subject_id="a" * 64)
        else:
            aca_receipts.validate_compatibility_record(payload, bindings=BINDINGS, now=NOW)


@pytest.mark.parametrize("scenario_id", ["A", "B"])
def test_destroy_approval_remains_usable_for_each_scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario_id: str) -> None:
    document = {"schema": "elspeth.aws-ecs-approval.v1", "scenario_id": scenario_id, "kind": "terraform-destroy-plan"}
    approval_path = tmp_path / "destroy-approval.json"
    approval_path.write_text(json.dumps(document))
    approval_path.chmod(0o600)
    approval_sha = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    manifest = {
        "evidence": {
            "approvals": [
                {
                    "scenario_id": scenario_id,
                    "kind": "terraform-destroy-plan",
                    "plan_receipt_sha256": "a" * 64,
                    "approval_sha256": approval_sha,
                    "approval_path": str(approval_path),
                    "expires_at": "2026-07-14T03:00:00Z",
                }
            ]
        }
    }
    monkeypatch.setattr(approvals, "_read_control_manifest", lambda path: manifest)
    approvals.approval_require_current(
        tmp_path / "control.json",
        scenario_id=scenario_id,
        kind="terraform-destroy-plan",
        plan_receipt_hash="a" * 64,
        approval_hash=approval_sha,
        now=lambda: NOW_ECS,
    )


def test_scenario_b_read_only_inventory_remains_available_for_cleanup(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    original = manifest_path.read_bytes()
    assignments = control_service.scenario_load(manifest_path, scenario_id="B", now=lambda: NOW_ECS)
    assert "ACTIVE_SCENARIO_ID=B\n" in assignments
    assert "PREVIOUS_TASK_DEFINITION=" in assignments
    assert manifest_path.read_bytes() == original


@pytest.mark.parametrize("scenario_id", [None, "", "unknown", " A ", " ", "B"])
@pytest.mark.parametrize("guard_enabled", [True, False], ids=["policy-enforced", "guard-removal-negative-control"])
def test_provision_storage_requires_explicit_a_before_settings_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario_id: str | None, guard_enabled: bool
) -> None:
    if scenario_id is None:
        monkeypatch.delenv("ELSPETH_ACCEPTANCE_SCENARIO_ID", raising=False)
    else:
        monkeypatch.setenv("ELSPETH_ACCEPTANCE_SCENARIO_ID", scenario_id)
    calls: list[str] = []

    def forbid_settings() -> object:
        calls.append("settings")
        raise AcceptanceCheckError("forbidden_side_effect")

    monkeypatch.setattr(capture, "settings_from_env", forbid_settings)
    if not guard_enabled:
        monkeypatch.setattr(capture, "_require_supported_deployment_scenario", lambda scenario: None)
    # An absent external input is refused independently of the value guard.
    # Removing that guard exposes the settings sentinel only for present inputs.
    admission_refuses = guard_enabled or scenario_id is None
    expected = "compatibility_transition_unsupported" if admission_refuses else "forbidden_side_effect"
    with pytest.raises(AcceptanceCheckError, match=expected):
        capture.provision_storage()
    assert calls == ([] if admission_refuses else ["settings"])
    assert list(tmp_path.iterdir()) == []


def test_provision_storage_explicit_a_reaches_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_ACCEPTANCE_SCENARIO_ID", "A")

    def settings() -> object:
        raise AcceptanceCheckError("settings_reached")

    monkeypatch.setattr(capture, "settings_from_env", settings)
    with pytest.raises(AcceptanceCheckError, match="settings_reached"):
        capture.provision_storage()


@pytest.mark.parametrize("operation", ["terraform_plan_receipt", "terraform_applied", "terraform_noop_receipt"])
@pytest.mark.parametrize("guard_enabled", [True, False], ids=["policy-enforced", "guard-removal-negative-control"])
def test_scenario_b_manifest_activation_refuses_before_lock_or_manifest_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, guard_enabled: bool
) -> None:
    calls: list[str] = []

    def forbid_lock(*args: object, **kwargs: object) -> object:
        calls.append("lock")
        raise AcceptanceCheckError("forbidden_side_effect")

    def forbid_read(*args: object, **kwargs: object) -> object:
        calls.append("manifest_read")
        raise AcceptanceCheckError("forbidden_manifest_read")

    monkeypatch.setattr(secure_documents, "_receipt_manifest_write_lock", forbid_lock)
    monkeypatch.setattr(control_service, "_read_control_manifest", forbid_read)
    if not guard_enabled:
        monkeypatch.setattr(control_service, "_require_supported_deployment_scenario", lambda scenario: None)
    expected = "compatibility_transition_unsupported" if guard_enabled else "forbidden_side_effect"
    with pytest.raises(AcceptanceCheckError, match=expected):
        if operation == "terraform_plan_receipt":
            control_service.control_manifest_update(tmp_path / "control.json", terraform_plan_receipt=f"B:{'a' * 64}:{'b' * 64}:{'c' * 64}")
        elif operation == "terraform_applied":
            control_service.control_manifest_update(tmp_path / "control.json", terraform_applied=f"B:{'a' * 64}:{'b' * 64}:{'c' * 64}")
        else:
            control_service.control_manifest_update(tmp_path / "control.json", terraform_noop_receipt=f"B:{'a' * 64}:{'b' * 64}")
    assert calls == ([] if guard_enabled else ["lock"])
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("guard_enabled", [True, False], ids=["policy-enforced", "guard-removal-negative-control"])
def test_private_current_apply_approval_rejects_b_before_approval_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard_enabled: bool
) -> None:
    approval_path = tmp_path / "approved-legacy-transition.json"
    approval_path.write_text(json.dumps({"scenario_id": "B", "kind": "terraform-plan", "decision": "approved"}))
    original = approval_path.read_bytes()
    records: list[object] = [
        {
            "scenario_id": "B",
            "kind": "terraform-plan",
            "plan_receipt_sha256": "a" * 64,
            "approval_sha256": "b" * 64,
            "expires_at": "2026-07-14T03:00:00Z",
            "approval_path": str(approval_path),
        }
    ]
    calls: list[str] = []

    def forbid_read(*args: object, **kwargs: object) -> object:
        calls.append("approval_read")
        raise AcceptanceCheckError("forbidden_side_effect")

    monkeypatch.setattr(approvals, "_read_protected_document", forbid_read)
    if not guard_enabled:
        monkeypatch.setattr(approvals, "_require_supported_deployment_scenario", lambda scenario: None)
    expected = "compatibility_transition_unsupported" if guard_enabled else "forbidden_side_effect"
    with pytest.raises(AcceptanceCheckError, match=expected):
        approvals._require_current_approval(
            records,
            scenario_id="B",
            kind="terraform-plan",
            plan_receipt_sha256="a" * 64,
            approval_sha256="b" * 64,
            current=NOW_ECS,
        )
    assert calls == ([] if guard_enabled else ["approval_read"])
    assert approval_path.read_bytes() == original
