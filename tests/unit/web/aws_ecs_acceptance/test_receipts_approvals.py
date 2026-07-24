"""Receipt persistence and runtime approval owner tests."""

from __future__ import annotations

import base64
import hashlib
import json
import multiprocessing
import os
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty
from typing import Any

import pytest

from elspeth.web import aws_ecs_acceptance as acceptance
from elspeth.web._aws_ecs_acceptance import approvals as approvals_owner
from elspeth.web._aws_ecs_acceptance import receipt_store as receipt_store_owner
from elspeth.web._aws_ecs_acceptance import secure_documents as secure_documents_owner
from tests.unit.web.aws_ecs_acceptance.test_manifest_schema_inventory import (
    _init_control_manifest,
    _terraform_receipt,
)


def _s3_receipt_details() -> dict[str, object]:
    return {
        "object_count": 1,
        "source_sha256": "a" * 64,
        "sink_sha256": "a" * 64,
        "collision_rejected": True,
        "cleanup_succeeded": True,
    }


def _plugin_policy_receipt(*, include_landscape: bool = True) -> dict[str, object]:
    receipt: dict[str, object] = {
        "policy_hash": "1" * 64,
        "snapshot_hash": "2" * 64,
        "binding_sha256": "3" * 64,
        "tutorial_profile_ready": True,
        "tutorial_ready": False,
        "tutorial_blocker": "tutorial_required_control_coverage",
        "tutorial_profile_alias": "tutorial",
        "target_llm": "transform:llm",
        "selected_controls": [
            {
                "capability": "prompt_shield",
                "plugin_id": "transform:aws_bedrock_prompt_shield",
                "profile_alias": "prompt-approved",
                "mode": "required",
            },
            {
                "capability": "content_safety",
                "plugin_id": "transform:aws_bedrock_content_safety",
                "profile_alias": "content-approved",
                "mode": "required",
            },
        ],
    }
    if include_landscape:
        receipt["landscape_evidence"] = True
    return receipt


def _guardrail_receipt_details() -> dict[str, object]:
    return {
        "controls": [
            {
                "plugin_id": "aws_bedrock_prompt_shield",
                "profile_alias": "prompt-approved",
                "guardrail_version": "7",
                "safe_case_passed": True,
                "attack_case_blocked": True,
                "request_ids_present": True,
                "safe_text_sha256": "a" * 64,
                "blocked_text_sha256": "b" * 64,
                "checked_at": "2026-07-14T01:02:03Z",
            },
            {
                "plugin_id": "aws_bedrock_content_safety",
                "profile_alias": "content-approved",
                "guardrail_version": "11",
                "safe_case_passed": True,
                "attack_case_blocked": True,
                "request_ids_present": True,
                "safe_text_sha256": "c" * 64,
                "blocked_text_sha256": "d" * 64,
                "checked_at": "2026-07-14T01:02:03Z",
            },
        ],
        "plugin_policy": _plugin_policy_receipt(),
    }


def _store_receipt_in_process(
    manifest_path: str,
    receipt_path: str,
    scenario_id: str,
    subject_id: str,
    ready_queue: Any,
    start_event: Any,
    result_queue: Any,
) -> None:
    ready_queue.put(scenario_id)
    if not start_event.wait(timeout=10):
        result_queue.put(("error", scenario_id, "start_timeout"))
        return
    try:
        receipt_hash = acceptance.receipt_store(
            Path(manifest_path),
            scenario_id=scenario_id,
            kind="terraform-plan",
            subject_id=subject_id,
            receipt_file=Path(receipt_path),
        )
    except BaseException as exc:
        result_queue.put(("error", scenario_id, type(exc).__name__))
    else:
        result_queue.put(("ok", scenario_id, receipt_hash))


