"""Evidence projections (plan §9.1, ``test_evidence_projection.py``): az / KQL / ARG JSON onto closed detail sets.

Every projection is a Tier-3 boundary: malformed platform documents are
refused with a static check name, a canary token in any row is a leak, nothing
from a response body reaches a detail set. The receipt store and the bundle
check bind the shared ``testcontainer_run_gate`` exactly as ECS ``evidence.py``
binds it: absence refuses, a failed run refuses, two passing runs refuse.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from elspeth.web._acceptance_common.errors import AcceptanceCheckError, AcceptanceInputError
from elspeth.web._acceptance_common.replica_probes import ProbeResult, decide_lease_takeover
from elspeth.web._acceptance_common.testcontainer_run import TESTCONTAINER_RUN_RECEIPT_KIND, TESTCONTAINER_SELECTION
from elspeth.web._azure_container_apps_acceptance import evidence
from elspeth.web._azure_container_apps_acceptance.controller import LabelWeight
from elspeth.web._azure_container_apps_acceptance.evidence import (
    JobExecution,
    blob_managed_identity_details,
    bundle_check,
    connection_budget_details,
    cross_replica_progress_observation,
    doctor_job_details,
    lease_takeover_for_receipt,
    lease_takeover_observation,
    project_active_revisions,
    project_job_execution,
    project_label_weights,
    project_log_analytics_rows,
    project_replica_names,
    project_resource_graph_count,
    read_receipt_index,
    receipt_store,
    resource_graph_cleanup_details,
    revision_rollout_details,
    storage_job_details,
)
from elspeth.web._azure_container_apps_acceptance.receipt_contracts import (
    CHECK_KINDS,
    STORED_RECEIPT_KINDS,
    ReplicaBinding,
    encode_exec_receipt,
)

from .test_receipt_contracts import APP_ID, BINDING, CANDIDATE, SHA, VALID

EXECUTION = JobExecution(name="doctor-runtime-a-iwpi4il", status="Succeeded")
NOW = datetime(2026, 9, 5, 10, 30, tzinfo=UTC)


def _malformed(good: object, *mutations: tuple[tuple[object, ...], object]) -> list[object]:
    """``good`` plus one copy per mutation, each with one path replaced."""

    cases: list[object] = [None, 1, "x", [], {}, [good] if isinstance(good, dict) else {"data": good}]
    for path, value in mutations:
        mutated: Any = copy.deepcopy(good)
        node = mutated
        for step in path[:-1]:
            node = node[step]
        node[path[-1]] = value
        cases.append(mutated)
    return cases


# --------------------------------------------------------------------------- az projections


def test_job_execution_projection_rejects_malformed_documents() -> None:
    good = {"name": "doctor-runtime-a-iwpi4il", "properties": {"status": "Succeeded", "startTime": "2026-09-05T10:00:00Z"}}
    assert project_job_execution(good) == EXECUTION
    for payload in _malformed(
        good,
        (("name",), "Doctor Job"),
        (("name",), None),
        (("properties",), "Succeeded"),
        (("properties", "status"), 1),
        (("properties", "status"), "x" * 33),
    ):
        with pytest.raises(AcceptanceCheckError, match="platform_document_schema"):
            project_job_execution(payload)


def test_replica_list_projection_rejects_malformed_documents() -> None:
    good = [{"name": "elspeth-web--abc-a-1", "properties": {}}, {"name": "elspeth-web--abc-a-2"}]
    assert project_replica_names(good) == ("elspeth-web--abc-a-1", "elspeth-web--abc-a-2")
    for payload in _malformed(good, ((0, "name"), "Replica One"), ((1,), "elspeth-web--abc-a-2"), ((1, "name"), "elspeth-web--abc-a-1")):
        with pytest.raises(AcceptanceCheckError, match="platform_document_schema"):
            project_replica_names(payload)
    with pytest.raises(AcceptanceCheckError, match="platform_document_schema"):
        project_replica_names([{"name": f"r-{index}"} for index in range(101)])


def test_revision_list_projection_rejects_malformed_documents() -> None:
    good = [{"name": "elspeth-web--abc123def456", "traffic": 100, "state": "Running"}]
    assert project_active_revisions(good) == (
        evidence.ActiveRevision(name="elspeth-web--abc123def456", traffic_weight=100, running_state="Running"),
    )
    assert project_active_revisions([]) == (), "no active revision is a valid (and failing) rollout observation"
    for payload in _malformed(
        good, ((0, "traffic"), True), ((0, "traffic"), 101), ((0, "traffic"), "100"), ((0, "state"), "Running!"), ((0, "name"), "Elspeth")
    ):
        if payload == []:
            continue
        with pytest.raises(AcceptanceCheckError, match="platform_document_schema"):
            project_active_revisions(payload)


def test_revision_rollout_details_demand_one_running_candidate_revision_at_full_weight() -> None:
    revisions = project_active_revisions([{"name": "elspeth-web--abc123def456", "traffic": 100, "state": "Running"}])
    details = revision_rollout_details(
        revisions,
        expected_revision="elspeth-web--abc123def456",
        image_digest=f"sha256:{SHA}",
        running_replicas=2,
        health_status=200,
        ready_status=200,
    )
    assert details == VALID["revision-rollout"]()
    for bad in (
        project_active_revisions([]),
        project_active_revisions([{"name": "elspeth-web--other", "traffic": 100, "state": "Running"}]),
        project_active_revisions(
            [
                {"name": "elspeth-web--abc123def456", "traffic": 50, "state": "Running"},
                {"name": "elspeth-web--old", "traffic": 50, "state": "Running"},
            ]
        ),
        project_active_revisions([{"name": "elspeth-web--abc123def456", "traffic": 100, "state": "Degraded"}]),
    ):
        with pytest.raises(AcceptanceCheckError, match="revision_rollout"):
            revision_rollout_details(
                bad,
                expected_revision="elspeth-web--abc123def456",
                image_digest=f"sha256:{SHA}",
                running_replicas=2,
                health_status=200,
                ready_status=200,
            )


def _metrics(count: float = 4.0, points: int = 10, offset_field: str = "maximum") -> dict[str, object]:
    start = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
    return {
        "value": [
            {
                "name": {"value": "active_connections", "localizedValue": "Active Connections"},
                "timeseries": [
                    {"data": [{"timeStamp": (start + timedelta(minutes=i)).isoformat(), offset_field: count + i} for i in range(points)]}
                ],
            }
        ]
    }


def test_active_connections_projection_rejects_malformed_documents() -> None:
    details = connection_budget_details(
        _metrics(),
        window_start=datetime(2026, 9, 5, 10, 0, tzinfo=UTC),
        acceptance_run_id="run",
        server_id="server",
        max_connections=100,
        approved_budget=40,
        safety_margin=20,
    )
    assert details["mechanism"] == "azure_monitor_metrics"
    budget = details["budget"]
    assert budget["schema"] == "elspeth.postgres-flexible-connection-budget.v1"
    assert budget["high_water"] == 13.0 and budget["ok"] is True
    assert [point["timestamp"] for point in budget["points"]][:2] == ["2026-09-05T10:00:00Z", "2026-09-05T10:01:00Z"]
    assert budget["acceptance_run_id_sha256"] == hashlib.sha256(b"run").hexdigest()
    for payload in (
        *_malformed(
            _metrics(),
            (("value", 0, "name", "value"), "cpu_percent"),
            (("value", 0, "timeseries", 0, "data", 3, "maximum"), True),
            (("value", 0, "timeseries", 0, "data", 3, "maximum"), -1),
        ),
        _metrics(points=9),
        _metrics(offset_field="average"),
        {"value": cast(list[object], _metrics()["value"]) * 2},
    ):
        with pytest.raises(AcceptanceCheckError, match=r"platform_document_schema|receipt_store_schema"):
            connection_budget_details(
                payload,
                window_start=datetime(2026, 9, 5, 10, 0, tzinfo=UTC),
                acceptance_run_id="run",
                server_id="server",
                max_connections=100,
                approved_budget=40,
                safety_margin=20,
            )
    with pytest.raises(AcceptanceCheckError, match="receipt_store_schema"):
        connection_budget_details(
            _metrics(count=50.0),
            window_start=datetime(2026, 9, 5, 10, 0, tzinfo=UTC),
            acceptance_run_id="run",
            server_id="server",
            max_connections=100,
            approved_budget=40,
            safety_margin=20,
        )
    with pytest.raises(AcceptanceInputError):
        connection_budget_details(
            _metrics(),
            window_start=datetime(2026, 9, 5, 10, 0, tzinfo=UTC).replace(tzinfo=None),
            acceptance_run_id="run",
            server_id="server",
            max_connections=100,
            approved_budget=40,
            safety_margin=20,
        )


def test_log_analytics_projection_rejects_malformed_rows_and_canary_leaks() -> None:
    canary = "canary-0123456789abcdef"
    rows = [{"TimeGenerated": "2026-09-05T10:00:00Z", "Log_s": '{"event":"RunStarted"}', "ContainerGroupName_g": "elspeth-web--abc-a-1"}]
    assert project_log_analytics_rows(rows, canary_token=canary) == evidence.LogAnalyticsRows(row_count=1, canary_absent=True)
    with pytest.raises(AcceptanceCheckError, match="log_analytics_canary"):
        project_log_analytics_rows([*rows, {"Log_s": f"secret={canary}"}], canary_token=canary)
    for payload in ({"tables": []}, [rows[0], "row"], None):
        with pytest.raises(AcceptanceCheckError, match="platform_document_schema"):
            project_log_analytics_rows(payload, canary_token=canary)
    with pytest.raises(AcceptanceInputError):
        project_log_analytics_rows(rows, canary_token="short")


def test_resource_graph_projection_rejects_malformed_documents() -> None:
    assert project_resource_graph_count({"data": [{"Count": 0}], "total_records": 1}) == 0
    assert project_resource_graph_count(3) == 3
    for payload in (True, -1, "0", {"data": []}, {"data": [{"Count": True}]}, {"data": [{"count": 1}]}, {"data": [1]}, None):
        with pytest.raises(AcceptanceCheckError, match="platform_document_schema"):
            project_resource_graph_count(payload)


def test_resource_graph_cleanup_details_record_exactly_one_vault_fate() -> None:
    tombstoned = resource_graph_cleanup_details(
        resource_group="rg", remaining_resources=0, key_vault_purged=False, scheduled_purge_date=datetime(2026, 12, 4, 10, tzinfo=UTC)
    )
    assert tombstoned == {**VALID["resource-graph-cleanup"](), "resource_group_sha256": hashlib.sha256(b"rg").hexdigest()}
    purged = resource_graph_cleanup_details(resource_group="rg", remaining_resources=0, key_vault_purged=True, scheduled_purge_date=None)
    assert purged["key_vault_purged"] is True and purged["key_vault_tombstoned"] is False and purged["scheduled_purge_date"] is None
    with pytest.raises(AcceptanceCheckError, match="resource_graph_cleanup"):
        resource_graph_cleanup_details(resource_group="rg", remaining_resources=1, key_vault_purged=True, scheduled_purge_date=None)
    with pytest.raises(AcceptanceInputError):
        resource_graph_cleanup_details(resource_group="rg", remaining_resources=0, key_vault_purged=False, scheduled_purge_date=None)


def test_traffic_projection_rejects_malformed_documents() -> None:
    good = [{"label": "a", "weight": 50, "revisionName": "elspeth-web--x-a"}, {"label": "b", "weight": 50}]
    assert project_label_weights(good) == (LabelWeight(label="a", weight=50), LabelWeight(label="b", weight=50))
    assert project_label_weights([]) == ()
    for payload in _malformed(good, ((0, "weight"), True), ((0, "weight"), 150), ((1, "label"), "B")):
        if payload == []:
            continue
        with pytest.raises(AcceptanceCheckError, match="platform_document_schema"):
            project_label_weights(payload)


# --------------------------------------------------------------------------- job details


def test_doctor_report_projection_rejects_malformed_or_failing_reports() -> None:
    report = json.dumps(
        [
            {"name": name, "ok": True, "detail": "ok"}
            for name in (
                "deployment_target",
                "session_schema",
                "landscape_schema",
                "data_dir_writable",
                "payload_store_writable",
                "blob_writable",
            )
        ]
    ).encode()
    details = doctor_job_details(report, execution=EXECUTION, job_name="doctor-runtime-a")
    assert details["checks_ok"] == sorted(
        ["deployment_target", "session_schema", "landscape_schema", "data_dir_writable", "payload_store_writable", "blob_writable"]
    )
    assert details["report_sha256"] == hashlib.sha256(report).hexdigest()
    assert details["init_schema"] is False and "detail" not in json.dumps(details)
    failing = json.loads(report)
    failing[1]["ok"] = False
    with pytest.raises(AcceptanceCheckError, match="doctor_check_failed"):
        doctor_job_details(json.dumps(failing).encode(), execution=EXECUTION, job_name="doctor-runtime-a")
    for payload in (
        b"",
        b"{}",
        b"[]",
        b"not json",
        json.dumps([{"name": "x", "ok": "true", "detail": ""}]).encode(),
        json.dumps([{"name": "Bad Name", "ok": True, "detail": ""}]).encode(),
    ):
        with pytest.raises(AcceptanceCheckError, match="platform_document_schema"):
            doctor_job_details(payload, execution=EXECUTION, job_name="doctor-runtime-a")
    with pytest.raises(AcceptanceCheckError, match="job_execution_failed"):
        doctor_job_details(report, execution=JobExecution(name="x", status="Failed"), job_name="doctor-runtime-a")
    with pytest.raises(AcceptanceInputError):
        doctor_job_details(report, execution=EXECUTION, job_name="provision-storage")


def test_storage_and_blob_details_carry_only_what_the_driver_measured() -> None:
    execution = JobExecution(name="provision-storage-abc", status="Succeeded")
    assert storage_job_details(execution, owner_uid=1654, owner_gid=1654, mode="0700") == {
        **VALID["verify-storage-job"](),
        "execution_name": "provision-storage-abc",
    }
    blob_execution = JobExecution(name="verify-blob-managed-identity-abc", status="Succeeded")
    report = {"cases_total": 2, "cases_passed": 2, "blob_sha256": SHA, "collision_rejected": True, "cleanup_succeeded": True}
    assert blob_managed_identity_details(report, execution=blob_execution) == {
        **VALID["verify-blob-managed-identity"](),
        "execution_name": "verify-blob-managed-identity-abc",
    }


def test_blob_report_projection_rejects_malformed_reports() -> None:
    execution = JobExecution(name="verify-blob-managed-identity-abc", status="Succeeded")
    good = {"cases_total": 2, "cases_passed": 2, "blob_sha256": SHA, "collision_rejected": True, "cleanup_succeeded": True}
    for payload in _malformed(
        good, (("cases_total",), "2"), (("cases_passed",), True), (("blob_sha256",), "abc"), (("collision_rejected",), 1)
    ):
        with pytest.raises(AcceptanceCheckError, match="platform_document_schema"):
            blob_managed_identity_details(payload, execution=execution)


# --------------------------------------------------------------------------- probe observations


def _takeover_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "primitive": "role_revocation",
        "owner_instance_id": "postgresql-a",
        "survivor_instance_id": "postgresql-b",
        "owner_row": {"instance_id": "postgresql-a", "state": "active", "lease_expires_at": "2026-09-05T10:00:30Z"},
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
    }
    document.update(overrides)
    return document


def test_lease_takeover_observation_rejects_malformed_documents() -> None:
    observation = lease_takeover_observation(_takeover_document())
    assert decide_lease_takeover(observation).outcome == "pass"
    assert lease_takeover_observation(_takeover_document(owner_row=None)).owner_row is None
    for payload in (
        *_malformed(
            _takeover_document(),
            (("primitive",), "kill_dash_nine"),
            (("duplicate_sink_effects",), -1),
            (("takeover_observed_at",), "2026-09-05T10:01:05"),
            (("owner_row", "state"), 1),
        ),
        _takeover_document(before_expiry={"addressed_to": "b", "status": 409}),
        _takeover_document(after_expiry={"addressed_to": "b", "status": 9, "instance_id": None, "body": {}}),
        _takeover_document(extra=1),
    ):
        with pytest.raises(AcceptanceCheckError, match="probe_observation_schema"):
            lease_takeover_observation(payload)


def _progress_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "owner_instance_id": "postgresql-a",
        "reader_instance_id": "postgresql-b",
        "poll_interval_seconds": 2.0,
        "status_visible_after_seconds": 0.4,
        "outputs_visible_after_seconds": 0.9,
        "messages_visible_after_seconds": 1.1,
        "blob_sha256_via_owner": SHA,
        "blob_sha256_via_reader": SHA,
        "terminal_status_on_reader": "completed",
    }
    document.update(overrides)
    return document


def test_progress_observation_rejects_malformed_documents() -> None:
    assert cross_replica_progress_observation(_progress_document()).reader_instance_id == "postgresql-b"
    assert cross_replica_progress_observation(_progress_document(terminal_status_on_reader=None)).terminal_status_on_reader is None
    for payload in _malformed(
        _progress_document(),
        (("poll_interval_seconds",), True),
        (("status_visible_after_seconds",), -0.5),
        (("blob_sha256_via_owner",), "x"),
        (("owner_instance_id",), ""),
    ):
        with pytest.raises(AcceptanceCheckError, match="probe_observation_schema"):
            cross_replica_progress_observation(payload)


def test_a_graceful_stop_takeover_is_recorded_as_a_failed_p3_never_a_pass() -> None:
    stopped = lease_takeover_observation(
        _takeover_document(owner_row={"instance_id": "postgresql-a", "state": "stopped", "lease_expires_at": "2026-09-05T10:00:30Z"})
    )
    shared = decide_lease_takeover(stopped)
    assert shared.mechanism == "graceful_stop" and shared.outcome == "pass"
    recorded = lease_takeover_for_receipt(shared)
    assert recorded.outcome == "fail" and recorded.mechanism == "graceful_stop"
    assert recorded.reasons == ("owner_reached_its_release_path:graceful_stop_is_not_a_dead_owner",)
    dead = decide_lease_takeover(lease_takeover_observation(_takeover_document()))
    assert lease_takeover_for_receipt(dead) is dead
    with pytest.raises(AcceptanceInputError):
        lease_takeover_for_receipt(ProbeResult(probe="P1", outcome="pass", mechanism="session_operation_fence"))


# --------------------------------------------------------------------------- receipt store and bundle


def _receipt(kind: str) -> dict[str, object]:
    line = encode_exec_receipt(kind, cast(Any, VALID[kind]()), candidate_sha=CANDIDATE, binding=BINDING, scenario_id="A")
    encoded = line.split(":", 1)[1]
    return cast(dict[str, object], json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))))


def _testcontainer_run(exit_code: int = 0, junit: str = "d" * 64) -> dict[str, object]:
    return {
        "schema": "elspeth.azure-container-apps-testcontainer-run.v1",
        "kind": TESTCONTAINER_RUN_RECEIPT_KIND,
        "candidate_sha": CANDIDATE,
        "scenario_id": "A",
        "selection": list(TESTCONTAINER_SELECTION),
        "exit_code": exit_code,
        "collected": 40,
        "passed": 40 - (2 if exit_code else 0),
        "failed": 2 if exit_code else 0,
        "errors": 0,
        "skipped": 0,
        "junit_sha256": junit,
        "recorded_at": "2026-09-05T10:00:00Z",
    }


def _fill_store(store: Path, *, kinds: set[str] = CHECK_KINDS, runs: tuple[dict[str, object], ...] = (_testcontainer_run(),)) -> None:
    for kind in sorted(kinds):
        receipt_store(
            store, kind=kind, scenario_id="A", subject_id=BINDING.subject, candidate_sha=CANDIDATE, document=_receipt(kind), now=NOW
        )
    for run in runs:
        receipt_store(
            store,
            kind=TESTCONTAINER_RUN_RECEIPT_KIND,
            scenario_id="A",
            subject_id=cast(str, run["junit_sha256"]),
            candidate_sha=CANDIDATE,
            document=run,
            now=NOW,
        )


def test_receipt_store_persists_canonical_bytes_under_their_hash_and_indexes_them(tmp_path: Path) -> None:
    store = tmp_path / "receipts"
    stored = receipt_store(
        store,
        kind="replica-run-start",
        scenario_id="A",
        subject_id=BINDING.subject,
        candidate_sha=CANDIDATE,
        document=_receipt("replica-run-start"),
        now=NOW,
    )
    path = store / f"{stored.receipt_sha256}.json"
    assert path.read_text() == stored.canonical_json
    assert oct(path.stat().st_mode & 0o777) == "0o600" and oct(store.stat().st_mode & 0o777) == "0o700"
    rows = read_receipt_index(store)
    assert rows == [
        {
            "scenario_id": "A",
            "kind": "replica-run-start",
            "subject_sha256": BINDING.sha256,
            "receipt_sha256": stored.receipt_sha256,
            "stored_at": "2026-09-05T10:30:00Z",
        }
    ]
    again = receipt_store(
        store,
        kind="replica-run-start",
        scenario_id="A",
        subject_id=BINDING.subject,
        candidate_sha=CANDIDATE,
        document=_receipt("replica-run-start"),
        now=NOW + timedelta(hours=1),
    )
    assert again.receipt_sha256 == stored.receipt_sha256 and read_receipt_index(store) == rows
    conflicting = _receipt("replica-run-start")
    cast(dict[str, object], conflicting["details"])["evidence"] = {"trials": 19}
    with pytest.raises(AcceptanceCheckError, match="receipt_store_conflict"):
        receipt_store(
            store,
            kind="replica-run-start",
            scenario_id="A",
            subject_id=BINDING.subject,
            candidate_sha=CANDIDATE,
            document=conflicting,
            now=NOW,
        )
    with pytest.raises(AcceptanceCheckError, match="receipt_store_binding"):
        receipt_store(
            store,
            kind="replica-run-start",
            scenario_id="A",
            subject_id="",
            candidate_sha=CANDIDATE,
            document=_receipt("replica-run-start"),
            now=NOW,
        )


def test_receipt_index_rejects_malformed_rows(tmp_path: Path) -> None:
    store = tmp_path / "receipts"
    _fill_store(store, kinds={"replica-run-start"})
    good = json.loads((store / "index.json").read_text())
    for document in _malformed(
        good,
        (("receipts", 0, "kind"), "verify-s3"),
        (("receipts", 0, "receipt_sha256"), "abc"),
        (("receipts", 0, "stored_at"), 1),
        (("receipts", 0), {"kind": "replica-run-start"}),
    ):
        with pytest.raises(AcceptanceCheckError, match="receipt_store_index"):
            evidence._index_rows(document)
    (store / "index.json").write_text(json.dumps({"receipts": [{**good["receipts"][0], "kind": "verify-s3"}]}))
    with pytest.raises(AcceptanceCheckError, match="receipt_store_index"):
        read_receipt_index(store)


def test_bundle_check_passes_only_with_every_kind_valid_and_one_passing_testcontainer_run(tmp_path: Path) -> None:
    store = tmp_path / "receipts"
    _fill_store(store)
    verdict = bundle_check(store, candidate_sha=CANDIDATE, scenario_id="A")
    assert verdict.passed and verdict.testcontainer_reason is None and verdict.testcontainer_receipt_sha256 is not None
    assert {row["kind"] for row in read_receipt_index(store)} == STORED_RECEIPT_KINDS


def test_bundle_check_binds_the_shared_testcontainer_gate_exactly_as_ecs_does(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    _fill_store(missing, runs=())
    assert bundle_check(missing, candidate_sha=CANDIDATE, scenario_id="A").testcontainer_reason == "testcontainer_run_missing"
    failed = tmp_path / "failed"
    _fill_store(failed, runs=(_testcontainer_run(exit_code=1),))
    assert bundle_check(failed, candidate_sha=CANDIDATE, scenario_id="A").testcontainer_reason == "testcontainer_run_failed"
    superseded = tmp_path / "superseded"
    _fill_store(superseded, runs=(_testcontainer_run(exit_code=1), _testcontainer_run(junit="e" * 64)))
    assert bundle_check(superseded, candidate_sha=CANDIDATE, scenario_id="A").passed, (
        "a failed run stays as evidence and a later passing one supersedes it"
    )
    ambiguous = tmp_path / "ambiguous"
    _fill_store(ambiguous, runs=(_testcontainer_run(), _testcontainer_run(junit="e" * 64)))
    assert bundle_check(ambiguous, candidate_sha=CANDIDATE, scenario_id="A").testcontainer_reason == "testcontainer_run_ambiguous"
    tampered = tmp_path / "tampered"
    _fill_store(tampered)
    run_row = next(row for row in read_receipt_index(tampered) if row["kind"] == TESTCONTAINER_RUN_RECEIPT_KIND)
    path = tampered / f"{run_row['receipt_sha256']}.json"
    document = json.loads(path.read_text())
    document["passed"] = 39
    document["failed"] = 1
    os.chmod(path, 0o600)
    path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")))
    assert bundle_check(tampered, candidate_sha=CANDIDATE, scenario_id="A").testcontainer_reason == "testcontainer_run_invalid"


def test_bundle_check_refuses_missing_kinds_invalid_receipts_and_failed_probes(tmp_path: Path) -> None:
    partial = tmp_path / "partial"
    _fill_store(partial, kinds=CHECK_KINDS - {"replica-lease-takeover", "verify-log-analytics"})
    verdict = bundle_check(partial, candidate_sha=CANDIDATE, scenario_id="A")
    assert verdict.missing_kinds == ("replica-lease-takeover", "verify-log-analytics") and not verdict.passed
    failed = tmp_path / "failed-probe"
    _fill_store(failed, kinds=CHECK_KINDS - {"replica-lease-takeover"})
    unreachable = _receipt("replica-lease-takeover")
    cast(dict[str, object], unreachable["details"]).update(
        {"outcome": "unreachable", "reasons": ["web_instances_has_no_row_for_the_owner:membership_authority_not_landed"]}
    )
    receipt_store(
        failed,
        kind="replica-lease-takeover",
        scenario_id="A",
        subject_id=BINDING.subject,
        candidate_sha=CANDIDATE,
        document=unreachable,
        now=NOW,
    )
    verdict = bundle_check(failed, candidate_sha=CANDIDATE, scenario_id="A")
    assert verdict.failed_probes == ("P3",) and not verdict.passed, "an unreachable P3 is not waived"
    tampered = tmp_path / "tampered"
    _fill_store(tampered)
    row = next(row for row in read_receipt_index(tampered) if row["kind"] == "replica-progress")
    path = tampered / f"{row['receipt_sha256']}.json"
    document = json.loads(path.read_text())
    document["details"]["evidence"]["poll_interval_seconds"] = 3.0
    path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")))
    verdict = bundle_check(tampered, candidate_sha=CANDIDATE, scenario_id="A")
    assert [(item.receipt_sha256, item.check) for item in verdict.invalid_receipts] == [(row["receipt_sha256"], "exec_receipt_schema")]
    assert not verdict.passed
    other_candidate = bundle_check(tmp_path / "partial", candidate_sha="f" * 40, scenario_id="A")
    assert other_candidate.invalid_receipts and not other_candidate.passed


def test_probe_receipts_bind_to_the_replica_the_driver_verified() -> None:
    other = ReplicaBinding(
        container_app_id=APP_ID, revision="elspeth-web--abc123def456-b", replica="elspeth-web--abc123def456-b-86c8c4b497-aaaaa"
    )
    assert other.sha256 != BINDING.sha256
    line = encode_exec_receipt(
        "replica-fence-conflict", cast(Any, VALID["replica-fence-conflict"]()), candidate_sha=CANDIDATE, binding=other, scenario_id="A"
    )
    assert other.sha256 in line or json.loads(_decode(line))["replica_binding_sha256"] == other.sha256


def _decode(line: str) -> str:
    encoded = line.split(":", 1)[1]
    return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
