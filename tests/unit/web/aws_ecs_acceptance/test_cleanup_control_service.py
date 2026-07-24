"""Owner tests for cleanup and high-level control services."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH
from elspeth.web import aws_ecs_acceptance as acceptance
from elspeth.web._aws_ecs_acceptance import cleanup, control_service
from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH
from tests.unit.web.aws_ecs_acceptance.test_evidence_gate_ledger import (
    _bind_gate_ledger_candidate,
    _checkpoint_evidence_export,
    _fill_cleanup_gate_prefix,
    _fill_gate_ledger_prefix,
    _gate_ledger_init,
)
from tests.unit.web.aws_ecs_acceptance.test_manifest_schema_inventory import _init_control_manifest


def test_facade_reexports_cleanup_and_control_service_owners_by_identity() -> None:
    assert acceptance.cleanup_evidence_finalize is cleanup.cleanup_evidence_finalize
    for name in (
        "control_manifest_validate",
        "control_manifest_update",
        "control_manifest_load_cleanup",
        "scenario_load",
        "validate_compatibility_record",
    ):
        assert getattr(acceptance, name) is getattr(control_service, name)


def test_compatibility_record_is_bound_to_resolved_scenario_and_stored_by_hash(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag=f"acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline-{'a' * 40}",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )
    acceptance.control_manifest_update(
        manifest_path,
        ecr_baseline_digest="sha256:" + "b" * 64,
        ecr_candidate_digest="sha256:" + "d" * 64,
        now=lambda: datetime(2026, 7, 14, 1, 2, 10, tzinfo=UTC),
    )
    inventory = json.loads((tmp_path / "scenario-b.json").read_text())
    record = {
        "schema": "elspeth.aws-ecs-compatibility-record.v2",
        "record_id": "change-123",
        "acceptance_run_id": inventory["acceptance_run_id"],
        "scenario_id": "B",
        "candidate_sha": inventory["candidate_sha"],
        "candidate_image_digest": "sha256:" + "d" * 64,
        "candidate_task_definition": inventory["values"]["CANDIDATE_TASK_DEFINITION"],
        "candidate_doctor_task_definition": inventory["values"]["DOCTOR_TASK_DEFINITION"],
        "candidate_package_version": "0.7.1",
        "previous_source_sha": "a" * 40,
        "previous_image_digest": "sha256:" + "b" * 64,
        "previous_task_definition": inventory["values"]["PREVIOUS_TASK_DEFINITION"],
        "rollback_doctor_task_definition": inventory["values"]["ROLLBACK_DOCTOR_TASK_DEFINITION"],
        "previous_package_version": "0.7.0",
        "schema_facts": {
            "candidate": {
                "session_epoch": SESSION_SCHEMA_EPOCH,
                "landscape_epoch": SQLITE_SCHEMA_EPOCH,
                "run_web_plugin_policy_present": True,
            },
            "previous": {
                "session_epoch": 27,
                "landscape_epoch": 23,
                "run_web_plugin_policy_present": True,
            },
            "structural_changes": (
                "landscape_epoch_23_to_29_token_ownership_artifact_idempotency_sink_effect_ledger_coalesce_receipts_"
                "per_member_failsink_provenance_output_contract_hash_run_scoped_validation_errors_and_token_ancestry_"
                "batch_expansion_claim_and_sidecar_journal_outbox"
            ),
            "semantics_only_changes": "none",
            "archive_export_decision": "required_before_forward_migration",
            "destructive_reset_required": False,
        },
        "forward_compatible": True,
        "backward_compatible": False,
        "rollback_permitted": False,
        "decision": "approved",
        "approver_identity": "database-operator",
        "countersigner_identity": "release-operator",
        "approved_at": "2026-07-14T01:00:00Z",
        "countersigned_at": "2026-07-14T01:01:00Z",
        "expires_at": "2026-07-14T03:00:00Z",
    }
    record_path = tmp_path / "compatibility-b.json"
    record_path.write_text(json.dumps(record))
    os.chmod(record_path, 0o600)

    receipt = acceptance.validate_compatibility_record(
        record_path,
        manifest_path=manifest_path,
        scenario_id="B",
        now=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
    )
    claimed_safe_rollback = json.loads(json.dumps(record))
    claimed_safe_rollback["backward_compatible"] = True
    claimed_safe_rollback["rollback_permitted"] = True
    record_path.write_text(json.dumps(claimed_safe_rollback))
    with pytest.raises(acceptance.AcceptanceCheckError, match="compatibility_record_binding"):
        acceptance.validate_compatibility_record(
            record_path,
            manifest_path=manifest_path,
            scenario_id="B",
            now=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
        )
    claimed_safe_rollback_receipt = json.loads(json.dumps(receipt))
    claimed_safe_rollback_receipt["backward_compatible"] = True
    claimed_safe_rollback_receipt["rollback_permitted"] = True
    with pytest.raises(acceptance.AcceptanceCheckError, match="receipt_store_schema"):
        acceptance.receipt_store(
            manifest_path,
            scenario_id="B",
            kind="compatibility-record",
            subject_id=receipt["record_sha256"],  # type: ignore[arg-type]
            receipt_bytes=json.dumps(claimed_safe_rollback_receipt).encode(),
            now=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
        )
    for path, replacement in (
        (("candidate_doctor_task_definition",), inventory["values"]["CANDIDATE_TASK_DEFINITION"]),
        (("rollback_doctor_task_definition",), inventory["values"]["PREVIOUS_TASK_DEFINITION"]),
        (("previous_source_sha",), "f" * 40),
        (("previous_image_digest",), "sha256:" + "f" * 64),
        (("previous_package_version",), "0.7.1"),
        (("schema_facts", "candidate", "landscape_epoch"), 22),
        (("schema_facts", "candidate", "session_epoch"), SESSION_SCHEMA_EPOCH - 1),
        (("schema_facts", "previous", "session_epoch"), 26),
        (
            ("schema_facts", "structural_changes"),
            "landscape_epoch_23_to_25_token_ownership_and_artifact_idempotency",
        ),
    ):
        mutated = json.loads(json.dumps(record))
        target = mutated
        for segment in path[:-1]:
            target = target[segment]
        target[path[-1]] = replacement
        record_path.write_text(json.dumps(mutated))
        with pytest.raises(acceptance.AcceptanceCheckError, match="compatibility_record_binding"):
            acceptance.validate_compatibility_record(
                record_path,
                manifest_path=manifest_path,
                scenario_id="B",
                now=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
            )
    record_path.write_text(json.dumps(record))
    receipt_hash = acceptance.receipt_store(
        manifest_path,
        scenario_id="B",
        kind="compatibility-record",
        subject_id=receipt["record_sha256"],  # type: ignore[arg-type]
        receipt_bytes=json.dumps(receipt).encode(),
        now=lambda: datetime(2026, 7, 14, 1, 4, tzinfo=UTC),
    )

    assert len(receipt_hash) == 64
    assert receipt["approvals_present"] is True
    assert receipt["previous_package_version"] == "0.7.0"
    assert "database-operator" not in json.dumps(receipt)


def test_cleanup_evidence_finalize_is_two_phase_refuses_pending_and_clears_only_after_all_surfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "control.json"
    manifest = _init_control_manifest(manifest_path)
    ledger_path = Path(manifest["gate_ledger_path"])
    _gate_ledger_init(ledger_path)
    acceptance.gate_ledger_record(
        ledger_path,
        check_id="candidate",
        exit_status=0,
        receipt_hash="b" * 64,
        candidate_sha="c" * 40,
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )
    _fill_gate_ledger_prefix(ledger_path)
    _fill_cleanup_gate_prefix(ledger_path)
    _checkpoint_evidence_export(manifest_path, ledger_path)
    prepared = acceptance.cleanup_evidence_finalize(
        manifest_path,
        ledger_path=ledger_path,
        phase="prepare",
        clear_cleanup_required=False,
        now=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
    )
    assert prepared["cleanup_required"] is True
    with pytest.raises(acceptance.AcceptanceCheckError, match="cleanup_finalize_pending"):
        acceptance.cleanup_evidence_finalize(
            manifest_path,
            ledger_path=ledger_path,
            phase="commit",
            clear_cleanup_required=True,
            now=lambda: datetime(2026, 7, 14, 1, 4, tzinfo=UTC),
        )

    for surface in acceptance.CLEANUP_SURFACES:
        if surface != "coordinator":
            acceptance.control_manifest_update(
                manifest_path,
                cleanup_checkpoint=f"{surface}:confirmed",
                now=lambda: datetime(2026, 7, 14, 1, 5, tzinfo=UTC),
            )
    original_require_mutable = control_service._require_mutable_control_manifest
    mutation_inside_lock = threading.Event()
    release_mutation = threading.Event()
    finalizer_started = threading.Event()
    finalizer_finished = threading.Event()
    results: dict[str, dict[str, object]] = {}
    errors: list[BaseException] = []

    def pause_late_mutation(manifest: Mapping[str, object]) -> None:
        original_require_mutable(manifest)
        if threading.current_thread().name == "late-manifest-mutator":
            mutation_inside_lock.set()
            if not release_mutation.wait(timeout=5):
                raise AssertionError("timed out waiting to release manifest mutation")

    def mutate_manifest() -> None:
        try:
            results["mutation"] = acceptance.control_manifest_update(
                manifest_path,
                cleanup_checkpoint="coordinator:confirmed",
                now=lambda: datetime(2026, 7, 14, 1, 5, 30, tzinfo=UTC),
            )
        except BaseException as exc:
            errors.append(exc)

    def finalize_manifest() -> None:
        finalizer_started.set()
        try:
            results["finalizer"] = acceptance.cleanup_evidence_finalize(
                manifest_path,
                ledger_path=ledger_path,
                phase="commit",
                clear_cleanup_required=True,
                now=lambda: datetime(2026, 7, 14, 1, 6, tzinfo=UTC),
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            finalizer_finished.set()

    monkeypatch.setattr(control_service, "_require_mutable_control_manifest", pause_late_mutation)
    mutation_thread = threading.Thread(target=mutate_manifest, name="late-manifest-mutator")
    finalizer_thread = threading.Thread(target=finalize_manifest, name="final-receipt-writer")
    mutation_thread.start()
    assert mutation_inside_lock.wait(timeout=5)
    finalizer_thread.start()
    assert finalizer_started.wait(timeout=5)
    assert not finalizer_finished.wait(timeout=0.1)
    release_mutation.set()
    mutation_thread.join(timeout=5)
    finalizer_thread.join(timeout=5)
    assert not mutation_thread.is_alive()
    assert not finalizer_thread.is_alive()
    assert errors == []
    committed = results["finalizer"]
    assert committed["cleanup_required"] is False
    final_receipt = manifest_path.with_name(f"{manifest_path.name}.final-receipt.json")
    committed_manifest_bytes = manifest_path.read_bytes()
    committed_receipt_bytes = final_receipt.read_bytes()
    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_finalized"):
        acceptance.control_manifest_update(
            path=manifest_path,
            cleanup_checkpoint="coordinator:confirmed",
            now=lambda: datetime(2026, 7, 14, 1, 6, 30, tzinfo=UTC),
        )
    assert manifest_path.read_bytes() == committed_manifest_bytes
    assert final_receipt.read_bytes() == committed_receipt_bytes

    evidence = committed["evidence"]
    scenarios = committed["scenarios"]
    assert isinstance(evidence, dict) and isinstance(scenarios, dict)
    scenario_a = scenarios["A"]
    assert isinstance(scenario_a, dict)
    sealed_mutations = (
        lambda: acceptance.control_manifest_bind_retained_evidence(
            manifest_path,
            receipt_path=str(evidence["retained_evidence_path"]),
        ),
        lambda: acceptance.control_manifest_checkpoint_operator_evidence(
            manifest_path,
            exec_receipt_path=str(tmp_path / "unused-exec-receipt.json"),
            checkpoint_path=str(tmp_path / "unused-checkpoint.json"),
        ),
        lambda: acceptance.control_manifest_bind_scenario(
            manifest_path,
            scenario_id="A",
            inventory_path=str(scenario_a["inventory_path"]),
        ),
        lambda: acceptance.receipt_store(
            manifest_path,
            scenario_id="A",
            kind="terraform-plan",
            subject_id="1" * 64,
            receipt_file=tmp_path / "a-plan-receipt.json",
        ),
        lambda: acceptance.approval_verify(
            manifest_path,
            scenario_id="A",
            kind="terraform-plan",
            plan_receipt_hash="1" * 64,
            approval_file=tmp_path / "unused-approval.json",
            signature_verifier=lambda _payload, _signature, _key_id: True,
        ),
    )
    for mutate in sealed_mutations:
        with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_finalized"):
            mutate()
        assert manifest_path.read_bytes() == committed_manifest_bytes
        assert final_receipt.read_bytes() == committed_receipt_bytes

    version_directory = tmp_path / "version-2"
    version_directory.mkdir()
    versioned_manifest_path = version_directory / "control.json"
    _init_control_manifest(
        versioned_manifest_path,
        run_id="4b735e5b-3037-4a3f-938b-69135ef9cd62",
    )
    acceptance.control_manifest_update(
        versioned_manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4b735e5b-3037-4a3f-938b-69135ef9cd62-baseline",
        ecr_candidate_tag="acceptance-4b735e5b-3037-4a3f-938b-69135ef9cd62-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance-v2",
        now=lambda: datetime(2026, 7, 14, 1, 6, 40, tzinfo=UTC),
    )
    assert manifest_path.read_bytes() == committed_manifest_bytes
    assert final_receipt.read_bytes() == committed_receipt_bytes
    assert acceptance.control_manifest_get(versioned_manifest_path, "cleanup_required") == "true"
    assert acceptance.control_manifest_get(manifest_path, "cleanup_states.coordinator") == "confirmed"
    cleanup_ledger = json.loads(ledger_path.read_text())
    assert cleanup_ledger["finalized"] is None
    assert cleanup_ledger["cleanup_records"][-1]["check_id"] == acceptance._TERMINAL_GATE_CHECK_ID
    acceptance.control_manifest_validate(
        manifest_path,
        acceptance_run_id="4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
        candidate_sha="c" * 40,
        cleanup_only=True,
        require_cleanup_cleared=True,
        now=lambda: datetime(2026, 7, 14, 1, 7, tzinfo=UTC),
    )
    acceptance.gate_ledger_finalize(
        ledger_path,
        candidate_sha="c" * 40,
        now=lambda: datetime(2026, 7, 14, 1, 7, 10, tzinfo=UTC),
    )
    acceptance.control_manifest_validate(
        manifest_path,
        cleanup_only=True,
        require_cleanup_cleared=True,
        now=lambda: datetime(2026, 7, 14, 1, 7, 20, tzinfo=UTC),
    )
    committed_bytes = manifest_path.read_bytes()
    assert "CLEANUP_REQUIRED=0" in acceptance.control_manifest_load_cleanup(
        manifest_path,
        now=lambda: datetime(2026, 7, 14, 6, 0, tzinfo=UTC),
    )
    assert manifest_path.read_bytes() == committed_bytes
    acceptance.control_manifest_validate(
        manifest_path,
        cleanup_only=True,
        require_cleanup_cleared=True,
        now=lambda: datetime(2026, 7, 14, 6, 1, tzinfo=UTC),
    )
    assert final_receipt.stat().st_mode & 0o777 == 0o600
    final_payload = json.loads(final_receipt.read_text())
    assert len(final_payload["manifest_sha256"]) == 64
    assert len(final_payload["ledger_sha256"]) == 64
    protected_writes: list[Path] = []
    cleanup_appends: list[str] = []
    original_protected_write = cleanup._write_protected_document
    original_cleanup_append = cleanup.gate_ledger_record_cleanup

    def record_protected_write(path: Path, payload: Mapping[str, object], **kwargs: object) -> None:
        protected_writes.append(path)
        original_protected_write(path, payload, **kwargs)  # type: ignore[arg-type]

    def record_cleanup_append(path: Path, **kwargs: object) -> dict[str, object]:
        cleanup_appends.append(str(kwargs.get("check_id")))
        return original_cleanup_append(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cleanup, "_write_protected_document", record_protected_write)
    monkeypatch.setattr(cleanup, "gate_ledger_record_cleanup", record_cleanup_append)
    final_receipt.unlink()
    with pytest.raises(acceptance.AcceptanceCheckError, match="cleanup_finalize_receipt"):
        acceptance.cleanup_evidence_finalize(
            manifest_path,
            ledger_path=ledger_path,
            phase="commit",
            clear_cleanup_required=True,
            now=lambda: datetime(2026, 7, 14, 1, 8, tzinfo=UTC),
        )
    assert protected_writes == []
    assert cleanup_appends == []
    assert not final_receipt.exists()
    monkeypatch.setattr(cleanup, "_write_protected_document", original_protected_write)
    monkeypatch.setattr(cleanup, "gate_ledger_record_cleanup", original_cleanup_append)
    original_protected_write(final_receipt, final_payload, create=True, exists_check="test", write_check="test")
    final_receipt.write_text(json.dumps({**final_payload, "receipts_sha256": "f" * 64}))
    os.chmod(final_receipt, 0o600)
    with pytest.raises(acceptance.AcceptanceCheckError, match="cleanup_finalize_receipt"):
        acceptance.cleanup_evidence_finalize(
            manifest_path,
            ledger_path=ledger_path,
            phase="commit",
            clear_cleanup_required=True,
            now=lambda: datetime(2026, 7, 14, 1, 9, tzinfo=UTC),
        )


def test_cleanup_evidence_finalize_recovers_after_terminal_row_precedes_manifest_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "control.json"
    manifest = _init_control_manifest(manifest_path)
    ledger_path = Path(manifest["gate_ledger_path"])
    _gate_ledger_init(ledger_path)
    _fill_gate_ledger_prefix(ledger_path)
    _fill_cleanup_gate_prefix(ledger_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )
    _checkpoint_evidence_export(manifest_path, ledger_path)
    acceptance.cleanup_evidence_finalize(
        manifest_path,
        ledger_path=ledger_path,
        phase="prepare",
        clear_cleanup_required=False,
        now=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
    )
    for surface in acceptance.CLEANUP_SURFACES:
        if surface != "coordinator":
            acceptance.control_manifest_update(
                manifest_path,
                cleanup_checkpoint=f"{surface}:confirmed",
                now=lambda: datetime(2026, 7, 14, 1, 4, tzinfo=UTC),
            )

    original_write = cleanup._write_protected_document

    def interrupt_manifest_commit(path: Path, payload: Mapping[str, object], **kwargs: object) -> None:
        if path == manifest_path and payload.get("cleanup_required") is False:
            raise acceptance.AcceptanceCheckError("simulated_manifest_commit_interrupt")
        original_write(path, payload, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cleanup, "_write_protected_document", interrupt_manifest_commit)
    with pytest.raises(acceptance.AcceptanceCheckError, match="simulated_manifest_commit_interrupt"):
        acceptance.cleanup_evidence_finalize(
            manifest_path,
            ledger_path=ledger_path,
            phase="commit",
            clear_cleanup_required=True,
            now=lambda: datetime(2026, 7, 14, 1, 5, tzinfo=UTC),
        )
    monkeypatch.setattr(cleanup, "_write_protected_document", original_write)

    interrupted_manifest = json.loads(manifest_path.read_text())
    interrupted_ledger = json.loads(ledger_path.read_text())
    assert interrupted_manifest["cleanup_required"] is True
    assert interrupted_manifest["final_evidence"]["phase"] == "prepared"
    assert interrupted_ledger["cleanup_records"][-1]["check_id"] == acceptance._TERMINAL_GATE_CHECK_ID

    acceptance.cleanup_evidence_finalize(
        manifest_path,
        ledger_path=ledger_path,
        phase="prepare",
        clear_cleanup_required=False,
        now=lambda: datetime(2026, 7, 14, 1, 6, tzinfo=UTC),
    )
    recovered = acceptance.cleanup_evidence_finalize(
        manifest_path,
        ledger_path=ledger_path,
        phase="commit",
        clear_cleanup_required=True,
        now=lambda: datetime(2026, 7, 14, 1, 7, tzinfo=UTC),
    )
    assert recovered["cleanup_required"] is False
    assert recovered["final_evidence"]["phase"] == "committed"  # type: ignore[index]


@pytest.mark.parametrize("interrupt_after", ["prepared-manifest", "terminal-ledger", "final-receipt", "committed-manifest"])
def test_cleanup_finalize_recovers_after_each_durable_write_and_committed_replay_is_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_after: str,
) -> None:
    manifest_path = tmp_path / "control.json"
    manifest = _init_control_manifest(manifest_path)
    ledger_path = Path(manifest["gate_ledger_path"])
    _gate_ledger_init(ledger_path)
    _fill_gate_ledger_prefix(ledger_path)
    _fill_cleanup_gate_prefix(ledger_path)
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )
    _checkpoint_evidence_export(manifest_path, ledger_path)
    for surface in acceptance.CLEANUP_SURFACES:
        if surface != "coordinator":
            acceptance.control_manifest_update(
                manifest_path,
                cleanup_checkpoint=f"{surface}:confirmed",
                now=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
            )

    final_receipt_path = manifest_path.with_name(f"{manifest_path.name}.final-receipt.json")
    original_write = cleanup._write_protected_document
    original_append = cleanup.gate_ledger_record_cleanup
    interrupted = False

    def protected_write(path: Path, payload: Mapping[str, object], **kwargs: object) -> None:
        nonlocal interrupted
        original_write(path, payload, **kwargs)  # type: ignore[arg-type]
        final_evidence = payload.get("final_evidence")
        prepared_write = path == manifest_path and isinstance(final_evidence, Mapping) and final_evidence.get("phase") == "prepared"
        committed_write = path == manifest_path and payload.get("cleanup_required") is False
        final_receipt_write = path == final_receipt_path
        matches = {
            "prepared-manifest": prepared_write,
            "final-receipt": final_receipt_write,
            "committed-manifest": committed_write,
        }.get(interrupt_after, False)
        if matches and not interrupted:
            interrupted = True
            raise acceptance.AcceptanceCheckError(f"simulated_{interrupt_after}_interrupt")

    def append_terminal(path: Path, **kwargs: object) -> dict[str, object]:
        nonlocal interrupted
        result = original_append(path, **kwargs)  # type: ignore[arg-type]
        if interrupt_after == "terminal-ledger" and not interrupted:
            interrupted = True
            raise acceptance.AcceptanceCheckError("simulated_terminal-ledger_interrupt")
        return result

    if interrupt_after == "prepared-manifest":
        monkeypatch.setattr(cleanup, "_write_protected_document", protected_write)
        with pytest.raises(acceptance.AcceptanceCheckError, match="simulated_prepared-manifest_interrupt"):
            acceptance.cleanup_evidence_finalize(
                manifest_path,
                ledger_path=ledger_path,
                phase="prepare",
                clear_cleanup_required=False,
                now=lambda: datetime(2026, 7, 14, 1, 4, tzinfo=UTC),
            )
        monkeypatch.setattr(cleanup, "_write_protected_document", original_write)
        acceptance.cleanup_evidence_finalize(
            manifest_path,
            ledger_path=ledger_path,
            phase="prepare",
            clear_cleanup_required=False,
            now=lambda: datetime(2026, 7, 14, 1, 5, tzinfo=UTC),
        )
    else:
        acceptance.cleanup_evidence_finalize(
            manifest_path,
            ledger_path=ledger_path,
            phase="prepare",
            clear_cleanup_required=False,
            now=lambda: datetime(2026, 7, 14, 1, 4, tzinfo=UTC),
        )
        monkeypatch.setattr(cleanup, "_write_protected_document", protected_write)
        monkeypatch.setattr(cleanup, "gate_ledger_record_cleanup", append_terminal)
        with pytest.raises(acceptance.AcceptanceCheckError, match=f"simulated_{interrupt_after}_interrupt"):
            acceptance.cleanup_evidence_finalize(
                manifest_path,
                ledger_path=ledger_path,
                phase="commit",
                clear_cleanup_required=True,
                now=lambda: datetime(2026, 7, 14, 1, 6, tzinfo=UTC),
            )
        monkeypatch.setattr(cleanup, "_write_protected_document", original_write)
        monkeypatch.setattr(cleanup, "gate_ledger_record_cleanup", original_append)

    recovered = acceptance.cleanup_evidence_finalize(
        manifest_path,
        ledger_path=ledger_path,
        phase="commit",
        clear_cleanup_required=True,
        now=lambda: datetime(2026, 7, 14, 1, 7, tzinfo=UTC),
    )
    assert interrupted
    assert recovered["cleanup_required"] is False
    assert recovered["final_evidence"]["phase"] == "committed"  # type: ignore[index]
    assert final_receipt_path.exists()

    protected_writes: list[Path] = []
    cleanup_appends: list[str] = []

    def spy_write(path: Path, payload: Mapping[str, object], **kwargs: object) -> None:
        protected_writes.append(path)
        original_write(path, payload, **kwargs)  # type: ignore[arg-type]

    def spy_append(path: Path, **kwargs: object) -> dict[str, object]:
        cleanup_appends.append(str(kwargs.get("check_id")))
        return original_append(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cleanup, "_write_protected_document", spy_write)
    monkeypatch.setattr(cleanup, "gate_ledger_record_cleanup", spy_append)
    replayed = acceptance.cleanup_evidence_finalize(
        manifest_path,
        ledger_path=ledger_path,
        phase="commit",
        clear_cleanup_required=True,
        now=lambda: datetime(2026, 7, 14, 1, 8, tzinfo=UTC),
    )
    assert replayed == recovered
    assert protected_writes == []
    assert cleanup_appends == []


def test_cleanup_evidence_finalize_preserves_failed_deadline_as_a_valid_cleanup_terminal_state(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    manifest = _init_control_manifest(manifest_path, deadline="2026-07-14T02:00:00Z")
    ledger_path = Path(manifest["gate_ledger_path"])
    _gate_ledger_init(ledger_path)
    acceptance.gate_ledger_record(
        ledger_path,
        check_id="candidate",
        exit_status=0,
        receipt_hash="b" * 64,
        candidate_sha="c" * 40,
        now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
    )
    acceptance.control_manifest_update(
        manifest_path,
        cleanup_required=True,
        ecr_baseline_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-baseline",
        ecr_candidate_tag="acceptance-4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48-candidate",
        ecr_registry="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com",
        ecr_repository="elspeth-acceptance",
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )
    _bind_gate_ledger_candidate(ledger_path)
    _fill_cleanup_gate_prefix(ledger_path)
    _checkpoint_evidence_export(manifest_path, ledger_path)
    acceptance.control_manifest_load_cleanup(
        manifest_path,
        now=lambda: datetime(2026, 7, 14, 2, 1, tzinfo=UTC),
    )
    for surface in acceptance.CLEANUP_SURFACES:
        if surface not in {"coordinator", "teardown_deadline"}:
            acceptance.control_manifest_update(
                manifest_path,
                cleanup_checkpoint=f"{surface}:confirmed",
                now=lambda: datetime(2026, 7, 14, 2, 2, tzinfo=UTC),
            )
    acceptance.cleanup_evidence_finalize(
        manifest_path,
        ledger_path=ledger_path,
        phase="prepare",
        clear_cleanup_required=False,
        now=lambda: datetime(2026, 7, 14, 2, 3, tzinfo=UTC),
    )
    committed = acceptance.cleanup_evidence_finalize(
        manifest_path,
        ledger_path=ledger_path,
        phase="commit",
        clear_cleanup_required=True,
        now=lambda: datetime(2026, 7, 14, 2, 4, tzinfo=UTC),
    )

    assert committed["cleanup_states"]["teardown_deadline"] == "failed"  # type: ignore[index]
    assert json.loads(ledger_path.read_text())["finalized"] is None
    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_incomplete"):
        acceptance.gate_ledger_finalize(ledger_path, candidate_sha="c" * 40)
    acceptance.control_manifest_validate(
        manifest_path,
        cleanup_only=True,
        require_cleanup_cleared=True,
        now=lambda: datetime(2026, 7, 14, 2, 5, tzinfo=UTC),
    )