def _store_receipt_paused_after_publication(
    manifest_path: str,
    receipt_path: str,
    scenario_id: str,
    subject_id: str,
    published_event: Any,
    release_event: Any,
    result_queue: Any,
) -> None:
    original_write = receipt_store_owner._write_protected_document

    def pausing_write(path: Path, document: Mapping[str, object], **kwargs: object) -> None:
        original_write(path, document, **kwargs)  # type: ignore[arg-type]
        if kwargs.get("create") is True and path.parent.name == f"{Path(manifest_path).name}.receipts":
            published_event.set()
            if not release_event.wait(timeout=10):
                raise RuntimeError("release_timeout")

    receipt_store_owner._write_protected_document = pausing_write
    try:
        receipt_hash = receipt_store_owner.receipt_store(
            Path(manifest_path),
            scenario_id=scenario_id,
            kind="terraform-plan",
            subject_id=subject_id,
            receipt_file=Path(receipt_path),
        )
    except BaseException as exc:
        result_queue.put(("error", "writer-a", type(exc).__name__))
    else:
        result_queue.put(("ok", "writer-a", receipt_hash))


def _store_receipt_waiter(
    manifest_path: str,
    receipt_path: str,
    scenario_id: str,
    subject_id: str,
    attempt_queue: Any,
    result_queue: Any,
) -> None:
    attempt_queue.put(("writer-b", os.getpid()))
    try:
        receipt_hash = receipt_store_owner.receipt_store(
            Path(manifest_path),
            scenario_id=scenario_id,
            kind="terraform-plan",
            subject_id=subject_id,
            receipt_file=Path(receipt_path),
        )
    except BaseException as exc:
        result_queue.put(("error", "writer-b", type(exc).__name__))
    else:
        result_queue.put(("ok", "writer-b", receipt_hash))


def _read_receipts_cooperatively(
    manifest_path: str,
    attempt_queue: Any,
    result_queue: Any,
) -> None:
    path = Path(manifest_path)
    attempt_queue.put(("reader", os.getpid()))
    try:
        with secure_documents_owner._receipt_manifest_write_lock(path, check="receipt_store_write"):
            manifest = json.loads(path.read_text())
            receipt_directory = path.parent / f"{path.name}.receipts"
            published = {candidate.stem for candidate in receipt_directory.glob("*.json")}
            indexed = {receipt["receipt_sha256"] for receipt in manifest["evidence"]["receipts"] if isinstance(receipt, dict)}
    except BaseException as exc:
        result_queue.put(("error", "reader", type(exc).__name__))
    else:
        result_queue.put(("snapshot", "reader", indexed, published))


def test_receipt_and_approval_public_functions_are_facade_owner_identities() -> None:
    assert acceptance.receipt_store is receipt_store_owner.receipt_store
    assert acceptance.approval_verify is approvals_owner.approval_verify
    assert acceptance.approval_require_current is approvals_owner.approval_require_current
    assert acceptance._require_current_approval is approvals_owner._require_current_approval


