"""Owner tests for evidence projection and the serialized gate ledger."""

from __future__ import annotations

import hashlib
import importlib
import json
import multiprocessing
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty
from typing import Any

import pytest

from elspeth.web import aws_ecs_acceptance as acceptance
from tests.unit.web.aws_ecs_acceptance.test_manifest_schema_inventory import (
    _init_control_manifest,
    _terraform_receipt,
)


def test_facade_reexports_evidence_and_gate_ledger_owners_by_identity() -> None:
    facade = importlib.import_module("elspeth.web.aws_ecs_acceptance")
    evidence = importlib.import_module("elspeth.web._aws_ecs_acceptance.evidence")
    gate_ledger = importlib.import_module("elspeth.web._aws_ecs_acceptance.gate_ledger")

    assert facade.sanitize_evidence is evidence.sanitize_evidence
    assert facade.create_evidence_export_receipt is evidence.create_evidence_export_receipt
    assert facade.gate_ledger_init is gate_ledger.gate_ledger_init
    assert facade.gate_ledger_record is gate_ledger.gate_ledger_record
    assert facade.gate_ledger_record_cleanup is gate_ledger.gate_ledger_record_cleanup
    assert facade.gate_ledger_finalize is gate_ledger.gate_ledger_finalize


def test_sanitize_evidence_projects_logs_task_definitions_and_terraform_without_free_form_content() -> None:
    secret = "credential://user:password@provider.invalid/raw-request-id"
    logs = acceptance.sanitize_evidence(
        "web-log",
        {
            "events": [
                {
                    "timestamp": 1234,
                    "message": json.dumps(
                        {
                            "event_name": "startup_complete",
                            "severity": "info",
                            "ok": True,
                            "message": secret,
                            "url": secret,
                        }
                    ),
                }
            ],
            "nextToken": secret,
        },
    )
    assert logs == {
        "schema": "elspeth.aws-ecs-sanitized-evidence.v1",
        "kind": "web-log",
        "records": [{"timestamp": 1234, "event_name": "startup_complete", "severity": "info", "ok": True}],
        "counts": {"input": 1, "projected": 1},
    }

    task_definition = acceptance.sanitize_evidence(
        "task-definition",
        {
            "taskDefinition": {
                "taskDefinitionArn": secret,
                "revision": 17,
                "networkMode": "awsvpc",
                "containerDefinitions": [{"environment": [{"value": secret}]}, {}],
                "volumes": [{}],
                "requiresCompatibilities": ["FARGATE"],
            }
        },
    )
    assert task_definition["projection"] == {
        "revision": 17,
        "network_mode": "awsvpc",
        "container_count": 2,
        "volume_count": 1,
        "fargate_required": True,
    }

    terraform = acceptance.sanitize_evidence(
        "terraform-plan",
        {
            "resource_changes": [
                {"address": secret, "change": {"actions": ["create"]}},
                {"address": secret, "change": {"actions": ["delete", "create"]}},
                {"address": secret, "change": {"actions": ["no-op"]}},
            ],
            "planned_values": {"root_module": {"resources": [{"values": {"password": secret}}]}},
        },
        plan_sha256="a" * 64,
    )
    assert terraform["schema"] == "elspeth.aws-ecs-sanitized-evidence.v2"
    assert terraform["plan_sha256"] == "a" * 64
    assert terraform["projection"] == {
        "resource_change_count": 3,
        "create_count": 1,
        "update_count": 0,
        "delete_count": 0,
        "replace_count": 1,
        "no_op_count": 1,
        "has_delete": False,
        "has_replace": True,
    }
    assert secret not in json.dumps([logs, task_definition, terraform])


@pytest.mark.parametrize("plan_sha256", [None, "A" * 64, "a" * 63])
def test_sanitize_terraform_evidence_requires_exact_lowercase_plan_sha(plan_sha256: str | None) -> None:
    with pytest.raises(acceptance.AcceptanceCheckError, match="sanitize_evidence_schema"):
        acceptance.sanitize_evidence("terraform-plan", {"resource_changes": []}, plan_sha256=plan_sha256)


def test_sanitize_non_terraform_evidence_rejects_plan_sha() -> None:
    with pytest.raises(acceptance.AcceptanceCheckError, match="sanitize_evidence_schema"):
        acceptance.sanitize_evidence("web-log", {"events": []}, plan_sha256="a" * 64)