def test_receipt_store_serializes_processes_and_preserves_both_updates(tmp_path: Path) -> None:
    pytest.importorskip("fcntl")
    proc_locks = Path("/proc/locks")
    if not proc_locks.exists():
        pytest.skip("requires Linux /proc/locks waiter visibility")
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path, bind_resolved=False, prepare_apply_evidence=False)
    receipt_a_path = tmp_path / "terraform-receipt-a.json"
    receipt_b_path = tmp_path / "terraform-receipt-b.json"
    receipt_a_path.write_text(json.dumps(_terraform_receipt(deletes=0)))
    receipt_b_path.write_text(json.dumps(_terraform_receipt(deletes=1)))
    os.chmod(receipt_a_path, 0o600)
    os.chmod(receipt_b_path, 0o600)
    lock_path = manifest_path.with_name(f".{manifest_path.name}.lock")

    context = multiprocessing.get_context("spawn")
    attempt_queue = context.Queue()
    result_queue = context.Queue()
    published_event = context.Event()
    release_event = context.Event()
    writer_a = context.Process(
        target=_store_receipt_paused_after_publication,
        args=(
            str(manifest_path),
            str(receipt_a_path),
            "A",
            "a" * 64,
            published_event,
            release_event,
            result_queue,
        ),
    )
    writer_b = context.Process(
        target=_store_receipt_waiter,
        args=(
            str(manifest_path),
            str(receipt_b_path),
            "B",
            "b" * 64,
            attempt_queue,
            result_queue,
        ),
    )
    reader = context.Process(
        target=_read_receipts_cooperatively,
        args=(str(manifest_path), attempt_queue, result_queue),
    )
    processes = [writer_a, writer_b, reader]
    try:
        writer_a.start()
        assert published_event.wait(timeout=20)
        assert lock_path.exists()
        writer_b.start()
        reader.start()
        attempts = {attempt_queue.get(timeout=20) for _ in range(2)}
        waiting_process_ids = {pid for _role, pid in attempts}
        assert {role for role, _pid in attempts} == {"writer-b", "reader"}
        lock_inode = lock_path.stat().st_ino
        deadline = time.monotonic() + 10
        observed_waiters: set[int] = set()
        while time.monotonic() < deadline:
            observed_waiters = {
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
            if waiting_process_ids <= observed_waiters:
                break
            if writer_b.exitcode is not None or reader.exitcode is not None:
                break
            time.sleep(0.01)
        assert waiting_process_ids <= observed_waiters
        with pytest.raises(Empty):
            result_queue.get_nowait()
    finally:
        release_event.set()
        for process in processes:
            if process.pid is None:
                continue
            process.join(timeout=20)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    for process in processes:
        assert process.exitcode == 0
    outcomes = [result_queue.get(timeout=5) for _ in range(3)]
    snapshots = [outcome for outcome in outcomes if outcome[0] == "snapshot"]
    assert len(snapshots) == 1
    assert snapshots[0][2] == snapshots[0][3]
    assert {(outcome[0], outcome[1]) for outcome in outcomes if outcome[0] != "snapshot"} == {
        ("ok", "writer-a"),
        ("ok", "writer-b"),
    }
    manifest = json.loads(manifest_path.read_text())
    indexed = {receipt["receipt_sha256"] for receipt in manifest["evidence"]["receipts"]}
    published = {candidate.stem for candidate in (tmp_path / "control.json.receipts").glob("*.json")}
    assert len(indexed) == 2
    assert indexed == published


def test_receipt_store_creates_lock_with_exact_mode_under_restrictive_umask(tmp_path: Path) -> None:
    pytest.importorskip("fcntl")
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path, bind_resolved=False, prepare_apply_evidence=False)
    receipt_path = tmp_path / "terraform-receipt.json"
    receipt_path.write_text(json.dumps(_terraform_receipt()))
    os.chmod(receipt_path, 0o600)

    previous_umask = os.umask(0o777)
    try:
        with secure_documents_owner._receipt_manifest_write_lock(manifest_path, check="receipt_store_write"):
            pass
    finally:
        os.umask(previous_umask)

    lock_path = manifest_path.with_name(f".{manifest_path.name}.lock")
    assert lock_path.stat().st_mode & 0o777 == 0o600
    acceptance.receipt_store(
        manifest_path,
        scenario_id="A",
        kind="terraform-plan",
        subject_id="a" * 64,
        receipt_file=receipt_path,
    )


def test_receipt_store_persists_only_canonical_sanitized_content_and_checkpoints_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path, bind_resolved=False, prepare_apply_evidence=False)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(_terraform_receipt()))
    os.chmod(receipt_path, 0o600)

    receipt_hash = acceptance.receipt_store(
        manifest_path,
        scenario_id="A",
        kind="terraform-plan",
        subject_id="d" * 64,
        receipt_file=receipt_path,
        now=lambda: datetime(2026, 7, 14, 1, 5, tzinfo=UTC),
    )
    assert len(receipt_hash) == 64
    stored = manifest_path.parent / f"{manifest_path.name}.receipts" / f"{receipt_hash}.json"
    assert stored.stat().st_mode & 0o777 == 0o600
    assert "d" * 64 not in manifest_path.read_text()
    evidence = json.loads(manifest_path.read_text())["evidence"]
    assert evidence["receipts"] == [
        {
            "scenario_id": "A",
            "kind": "terraform-plan",
            "subject_sha256": hashlib.sha256(("d" * 64).encode()).hexdigest(),
            "receipt_sha256": receipt_hash,
            "stored_at": "2026-07-14T01:05:00Z",
        }
    ]
    assert (
        acceptance.receipt_store(
            manifest_path,
            scenario_id="A",
            kind="terraform-plan",
            subject_id="d" * 64,
            receipt_file=receipt_path,
            now=lambda: datetime(2026, 7, 14, 1, 6, tzinfo=UTC),
        )
        == receipt_hash
    )
    assert len(json.loads(manifest_path.read_text())["evidence"]["receipts"]) == 1


def test_receipt_store_accepts_bootstrap_terraform_but_rejects_application_receipts(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path, bind_resolved=False, prepare_apply_evidence=False)
    receipt_path = tmp_path / "bootstrap-plan.json"
    receipt_path.write_text(json.dumps(_terraform_receipt()))
    os.chmod(receipt_path, 0o600)

    receipt_hash = acceptance.receipt_store(
        manifest_path,
        scenario_id="bootstrap",
        kind="terraform-plan",
        subject_id="a" * 64,
        receipt_file=receipt_path,
    )

    assert len(receipt_hash) == 64
    with pytest.raises(acceptance.AcceptanceCheckError, match="receipt_store_binding"):
        acceptance.receipt_store(
            manifest_path,
            scenario_id="bootstrap",
            kind="verify-s3",
            subject_id="task",
            receipt_bytes=json.dumps(_s3_receipt_details()).encode(),
        )

    approval_path = tmp_path / "bootstrap-approval.json"
    approval_path.write_text(
        json.dumps(
            {
                "schema": "elspeth.aws-ecs-approval.v1",
                "acceptance_run_id": "4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
                "scenario_id": "bootstrap",
                "kind": "terraform-plan",
                "plan_receipt_hash": receipt_hash,
                "approver_identity": "infrastructure-owner",
                "authority": "terraform-apply",
                "decision": "approved",
                "approved_at": "2026-07-14T01:00:00Z",
                "expires_at": "2026-07-14T02:00:00Z",
                "key_id": "owner-key-1",
                "signature": "opaque-signature",
            }
        )
    )
    os.chmod(approval_path, 0o600)
    approval_hash = acceptance.approval_verify(
        manifest_path,
        scenario_id="bootstrap",
        kind="terraform-plan",
        plan_receipt_hash=receipt_hash,
        approval_file=approval_path,
        signature_verifier=lambda _payload, _signature, _key: True,
        now=lambda: datetime(2026, 7, 14, 1, 5, tzinfo=UTC),
    )
    acceptance.approval_require_current(
        manifest_path,
        scenario_id="bootstrap",
        kind="terraform-plan",
        plan_receipt_hash=receipt_hash,
        approval_hash=approval_hash,
        now=lambda: datetime(2026, 7, 14, 1, 6, tzinfo=UTC),
    )


def test_receipt_store_rejects_unprotected_or_raw_secret_shaped_documents(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps({"schema": "elspeth.test.v1", "password": "raw-secret"}))
    os.chmod(receipt_path, 0o600)
    with pytest.raises(acceptance.AcceptanceCheckError, match="receipt_store_schema") as raised:
        acceptance.receipt_store(
            manifest_path,
            scenario_id="A",
            kind="terraform-plan",
            subject_id="a" * 64,
            receipt_file=receipt_path,
        )
    assert "raw-secret" not in str(raised.value)