@pytest.mark.parametrize("kind", sorted(acceptance.EVIDENCE_KINDS))
def test_sanitize_evidence_rejects_malformed_top_level_for_every_kind(kind: str) -> None:
    with pytest.raises(acceptance.AcceptanceCheckError, match="sanitize_evidence_schema"):
        acceptance.sanitize_evidence(kind, ["raw-provider-response"])


def _bind_gate_ledger_candidate(ledger_path: Path) -> None:
    ledger = json.loads(ledger_path.read_text())
    if ledger["candidate_sha"] is None:
        existing = {record["check_id"] for record in ledger["records"]}
        for check_id in acceptance._TASK1_GATE_CHECK_ORDER:
            if check_id in existing:
                continue
            acceptance.gate_ledger_record(
                ledger_path,
                check_id=check_id,
                exit_status=0,
                receipt_hash=hashlib.sha256(check_id.encode()).hexdigest(),
                candidate_sha="c" * 40,
                now=lambda: datetime(2026, 7, 14, 1, 1, tzinfo=UTC),
            )
        acceptance.gate_ledger_bind_candidate(
            ledger_path,
            candidate_sha="c" * 40,
            now=lambda: datetime(2026, 7, 14, 1, 1, 30, tzinfo=UTC),
        )


def _fill_gate_ledger_prefix(ledger_path: Path) -> None:
    _bind_gate_ledger_candidate(ledger_path)
    existing = {record["check_id"] for record in json.loads(ledger_path.read_text())["records"]}
    for check_id in acceptance._SUCCESS_GATE_CHECK_ORDER:
        if check_id in existing:
            continue
        acceptance.gate_ledger_record(
            ledger_path,
            check_id=check_id,
            exit_status=0,
            receipt_hash=hashlib.sha256(check_id.encode()).hexdigest(),
            candidate_sha="c" * 40,
            now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
        )


def _fill_cleanup_gate_prefix(ledger_path: Path) -> None:
    existing = {record["check_id"] for record in json.loads(ledger_path.read_text())["cleanup_records"]}
    for check_id in acceptance._CLEANUP_GATE_CHECK_ORDER[:-1]:
        if check_id in existing:
            continue
        acceptance.gate_ledger_record_cleanup(
            ledger_path,
            check_id=check_id,
            exit_status=0,
            receipt_hash=hashlib.sha256(check_id.encode()).hexdigest(),
            candidate_sha="c" * 40,
            now=lambda: datetime(2026, 7, 14, 1, 2, 10, tzinfo=UTC),
        )


def _gate_ledger_init(ledger_path: Path) -> dict[str, object]:
    return acceptance.gate_ledger_init(
        ledger_path,
        branch="feat/aws-ecs-program",
        starting_sha="a" * 40,
        plan_sha256="1" * 64,
        program_base_sha="2" * 40,
        reconciled_release_sha="3" * 40,
        now=lambda: datetime(2026, 7, 14, 1, 0, tzinfo=UTC),
    )