def test_receipt_store_binds_exec_receipts_and_allows_shared_content_for_distinct_logical_identities(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path, bind_resolved=False, prepare_apply_evidence=False)
    task_arn = "arn:aws:ecs:ap-southeast-2:123456789012:task/cluster/private-task-id"
    env = {
        "ELSPETH_ACCEPTANCE_CANDIDATE_SHA": "c" * 40,
        "ELSPETH_ACCEPTANCE_TASK_ARN": task_arn,
        "ELSPETH_ACCEPTANCE_SCENARIO_ID": "A",
    }
    encoded = acceptance.encode_exec_receipt("verify-s3", _s3_receipt_details(), env)
    receipt = acceptance.extract_exec_receipt(
        encoded,
        expected_candidate_sha="c" * 40,
        expected_task_arn=task_arn,
        expected_scenario_id="A",
        expected_check="verify-s3",
    )
    exec_path = tmp_path / "exec-receipt.json"
    exec_path.write_text(json.dumps(receipt))
    os.chmod(exec_path, 0o600)
    assert (
        len(
            acceptance.receipt_store(
                manifest_path,
                scenario_id="A",
                kind="verify-s3",
                subject_id=task_arn,
                receipt_file=exec_path,
            )
        )
        == 64
    )

    terraform_path = tmp_path / "terraform-receipt.json"
    terraform_path.write_text(json.dumps(_terraform_receipt()))
    os.chmod(terraform_path, 0o600)
    hashes = {
        acceptance.receipt_store(
            manifest_path,
            scenario_id=scenario,
            kind="terraform-noop",
            subject_id="d" * 64,
            receipt_file=terraform_path,
        )
        for scenario in ("A", "B")
    }
    assert len(hashes) == 1
    assert len(json.loads(manifest_path.read_text())["evidence"]["receipts"]) == 3


def test_receipt_store_binds_guardrail_policy_receipt_to_protected_scenario_inventory(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    task_arn = "arn:aws:ecs:ap-southeast-2:123456789012:task/cluster/private-task-id"
    env = {
        "ELSPETH_ACCEPTANCE_CANDIDATE_SHA": "c" * 40,
        "ELSPETH_ACCEPTANCE_TASK_ARN": task_arn,
        "ELSPETH_ACCEPTANCE_SCENARIO_ID": "A",
    }
    details = _guardrail_receipt_details()
    policy = details["plugin_policy"]
    assert isinstance(policy, dict)
    policy["binding_sha256"] = "4" * 64
    encoded = acceptance.encode_exec_receipt("verify-bedrock-guardrails", details, env)
    receipt = acceptance.extract_exec_receipt(
        encoded,
        expected_candidate_sha="c" * 40,
        expected_task_arn=task_arn,
        expected_scenario_id="A",
        expected_check="verify-bedrock-guardrails",
        expected_plugin_policy_binding_sha256="4" * 64,
    )
    receipt_path = tmp_path / "guardrail-receipt.json"
    receipt_path.write_text(json.dumps(receipt))
    os.chmod(receipt_path, 0o600)

    with pytest.raises(acceptance.AcceptanceCheckError, match="receipt_store_binding"):
        acceptance.receipt_store(
            manifest_path,
            scenario_id="A",
            kind="verify-bedrock-guardrails",
            subject_id=task_arn,
            receipt_file=receipt_path,
        )


@pytest.mark.parametrize(
    "document",
    [
        {"schema": "x", "api_key": "secret", "url": "https://user:pass@example.invalid", "payload": "raw"},
        {"schema": "elspeth.aws-ecs-sanitized-evidence.v1", "kind": "terraform-plan", "projection": {"message": "raw"}},
        {"version": 1, "check": "verify-s3", "ok": True, "candidate_sha": "d" * 40},
    ],
)
def test_receipt_store_rejects_open_or_wrongly_bound_receipt_documents(tmp_path: Path, document: dict[str, object]) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(document))
    os.chmod(receipt_path, 0o600)
    with pytest.raises(acceptance.AcceptanceCheckError, match=r"receipt_store_(?:schema|binding)"):
        acceptance.receipt_store(
            manifest_path,
            scenario_id="A",
            kind="terraform-plan",
            subject_id="d" * 64,
            receipt_file=receipt_path,
        )


def test_receipt_store_accepts_closed_event_delivery_canary_receipt(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path)
    receipt = {
        "schema": "elspeth.aws-ecs-event-canary.v1",
        "delivered": True,
        "removed": True,
    }

    receipt_hash = acceptance.receipt_store(
        manifest_path,
        scenario_id="A",
        kind="deployment-event-canary",
        subject_id="a-0123456789abcdef0123-deployments",
        receipt_bytes=json.dumps(receipt).encode(),
    )

    assert receipt_hash == hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_approval_verify_binds_receipt_run_scenario_authority_decision_and_expiry_with_injected_verifier(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path, bind_resolved=False, prepare_apply_evidence=False)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(_terraform_receipt()))
    os.chmod(receipt_path, 0o600)
    receipt_hash = acceptance.receipt_store(
        manifest_path,
        scenario_id="A",
        kind="terraform-plan",
        subject_id="a" * 64,
        receipt_file=receipt_path,
        now=lambda: datetime(2026, 7, 14, 1, 5, tzinfo=UTC),
    )
    approval_path = tmp_path / "approval.json"
    approval = {
        "schema": "elspeth.aws-ecs-approval.v1",
        "acceptance_run_id": "4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
        "scenario_id": "A",
        "kind": "terraform-plan",
        "plan_receipt_hash": receipt_hash,
        "approver_identity": "infrastructure-owner",
        "authority": "terraform-apply",
        "decision": "approved",
        "approved_at": "2026-07-14T01:06:00Z",
        "expires_at": "2026-07-14T02:06:00Z",
        "key_id": "owner-key-1",
        "signature": "opaque-signature",
    }
    approval_path.write_text(json.dumps(approval))
    os.chmod(approval_path, 0o600)
    verified: list[tuple[bytes, str, str]] = []

    def verifier(payload: bytes, signature: str, key_id: str) -> bool:
        verified.append((payload, signature, key_id))
        return True

    approval_hash = acceptance.approval_verify(
        manifest_path,
        scenario_id="A",
        kind="terraform-plan",
        plan_receipt_hash=receipt_hash,
        approval_file=approval_path,
        signature_verifier=verifier,
        now=lambda: datetime(2026, 7, 14, 1, 7, tzinfo=UTC),
    )
    assert len(approval_hash) == 64
    assert verified and b"opaque-signature" not in verified[0][0]
    assert verified[0][1:] == ("opaque-signature", "owner-key-1")
    acceptance.approval_require_current(
        manifest_path,
        scenario_id="A",
        kind="terraform-plan",
        plan_receipt_hash=receipt_hash,
        approval_hash=approval_hash,
        now=lambda: datetime(2026, 7, 14, 1, 7, 5, tzinfo=UTC),
    )
    with pytest.raises(acceptance.AcceptanceCheckError, match="approval_expired"):
        acceptance.approval_require_current(
            manifest_path,
            scenario_id="A",
            kind="terraform-plan",
            plan_receipt_hash=receipt_hash,
            approval_hash=approval_hash,
            now=lambda: datetime(2026, 7, 14, 2, 7, tzinfo=UTC),
        )

    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_update"):
        acceptance.control_manifest_update(
            manifest_path,
            terraform_plan_receipt=f"A:{'a' * 64}:{'f' * 64}:{approval_hash}",
            now=lambda: datetime(2026, 7, 14, 1, 7, 10, tzinfo=UTC),
        )
    plan_binding = f"A:{'a' * 64}:{receipt_hash}:{approval_hash}"
    acceptance.control_manifest_update(
        manifest_path,
        terraform_plan_receipt=plan_binding,
        now=lambda: datetime(2026, 7, 14, 1, 7, 20, tzinfo=UTC),
    )
    with pytest.raises(acceptance.AcceptanceCheckError, match="approval_expired"):
        acceptance.control_manifest_update(
            manifest_path,
            terraform_applied=plan_binding,
            now=lambda: datetime(2026, 7, 14, 2, 7, tzinfo=UTC),
        )
    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_update"):
        acceptance.control_manifest_update(
            manifest_path,
            terraform_applied=f"A:{'b' * 64}:{receipt_hash}:{approval_hash}",
            now=lambda: datetime(2026, 7, 14, 1, 7, 30, tzinfo=UTC),
        )
    acceptance.control_manifest_update(
        manifest_path,
        terraform_applied=plan_binding,
        now=lambda: datetime(2026, 7, 14, 1, 7, 40, tzinfo=UTC),
    )
    noop_path = tmp_path / "noop.json"
    noop_path.write_text(json.dumps(_terraform_receipt()))
    os.chmod(noop_path, 0o600)
    noop_hash = acceptance.receipt_store(
        manifest_path,
        scenario_id="A",
        kind="terraform-noop",
        subject_id="b" * 64,
        receipt_file=noop_path,
        now=lambda: datetime(2026, 7, 14, 1, 7, 50, tzinfo=UTC),
    )
    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_update"):
        acceptance.control_manifest_update(
            manifest_path,
            terraform_noop_receipt=f"A:{'e' * 64}",
            now=lambda: datetime(2026, 7, 14, 1, 8, tzinfo=UTC),
        )
    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_update"):
        acceptance.control_manifest_update(
            manifest_path,
            terraform_noop_receipt=f"A:{'c' * 64}:{noop_hash}",
            now=lambda: datetime(2026, 7, 14, 1, 8, 5, tzinfo=UTC),
        )
    acceptance.control_manifest_update(
        manifest_path,
        terraform_noop_receipt=f"A:{'b' * 64}:{noop_hash}",
        now=lambda: datetime(2026, 7, 14, 1, 8, 10, tzinfo=UTC),
    )

    with pytest.raises(acceptance.AcceptanceCheckError, match="approval_expired"):
        acceptance.approval_verify(
            manifest_path,
            scenario_id="A",
            kind="terraform-plan",
            plan_receipt_hash=receipt_hash,
            approval_file=approval_path,
            signature_verifier=verifier,
            now=lambda: datetime(2026, 7, 14, 2, 7, tzinfo=UTC),
        )