def _checkpoint_export_phase(manifest_path: Path, ledger_path: Path, *, final: bool) -> None:
    manifest = json.loads(manifest_path.read_text())
    ledger = json.loads(ledger_path.read_text())
    receipts_sha256 = hashlib.sha256(
        json.dumps(
            {
                "receipts": manifest["evidence"]["receipts"],
                "approvals": manifest["evidence"]["approvals"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    ledger_records_sha256 = acceptance._gate_ledger_records_hash(ledger)
    suffix = "final-export-receipt" if final else "export-receipt"
    receipt_path = manifest_path.with_name(f"{manifest_path.name}.{suffix}.json")
    receipt_path.write_text(
        json.dumps(
            {
                "schema": "elspeth.aws-ecs-evidence-export.v1",
                "acceptance_run_id": manifest["acceptance_run_id"],
                "destination_sha256": manifest["evidence"]["destination_sha256"],
                "receipts_sha256": receipts_sha256,
                "ledger_records_sha256": ledger_records_sha256,
                "artifact_count": 1,
                "exported_at": "2026-07-14T01:02:30Z",
                "verified": True,
            }
        )
    )
    os.chmod(receipt_path, 0o600)
    if final:
        acceptance.control_manifest_update(
            manifest_path,
            final_evidence_export_receipt=str(receipt_path),
            now=lambda: datetime(2026, 7, 14, 1, 2, 31, tzinfo=UTC),
        )
    else:
        acceptance.control_manifest_update(
            manifest_path,
            evidence_export_receipt=str(receipt_path),
            now=lambda: datetime(2026, 7, 14, 1, 2, 30, tzinfo=UTC),
        )


def _checkpoint_evidence_export(manifest_path: Path, ledger_path: Path) -> None:
    _checkpoint_export_phase(manifest_path, ledger_path, final=False)
    _checkpoint_export_phase(manifest_path, ledger_path, final=True)


def test_create_evidence_export_receipt_derives_current_manifest_and_ledger_hashes(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    manifest = _init_control_manifest(manifest_path)
    ledger_path = Path(manifest["gate_ledger_path"])
    _gate_ledger_init(ledger_path)
    _fill_gate_ledger_prefix(ledger_path)
    output_path = tmp_path / "initial-export.json"

    receipt = acceptance.create_evidence_export_receipt(
        manifest_path,
        ledger_path=ledger_path,
        output_path=output_path,
        artifact_count=10,
        now=lambda: datetime(2026, 7, 14, 1, 2, 30, tzinfo=UTC),
    )

    assert receipt["verified"] is True
    assert receipt["artifact_count"] == 10
    assert receipt["acceptance_run_id"] == manifest["acceptance_run_id"]
    assert output_path.stat().st_mode & 0o777 == 0o600
    acceptance.control_manifest_update(
        manifest_path,
        evidence_export_receipt=str(output_path),
        now=lambda: datetime(2026, 7, 14, 1, 2, 31, tzinfo=UTC),
    )


def test_final_evidence_export_refreshes_receipts_created_during_cleanup(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    manifest = _init_control_manifest(manifest_path)
    ledger_path = Path(manifest["gate_ledger_path"])
    _gate_ledger_init(ledger_path)
    _fill_gate_ledger_prefix(ledger_path)
    _checkpoint_export_phase(manifest_path, ledger_path, final=False)
    baseline_evidence = json.loads(manifest_path.read_text())["evidence"]
    baseline_evidence_count = len(baseline_evidence["receipts"]) + len(baseline_evidence["approvals"])

    receipt_path = tmp_path / "destroy-receipt.json"
    receipt_path.write_text(json.dumps(_terraform_receipt(kind="terraform-destroy-plan", deletes=1, plan_sha256="d" * 64)))
    os.chmod(receipt_path, 0o600)
    acceptance.receipt_store(
        manifest_path,
        scenario_id="A",
        kind="terraform-destroy-plan",
        subject_id="d" * 64,
        receipt_file=receipt_path,
    )
    _fill_cleanup_gate_prefix(ledger_path)
    with pytest.raises(acceptance.AcceptanceCheckError, match="cleanup_finalize_export"):
        acceptance.cleanup_evidence_finalize(
            manifest_path,
            ledger_path=ledger_path,
            phase="prepare",
            clear_cleanup_required=False,
        )

    _checkpoint_export_phase(manifest_path, ledger_path, final=True)
    prepared = acceptance.cleanup_evidence_finalize(
        manifest_path,
        ledger_path=ledger_path,
        phase="prepare",
        clear_cleanup_required=False,
    )
    assert prepared["final_evidence"]["receipt_count"] == baseline_evidence_count + 1  # type: ignore[index]


def test_initial_evidence_export_binding_replays_after_cleanup_evidence_advances(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    manifest = _init_control_manifest(manifest_path)
    ledger_path = Path(manifest["gate_ledger_path"])
    _gate_ledger_init(ledger_path)
    _fill_gate_ledger_prefix(ledger_path)
    _checkpoint_export_phase(manifest_path, ledger_path, final=False)
    checkpointed = json.loads(manifest_path.read_text())
    initial_path = checkpointed["evidence"]["export_receipt_path"]
    initial_hash = checkpointed["evidence"]["export_receipt_sha256"]

    receipt_path = tmp_path / "destroy-plan-receipt.json"
    receipt_path.write_text(json.dumps(_terraform_receipt(kind="terraform-destroy-plan", deletes=1, plan_sha256="d" * 64)))
    os.chmod(receipt_path, 0o600)
    acceptance.receipt_store(
        manifest_path,
        scenario_id="A",
        kind="terraform-destroy-plan",
        subject_id="d" * 64,
        receipt_file=receipt_path,
    )

    replayed = acceptance.control_manifest_update(
        manifest_path,
        evidence_export_receipt=initial_path,
        now=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
    )

    assert replayed["evidence"]["export_receipt_path"] == initial_path  # type: ignore[index]
    assert replayed["evidence"]["export_receipt_sha256"] == initial_hash  # type: ignore[index]


def test_final_evidence_export_requires_distinct_path_and_preserves_initial_receipt(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    manifest = _init_control_manifest(manifest_path)
    ledger_path = Path(manifest["gate_ledger_path"])
    _gate_ledger_init(ledger_path)
    _fill_gate_ledger_prefix(ledger_path)
    _checkpoint_export_phase(manifest_path, ledger_path, final=False)
    checkpointed = json.loads(manifest_path.read_text())
    initial_path = Path(checkpointed["evidence"]["export_receipt_path"])

    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_conflict"):
        acceptance.control_manifest_update(
            manifest_path,
            final_evidence_export_receipt=str(initial_path),
            now=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
        )

    overwritten = json.loads(initial_path.read_text())
    overwritten["exported_at"] = "2026-07-14T01:03:10Z"
    initial_path.write_text(json.dumps(overwritten))
    os.chmod(initial_path, 0o600)
    final_path = tmp_path / "distinct-final-export.json"
    final_path.write_text(json.dumps(overwritten))
    os.chmod(final_path, 0o600)
    with pytest.raises(acceptance.AcceptanceCheckError, match="evidence_export_binding"):
        acceptance.control_manifest_update(
            manifest_path,
            final_evidence_export_receipt=str(final_path),
            now=lambda: datetime(2026, 7, 14, 1, 3, 20, tzinfo=UTC),
        )


def test_gate_ledger_records_idempotent_closed_checks_and_finalizes_checksum(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    initialized = _gate_ledger_init(path)
    assert initialized["plan_sha256"] == "1" * 64
    assert initialized["program_base_sha"] == "2" * 40
    assert initialized["reconciled_release_sha"] == "3" * 40
    assert initialized["cleanup_records"] == []
    assert initialized["success_record_count_at_cleanup_start"] is None
    assert acceptance.gate_ledger_get(path, "reconciled_release_sha") == "3" * 40
    assert _gate_ledger_init(path) == initialized
    first = acceptance.gate_ledger_record(
        path,
        check_id="candidate",
        exit_status=0,
        receipt_hash="b" * 64,
        candidate_sha="c" * 40,
        started_at="2026-07-14T01:01:00Z",
        ended_at="2026-07-14T01:01:02Z",
        now=lambda: datetime(2026, 7, 14, 1, 1, 2, tzinfo=UTC),
    )
    resumed = acceptance.gate_ledger_record(
        path,
        check_id="candidate",
        exit_status=0,
        receipt_hash="b" * 64,
        candidate_sha="c" * 40,
        started_at="2026-07-14T01:01:00Z",
        ended_at="2026-07-14T01:01:02Z",
        now=lambda: datetime(2026, 7, 14, 1, 2, tzinfo=UTC),
    )
    assert first == resumed
    assert len(first["records"]) == 1  # type: ignore[arg-type]
    _fill_gate_ledger_prefix(path)
    bound = json.loads(path.read_text())
    assert bound["candidate_sha"] == "c" * 40
    assert bound["candidate_bound_record_count"] == 1
    _fill_cleanup_gate_prefix(path)
    acceptance.gate_ledger_record_cleanup(
        path,
        check_id=acceptance._TERMINAL_GATE_CHECK_ID,
        exit_status=0,
        receipt_hash="e" * 64,
        candidate_sha="c" * 40,
    )

    finalized = acceptance.gate_ledger_finalize(
        path,
        candidate_sha="c" * 40,
        now=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
    )
    final = finalized["finalized"]
    assert isinstance(final, dict)
    assert final["record_count"] == len(acceptance._REQUIRED_GATE_CHECK_IDS)
    assert isinstance(final["records_sha256"], str) and len(final["records_sha256"]) == 64
    rendered = path.read_text()
    assert "expanded command" not in rendered
    assert "raw stdout" not in rendered

    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_finalized"):
        acceptance.gate_ledger_record_cleanup(
            path,
            check_id="cleanup",
            exit_status=0,
            receipt_hash="d" * 64,
            candidate_sha="c" * 40,
        )


def test_gate_ledger_rejects_conflicting_resume_and_invalid_or_secret_shaped_fields(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    _gate_ledger_init(path)
    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_conflict"):
        acceptance.gate_ledger_init(
            path,
            branch="feat/aws-ecs-program",
            starting_sha="a" * 40,
            plan_sha256="1" * 64,
            program_base_sha="2" * 40,
            reconciled_release_sha="4" * 40,
        )
    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_get"):
        acceptance.gate_ledger_get(path, "records")
    acceptance.gate_ledger_record(
        path,
        check_id="candidate",
        exit_status=0,
        receipt_hash="b" * 64,
        candidate_sha="c" * 40,
    )
    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_conflict"):
        acceptance.gate_ledger_record(
            path,
            check_id="candidate",
            exit_status=1,
            receipt_hash="b" * 64,
            candidate_sha="c" * 40,
        )
    _fill_gate_ledger_prefix(path)
    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_schema"):
        acceptance.gate_ledger_record(
            path,
            check_id="cleanup",
            exit_status=0,
            receipt_hash="b" * 64,
            candidate_sha="c" * 40,
        )
    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_schema"):
        acceptance.gate_ledger_record_cleanup(
            path,
            check_id="candidate",
            exit_status=0,
            receipt_hash="b" * 64,
            candidate_sha="c" * 40,
        )
    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_candidate"):
        acceptance.gate_ledger_record_cleanup(
            path,
            check_id="cleanup",
            exit_status=0,
            receipt_hash="b" * 64,
            candidate_sha="d" * 40,
        )
    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_schema"):
        acceptance.gate_ledger_record(
            path,
            check_id="curl https://user:password@example.invalid",
            exit_status=0,
            receipt_hash="b" * 64,
            candidate_sha="c" * 40,
        )

    failed_path = tmp_path / "failed-ledger.json"
    _gate_ledger_init(failed_path)
    acceptance.gate_ledger_record(
        failed_path,
        check_id="candidate",
        exit_status=1,
        receipt_hash="b" * 64,
        candidate_sha="c" * 40,
        now=lambda: datetime(2026, 7, 14, 1, 1, 15, tzinfo=UTC),
    )
    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_failed"):
        _fill_gate_ledger_prefix(failed_path)


def test_gate_ledger_enforces_candidate_bind_and_cleanup_phase_boundaries(tmp_path: Path) -> None:
    unbound_path = tmp_path / "unbound-ledger.json"
    _gate_ledger_init(unbound_path)
    for check_id in acceptance._TASK1_GATE_CHECK_ORDER:
        acceptance.gate_ledger_record(
            unbound_path,
            check_id=check_id,
            exit_status=0,
            receipt_hash=hashlib.sha256(check_id.encode()).hexdigest(),
            candidate_sha="c" * 40,
        )
    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_phase"):
        acceptance.gate_ledger_record(
            unbound_path,
            check_id="static",
            exit_status=0,
            receipt_hash="b" * 64,
            candidate_sha="c" * 40,
        )

    acceptance.gate_ledger_bind_candidate(unbound_path, candidate_sha="c" * 40)
    acceptance.gate_ledger_record(
        unbound_path,
        check_id="static",
        exit_status=0,
        receipt_hash="b" * 64,
        candidate_sha="c" * 40,
    )
    acceptance.gate_ledger_record_cleanup(
        unbound_path,
        check_id="cleanup",
        exit_status=0,
        receipt_hash="d" * 64,
        candidate_sha="c" * 40,
    )
    sealed = json.loads(unbound_path.read_text())
    assert sealed["success_record_count_at_cleanup_start"] == len(sealed["records"])
    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_phase"):
        acceptance.gate_ledger_record(
            unbound_path,
            check_id="tests",
            exit_status=0,
            receipt_hash="e" * 64,
            candidate_sha="c" * 40,
        )

    with pytest.raises(acceptance.AcceptanceCheckError, match="gate_ledger_conflict"):
        _gate_ledger_init(unbound_path)


def _finish_gate_ledger_in_process(
    ledger_path: str,
    operation: str,
    paused_after_read: Any,
    release_reader: Any,
    result_queue: Any,
) -> None:
    gate_ledger = importlib.import_module("elspeth.web._aws_ecs_acceptance.gate_ledger")
    original_read = gate_ledger._read_gate_ledger
    paused = False

    def read_then_pause(path: Path) -> dict[str, object]:
        nonlocal paused
        ledger = original_read(path)
        if operation == "cleanup" and not paused:
            paused = True
            paused_after_read.put("cleanup")
            if not release_reader.wait(timeout=10):
                raise RuntimeError("release_timeout")
        return ledger

    gate_ledger._read_gate_ledger = read_then_pause
    try:
        if operation == "cleanup":
            gate_ledger.gate_ledger_record_cleanup(
                Path(ledger_path),
                check_id="cleanup",
                exit_status=0,
                receipt_hash="e" * 64,
                candidate_sha="c" * 40,
                now=lambda: datetime(2026, 7, 14, 1, 3, tzinfo=UTC),
            )
        elif operation == "finalize":
            gate_ledger.gate_ledger_finalize(
                Path(ledger_path),
                candidate_sha="c" * 40,
                now=lambda: datetime(2026, 7, 14, 1, 4, tzinfo=UTC),
            )
        else:
            raise RuntimeError("unknown_operation")
    except BaseException as exc:
        result_queue.put(("error", operation, type(exc).__name__))
    else:
        result_queue.put(("ok", operation, None))


def test_gate_ledger_cleanup_and_finalize_serialize_from_the_preliminary_read(tmp_path: Path) -> None:
    pytest.importorskip("fcntl")
    proc_locks = Path("/proc/locks")
    if not proc_locks.exists():
        pytest.skip("requires Linux /proc/locks waiter visibility")

    ledger_path = tmp_path / "ledger.json"
    _gate_ledger_init(ledger_path)
    _fill_gate_ledger_prefix(ledger_path)

    context = multiprocessing.get_context("spawn")
    paused_after_read = context.Queue()
    release_reader = context.Event()
    result_queue = context.Queue()
    cleanup = context.Process(
        target=_finish_gate_ledger_in_process,
        args=(str(ledger_path), "cleanup", paused_after_read, release_reader, result_queue),
    )
    finalizer = context.Process(
        target=_finish_gate_ledger_in_process,
        args=(str(ledger_path), "finalize", paused_after_read, release_reader, result_queue),
    )
    processes = (cleanup, finalizer)
    try:
        cleanup.start()
        assert paused_after_read.get(timeout=20) == "cleanup"
        finalizer.start()
        assert finalizer.pid is not None
        lock_path = ledger_path.with_name(f".{ledger_path.name}.lock")
        lock_inode = lock_path.stat().st_ino
        deadline = time.monotonic() + 10
        waiting_process_ids: set[int] = set()
        while time.monotonic() < deadline:
            waiting_process_ids = {
                int(match.group("pid"))
                for line in proc_locks.read_text().splitlines()
                if (
                    match := re.match(
                        r"^\d+:\s+->\s+FLOCK\s+ADVISORY\s+WRITE\s+(?P<pid>\d+)\s+\S+:(?P<inode>\d+)\s+",
                        line,
                    )
                )
                and int(match.group("inode")) == lock_inode
            }
            if finalizer.pid in waiting_process_ids or finalizer.exitcode is not None:
                break
            time.sleep(0.01)
        assert finalizer.pid in waiting_process_ids
        with pytest.raises(Empty):
            result_queue.get_nowait()
    finally:
        release_reader.set()
        for process in processes:
            if process.pid is None:
                continue
            process.join(timeout=20)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert [process.exitcode for process in processes] == [0, 0]
    assert {result_queue.get(timeout=5) for _ in processes} == {
        ("ok", "cleanup", None),
        ("ok", "finalize", None),
    }
    ledger = json.loads(ledger_path.read_text())
    assert [record["check_id"] for record in ledger["cleanup_records"]] == ["cleanup"]
    finalized = ledger["finalized"]
    assert isinstance(finalized, dict)
    assert finalized["records_sha256"] == acceptance._gate_ledger_records_hash(ledger)