def test_approval_verify_fails_closed_without_configured_signature_verifier(tmp_path: Path) -> None:
    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path, bind_resolved=False, prepare_apply_evidence=False)
    approval_path = tmp_path / "approval.json"
    approval_path.write_text("{}")
    os.chmod(approval_path, 0o600)
    with pytest.raises(acceptance.AcceptanceCheckError, match="approval_verifier"):
        acceptance.approval_verify(
            manifest_path,
            scenario_id="A",
            kind="terraform-plan",
            plan_receipt_hash="a" * 64,
            approval_file=approval_path,
            environ={},
        )


def test_approval_verify_uses_protected_ed25519_keyring_when_no_verifier_is_injected(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    manifest_path = tmp_path / "control.json"
    _init_control_manifest(manifest_path, bind_resolved=False, prepare_apply_evidence=False)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(_terraform_receipt()))
    os.chmod(receipt_path, 0o600)
    receipt_hash = acceptance.receipt_store(
        manifest_path,
        scenario_id="A",
        kind="terraform-plan",
        subject_id="a" * 64,
        receipt_file=receipt_path,
        now=lambda: datetime(2026, 7, 14, 1, 5, tzinfo=UTC),
    )
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    keyring_path = tmp_path / "approval-keyring.json"
    keyring_path.write_text(
        json.dumps(
            {
                "schema": "elspeth.aws-ecs-approval-keyring.v1",
                "keys": {"owner-key-1": base64.urlsafe_b64encode(public_key).decode().rstrip("=")},
            }
        )
    )
    os.chmod(keyring_path, 0o600)
    approval = {
        "schema": "elspeth.aws-ecs-approval.v1",
        "acceptance_run_id": "4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48",
        "scenario_id": "A",
        "kind": "terraform-plan",
        "plan_receipt_hash": receipt_hash,
        "approver_identity": "infrastructure-owner",
        "authority": "terraform-apply",
        "decision": "approved",
        "approved_at": "2026-07-14T01:06:00Z",
        "expires_at": "2026-07-14T02:06:00Z",
        "key_id": "owner-key-1",
    }
    canonical = json.dumps(approval, sort_keys=True, separators=(",", ":")).encode()
    approval["signature"] = base64.urlsafe_b64encode(private_key.sign(canonical)).decode().rstrip("=")
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(json.dumps(approval))
    os.chmod(approval_path, 0o600)

    approval_hash = acceptance.approval_verify(
        manifest_path,
        scenario_id="A",
        kind="terraform-plan",
        plan_receipt_hash=receipt_hash,
        approval_file=approval_path,
        environ={"ELSPETH_ACCEPTANCE_APPROVAL_KEYRING": str(keyring_path)},
        now=lambda: datetime(2026, 7, 14, 1, 7, tzinfo=UTC),
    )

    assert len(approval_hash) == 64
