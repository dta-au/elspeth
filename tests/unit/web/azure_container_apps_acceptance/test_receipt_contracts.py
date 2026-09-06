"""Receipt contracts for the Container Apps binding (plan §9.1, ``test_receipt_contracts.py``).

The envelope with ``replica_binding_sha256``; every kind's closed detail set
with adversarial rejects (bool-as-number, open field sets, forbidden keys,
non-finite); the ``mechanism`` enum closed per kind; the compatibility record;
``schema_facts`` byte-equal to the ECS derivation through ``_acceptance_common``;
gate parity; the ``testcontainer-run`` kind under the azure schema id.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from elspeth.web._acceptance_common.compatibility_gate import compatibility_record_gate
from elspeth.web._acceptance_common.errors import AcceptanceCheckError, AcceptanceInputError
from elspeth.web._acceptance_common.replica_probes import MECHANISMS as PROBE_MECHANISMS_SET
from elspeth.web._acceptance_common.replica_probes import PROBE_MECHANISMS, Mechanism, Probe, ProbeResult
from elspeth.web._acceptance_common.schema_facts import _expected_schema_facts
from elspeth.web._acceptance_common.testcontainer_run import (
    TESTCONTAINER_RUN_RECEIPT_KIND,
    TESTCONTAINER_SELECTION,
    resolve_testcontainer_run_target,
)
from elspeth.web._aws_ecs_acceptance import receipt_contracts as ecs_receipt_contracts
from elspeth.web._azure_container_apps_acceptance import receipt_contracts as contracts
from elspeth.web._azure_container_apps_acceptance.receipt_contracts import (
    CHECK_KINDS,
    COMPATIBILITY_RECEIPT_SCHEMA,
    CONNECTION_BUDGET_SCHEMA,
    EXEC_RECEIPT_DESCRIPTOR,
    EXEC_RECEIPT_PREFIX,
    KIND_MECHANISMS,
    MECHANISMS,
    PLATFORM_MECHANISMS,
    PROBE_KINDS,
    STORED_RECEIPT_KINDS,
    CompatibilityRecordBindings,
    ReplicaBinding,
    encode_exec_receipt,
    extract_exec_receipt,
    validate_check_details,
    validate_compatibility_record,
    validate_stored_receipt,
)

APP_ID = (
    "/subscriptions/0f1e2d3c-4b5a-4c6d-8e7f-a0b1c2d3e4f5/resourceGroups/elspeth-acc-run1/providers/Microsoft.App/containerApps/elspeth-web"
)
REVISION = "elspeth-web--abc123def456-a"
REPLICA = "elspeth-web--abc123def456-a-86c8c4b497-zx9bq"
BINDING = ReplicaBinding(container_app_id=APP_ID, revision=REVISION, replica=REPLICA)
CANDIDATE = "0123456789abcdef0123456789abcdef01234567"
SHA = "a" * 64
RA = "postgresql-aaaaaaaa-0000-4000-8000-000000000001"
RB = "postgresql-bbbbbbbb-0000-4000-8000-000000000002"
RUNBOOK_CHECK_KINDS = (
    "verify-doctor-job",
    "verify-storage-job",
    "verify-blob-managed-identity",
    "verify-log-analytics",
    "verify-connection-budget",
    "compatibility-record",
    "revision-rollout",
    "replica-fence-conflict",
    "replica-run-start",
    "replica-lease-takeover",
    "replica-progress",
    "resource-graph-cleanup",
    "testcontainer-run",
)


def _budget() -> dict[str, object]:
    start = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
    return {
        "schema": CONNECTION_BUDGET_SCHEMA,
        "acceptance_run_id_sha256": SHA,
        "cluster_id_sha256": "b" * 64,
        "window_start": "2026-09-05T10:00:00Z",
        "window_end": "2026-09-05T10:10:00Z",
        "period_seconds": 60,
        "expected_points": 10,
        "points": [{"timestamp": (start + timedelta(minutes=i)).isoformat().replace("+00:00", "Z"), "count": 4.0 + i} for i in range(10)],
        "high_water": 13.0,
        "max_connections": 100,
        "approved_budget": 40,
        "safety_margin": 20,
        "ok": True,
    }


def _probe(probe: str, *, outcome: str = "pass", mechanism: str | None = None, reasons: list[str] | None = None) -> dict[str, object]:
    return {
        "probe": probe,
        "outcome": outcome,
        "mechanism": mechanism
        if mechanism is not None
        else sorted(PROBE_MECHANISMS[probe])[0]
        if probe != "P3"
        else "role_revocation_lease_expiry",
        "reasons": reasons if reasons is not None else ([] if outcome == "pass" else ["trial[0]:not_one_success_and_one_fence_refusal"]),
        "evidence": {"trials": 20, "distinct_winners": 2},
    }


def _job(name: str) -> dict[str, object]:
    return {"mechanism": "container_apps_job", "job_name": name, "execution_name": f"{name}-iwpi4il", "execution_status": "Succeeded"}


VALID: dict[str, Callable[[], dict[str, object]]] = {
    "verify-doctor-job": lambda: {
        **_job("doctor-runtime-a"),
        "report_sha256": SHA,
        "checks_ok": sorted(
            {"blob_writable", "data_dir_writable", "landscape_schema", "payload_store_writable", "session_schema", "deployment_target"}
        ),
        "init_schema": False,
    },
    "verify-storage-job": lambda: {
        **_job("provision-storage"),
        "directories": ["/mnt/elspeth/data", "/mnt/elspeth/data/blobs", "/mnt/elspeth/payloads"],
        "owner_uid": 1654,
        "owner_gid": 1654,
        "mode": "0700",
    },
    "verify-blob-managed-identity": lambda: {
        **_job("verify-blob-managed-identity"),
        "cases_total": 2,
        "cases_passed": 2,
        "blob_sha256": SHA,
        "collision_rejected": True,
        "cleanup_succeeded": True,
    },
    "verify-log-analytics": lambda: {
        "mechanism": "log_analytics_query",
        "workspace_id_sha256": SHA,
        "queries": [
            {"name": name, "query_sha256": SHA, "row_count": 3, "ingestion_lag_seconds_max": 42.5}
            for name in ("doctor-report", "fence-conflict-409", "replica-lifecycle", "run-sentinel-by-replica")
        ],
        "canary_tokens_absent": True,
    },
    "verify-connection-budget": lambda: {"mechanism": "azure_monitor_metrics", "budget": _budget()},
    "compatibility-record": lambda: {
        "mechanism": "operator_record",
        "schema": COMPATIBILITY_RECEIPT_SCHEMA,
        "record_sha256": SHA,
        "scenario_id": "A",
        "gate_passed": True,
        "failed_clauses": [],
    },
    "revision-rollout": lambda: {
        "mechanism": "single_revision_mode",
        "revision_name": "elspeth-web--abc123def456",
        "active_revisions": 1,
        "traffic_weight": 100,
        "image_digest": f"sha256:{SHA}",
        "running_replicas": 2,
        "health_status": 200,
        "ready_status": 200,
        "deployment_target": "azure-container-apps",
    },
    "replica-fence-conflict": lambda: _probe("P1"),
    "replica-run-start": lambda: _probe("P2"),
    "replica-lease-takeover": lambda: _probe("P3"),
    "replica-progress": lambda: {
        **_probe("P4a"),
        "owner_affine": {
            "probe": "P4b",
            "outcome": "cannot_pass",
            "mechanism": "owner_affine",
            "reasons": ["progress_stream_and_websocket_ticket_are_process_local"],
            "evidence": {"mitigation": "single_revision_sticky_sessions"},
        },
    },
    "resource-graph-cleanup": lambda: {
        "mechanism": "resource_graph_query",
        "resource_group_sha256": SHA,
        "remaining_resources": 0,
        "key_vault_purged": False,
        "key_vault_tombstoned": True,
        "scheduled_purge_date": "2026-12-04T10:00:00Z",
    },
}


def _envelope(check: str, details: object, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": 1,
        "check": check,
        "ok": True,
        "candidate_sha": CANDIDATE,
        "replica_binding_sha256": BINDING.sha256,
        "scenario_id": "A",
        "details": details,
    }
    payload.update(overrides)
    return payload


def _validate(check: str, details: object) -> None:
    EXEC_RECEIPT_DESCRIPTOR.detail_validators[check](cast(dict[str, object], details))


# --------------------------------------------------------------------------- vocabularies and binding


class TestVocabularies:
    def test_the_twelve_kinds_plus_testcontainer_run_are_exactly_the_runbooks(self) -> None:
        assert len(CHECK_KINDS) == 12
        assert frozenset(RUNBOOK_CHECK_KINDS) == STORED_RECEIPT_KINDS
        assert EXEC_RECEIPT_DESCRIPTOR.check_kinds == CHECK_KINDS
        assert set(VALID) == CHECK_KINDS

    def test_mechanisms_are_the_probe_vocabulary_plus_the_platform_ones_and_every_kind_claims_a_subset(self) -> None:
        assert MECHANISMS == PROBE_MECHANISMS_SET | PLATFORM_MECHANISMS
        assert not PROBE_MECHANISMS_SET & PLATFORM_MECHANISMS
        for kind, allowed in KIND_MECHANISMS.items():
            assert allowed and allowed <= MECHANISMS, kind
        for kind, probe in PROBE_KINDS.items():
            assert KIND_MECHANISMS[kind] == PROBE_MECHANISMS[probe]

    def test_descriptor_binds_the_replica_subject_under_the_azure_provider(self) -> None:
        assert EXEC_RECEIPT_DESCRIPTOR.provider == "azure"
        assert EXEC_RECEIPT_DESCRIPTOR.subject_field == "replica_binding_sha256"
        assert "task_arn_sha256" not in EXEC_RECEIPT_DESCRIPTOR.envelope_fields
        assert EXEC_RECEIPT_PREFIX == ecs_receipt_contracts._EXEC_RECEIPT_PREFIX


class TestReplicaBinding:
    def test_subject_is_the_documented_formula_and_the_hash_is_sha256_of_it(self) -> None:
        subject = f"{APP_ID}/revisions/{REVISION}/replicas/{REPLICA}"
        assert BINDING.subject == subject
        assert BINDING.sha256 == hashlib.sha256(subject.encode("utf-8")).hexdigest()

    @pytest.mark.parametrize(
        ("app_id", "revision", "replica"),
        [
            ("/subscriptions/x/resourceGroups/rg/providers/Microsoft.App/containerApps/elspeth-web", REVISION, REPLICA),
            ("arn:aws:ecs:ap-southeast-2:123456789012:task/cluster/abc", REVISION, REPLICA),
            (APP_ID, "other-app--abc123def456-a", "other-app--abc123def456-a-86c8c4b497-zx9bq"),
            (APP_ID, "elspeth-web-abc123", "elspeth-web-abc123-1"),
            (APP_ID, REVISION, "elspeth-web--zzz-86c8c4b497-zx9bq"),
            (APP_ID, REVISION, f"{REVISION}-UPPER"),
        ],
    )
    def test_foreign_or_malformed_parts_are_refused(self, app_id: str, revision: str, replica: str) -> None:
        with pytest.raises(AcceptanceInputError):
            ReplicaBinding(container_app_id=app_id, revision=revision, replica=replica)


# --------------------------------------------------------------------------- every kind: closed detail sets


@pytest.mark.parametrize("kind", sorted(CHECK_KINDS))
def test_every_kind_accepts_its_valid_details_and_round_trips_through_the_envelope(kind: str) -> None:
    details = VALID[kind]()
    _validate(kind, details)
    line = encode_exec_receipt(kind, cast(contracts.CheckDetails, details), candidate_sha=CANDIDATE, binding=BINDING, scenario_id="A")
    assert line.startswith(EXEC_RECEIPT_PREFIX)
    receipt = extract_exec_receipt(
        line, expected_candidate_sha=CANDIDATE, expected_binding=BINDING, expected_scenario_id="A", expected_check=kind
    )
    decoded = json.loads(receipt.canonical_json)
    assert decoded["details"] == details
    assert decoded["replica_binding_sha256"] == BINDING.sha256
    assert receipt.kind == kind and receipt.subject_sha256 == BINDING.sha256


def _scalar_paths(value: object, prefix: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    if isinstance(value, dict):
        return [path for key, child in value.items() for path in _scalar_paths(child, (*prefix, key))]
    if isinstance(value, list):
        return [path for index, child in enumerate(value) for path in _scalar_paths(child, (*prefix, index))]
    return [prefix]


def _set_path(document: Any, path: tuple[object, ...], value: object) -> None:
    node = document
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = value


def _get_path(document: Any, path: tuple[object, ...]) -> object:
    node = document
    for step in path:
        node = node[step]
    return node


@pytest.mark.parametrize("kind", sorted(CHECK_KINDS))
def test_every_kind_rejects_open_field_sets(kind: str) -> None:
    """The one detail boundary refuses a dropped field, an extra field, and a kind it does not know."""

    for field in VALID[kind]():
        dropped = VALID[kind]()
        del dropped[field]
        with pytest.raises(AcceptanceCheckError, match="exec_receipt_schema"):
            validate_check_details(kind, details=dropped)
    extra = VALID[kind]()
    extra["extra_field"] = "x"
    with pytest.raises(AcceptanceCheckError, match="exec_receipt_schema"):
        validate_check_details(kind, details=extra)
    with pytest.raises(AcceptanceCheckError, match="exec_receipt_schema"):
        validate_check_details("verify-s3", details=VALID[kind]())


@pytest.mark.parametrize("kind", sorted(CHECK_KINDS))
def test_every_kind_rejects_bool_as_number_and_non_finite_numbers(kind: str) -> None:
    """Every numeric leaf replaced by NaN/inf is refused by the stored-document admission; every typed numeric
    leaf replaced by a bool of equal truth value is refused by the kind validator. A probe's ``evidence`` bag is
    deliberately open (bounded by the shared visitor), so its leaves take only the non-finite mutation."""

    base = VALID[kind]()
    numeric_paths = [path for path in _scalar_paths(base) if type(_get_path(base, path)) in {int, float}]
    assert numeric_paths or kind in {"compatibility-record", "verify-doctor-job"}, f"{kind} has no numeric leaf to mutate"
    for path in numeric_paths:
        for replacement in (math.nan, math.inf, -math.inf):
            mutated = copy.deepcopy(base)
            _set_path(mutated, path, replacement)
            with pytest.raises(AcceptanceCheckError, match=r"receipt_store_schema|exec_receipt_schema"):
                validate_stored_receipt(
                    _envelope(kind, mutated), kind=kind, scenario_id="A", subject_sha256=BINDING.sha256, candidate_sha=CANDIDATE
                )
        if path[0] == "evidence":
            continue
        mutated = copy.deepcopy(base)
        _set_path(mutated, path, bool(_get_path(base, path)))
        with pytest.raises(AcceptanceCheckError, match=r"exec_receipt_schema|receipt_store_schema"):
            _validate(kind, mutated)


@pytest.mark.parametrize("kind", sorted(CHECK_KINDS))
def test_every_kind_rejects_a_forbidden_key_anywhere_in_the_stored_document(kind: str) -> None:
    """The shared forbidden-key visitor runs over the whole document before the kind validator sees it."""

    details = VALID[kind]()
    carrier = details["evidence"] if kind in PROBE_KINDS else details
    cast(dict[str, object], carrier)["password"] = "hunter2"
    with pytest.raises(AcceptanceCheckError, match="receipt_store_schema"):
        validate_stored_receipt(
            _envelope(kind, details), kind=kind, scenario_id="A", subject_sha256=BINDING.sha256, candidate_sha=CANDIDATE
        )


@pytest.mark.parametrize("kind", sorted(CHECK_KINDS))
def test_every_kind_rejects_every_mechanism_it_may_not_claim(kind: str) -> None:
    for mechanism in sorted((MECHANISMS - KIND_MECHANISMS[kind]) | {"kill_dash_nine", ""}):
        details = VALID[kind]()
        details["mechanism"] = mechanism
        with pytest.raises(AcceptanceCheckError, match="exec_receipt_schema"):
            _validate(kind, details)
    details = VALID[kind]()
    details["mechanism"] = True
    with pytest.raises(AcceptanceCheckError, match="exec_receipt_schema"):
        _validate(kind, details)


class TestKindSemantics:
    def test_doctor_job_requires_the_provider_neutral_doctor_checks_and_init_only_on_schema_init(self) -> None:
        details = VALID["verify-doctor-job"]()
        details["checks_ok"] = ["blob_writable", "landscape_schema", "session_schema"]
        with pytest.raises(AcceptanceCheckError):
            _validate("verify-doctor-job", details)
        unsorted = VALID["verify-doctor-job"]()
        unsorted["checks_ok"] = list(reversed(cast(list[str], unsorted["checks_ok"])))
        with pytest.raises(AcceptanceCheckError):
            _validate("verify-doctor-job", unsorted)
        init = VALID["verify-doctor-job"]()
        init["job_name"] = "doctor-schema-init"
        with pytest.raises(AcceptanceCheckError):
            _validate("verify-doctor-job", init)
        init["init_schema"] = True
        init["execution_name"] = "doctor-schema-init-abc"
        _validate("verify-doctor-job", init)
        failed = VALID["verify-doctor-job"]()
        failed["execution_status"] = "Failed"
        with pytest.raises(AcceptanceCheckError):
            _validate("verify-doctor-job", failed)

    def test_storage_job_pins_the_three_directories_uid_1654_and_mode_0700(self) -> None:
        for field, value in (
            ("directories", ["/mnt/elspeth/data"]),
            ("owner_uid", 1000),
            ("owner_gid", 0),
            ("mode", "0755"),
            ("job_name", "doctor-runtime-a"),
        ):
            details = VALID["verify-storage-job"]()
            details[field] = value
            with pytest.raises(AcceptanceCheckError):
                _validate("verify-storage-job", details)

    def test_blob_managed_identity_needs_both_cases_and_both_flags(self) -> None:
        for field, value in (
            ("cases_passed", 1),
            ("cases_total", 3),
            ("collision_rejected", False),
            ("cleanup_succeeded", False),
            ("blob_sha256", "abc"),
        ):
            details = VALID["verify-blob-managed-identity"]()
            details[field] = value
            with pytest.raises(AcceptanceCheckError):
                _validate("verify-blob-managed-identity", details)

    def test_log_analytics_needs_all_four_checked_in_queries_with_rows_within_the_ingestion_ceiling(self) -> None:
        details = VALID["verify-log-analytics"]()
        queries = cast(list[dict[str, object]], details["queries"])
        queries[0]["name"] = "doctor-report"
        queries[1]["name"] = "doctor-report"
        with pytest.raises(AcceptanceCheckError):
            _validate("verify-log-analytics", details)
        for mutation in ({"row_count": 0}, {"ingestion_lag_seconds_max": 601}, {"ingestion_lag_seconds_max": -1}, {"query_sha256": "nope"}):
            details = VALID["verify-log-analytics"]()
            cast(list[dict[str, object]], details["queries"])[2].update(mutation)
            with pytest.raises(AcceptanceCheckError):
                _validate("verify-log-analytics", details)
        details = VALID["verify-log-analytics"]()
        details["canary_tokens_absent"] = False
        with pytest.raises(AcceptanceCheckError):
            _validate("verify-log-analytics", details)

    def test_connection_budget_runs_the_shared_validator_under_the_flexible_server_schema_id(self) -> None:
        details = VALID["verify-connection-budget"]()
        budget = cast(dict[str, object], details["budget"])
        budget["schema"] = "elspeth.rds-connection-budget.v3"
        with pytest.raises(AcceptanceCheckError, match="receipt_store_schema"):
            _validate("verify-connection-budget", details)
        details = VALID["verify-connection-budget"]()
        cast(dict[str, object], details["budget"])["high_water"] = 99.0
        with pytest.raises(AcceptanceCheckError, match="receipt_store_schema"):
            _validate("verify-connection-budget", details)

    def test_compatibility_record_details_must_carry_a_passed_gate_for_scenario_a(self) -> None:
        for field, value in (
            ("gate_passed", False),
            ("failed_clauses", ["backward_compatible"]),
            ("scenario_id", "B"),
            ("schema", "elspeth.aws-ecs-compatibility-receipt.v2"),
        ):
            details = VALID["compatibility-record"]()
            details[field] = value
            with pytest.raises(AcceptanceCheckError):
                _validate("compatibility-record", details)

    def test_revision_rollout_is_one_revision_at_100_percent_with_at_least_two_running_replicas(self) -> None:
        for field, value in (
            ("active_revisions", 2),
            ("traffic_weight", 50),
            ("running_replicas", 1),
            ("health_status", 503),
            ("ready_status", 503),
            ("deployment_target", "aws-ecs"),
        ):
            details = VALID["revision-rollout"]()
            details[field] = value
            with pytest.raises(AcceptanceCheckError):
                _validate("revision-rollout", details)

    def test_resource_graph_cleanup_requires_zero_resources_and_exactly_one_vault_fate(self) -> None:
        purged = VALID["resource-graph-cleanup"]()
        purged.update({"key_vault_purged": True, "key_vault_tombstoned": False, "scheduled_purge_date": None})
        _validate("resource-graph-cleanup", purged)
        for mutation in (
            {"remaining_resources": 1},
            {"key_vault_purged": True},
            {"key_vault_purged": True, "key_vault_tombstoned": False},
            {"scheduled_purge_date": None},
            {"scheduled_purge_date": "2026-12-04T10:00:00+00:00"},
        ):
            details = VALID["resource-graph-cleanup"]()
            details.update(mutation)
            with pytest.raises(AcceptanceCheckError):
                _validate("resource-graph-cleanup", details)


class TestProbeKinds:
    @pytest.mark.parametrize(
        ("kind", "wrong_probe"),
        [("replica-fence-conflict", "P2"), ("replica-run-start", "P1"), ("replica-lease-takeover", "P4a"), ("replica-progress", "P3")],
    )
    def test_each_replica_kind_is_bound_to_its_probe(self, kind: str, wrong_probe: str) -> None:
        details = VALID[kind]()
        details["probe"] = wrong_probe
        with pytest.raises(AcceptanceCheckError, match="exec_receipt_schema"):
            _validate(kind, details)

    def test_outcome_and_reasons_are_re_admitted_through_probe_result(self) -> None:
        for mutation in (
            {"outcome": "passed"},
            {"outcome": "pass", "reasons": ["late"]},
            {"outcome": "fail", "reasons": []},
            {"reasons": "trial"},
            {"evidence": []},
        ):
            details = VALID["replica-fence-conflict"]()
            details.update(mutation)
            with pytest.raises(AcceptanceCheckError, match="exec_receipt_schema"):
                _validate("replica-fence-conflict", details)
        failed = VALID["replica-fence-conflict"]()
        failed.update({"outcome": "fail", "reasons": ["trial[3]:instances_not_distinct"]})
        _validate("replica-fence-conflict", failed)

    def test_lease_takeover_downgraded_to_graceful_stop_cannot_record_pass(self) -> None:
        details = VALID["replica-lease-takeover"]()
        details["mechanism"] = "graceful_stop"
        with pytest.raises(AcceptanceCheckError, match="exec_receipt_schema"):
            _validate("replica-lease-takeover", details)
        details.update({"outcome": "fail", "reasons": ["owner_reached_its_release_path:graceful_stop_is_not_a_dead_owner"]})
        _validate("replica-lease-takeover", details)
        unreachable = VALID["replica-lease-takeover"]()
        unreachable.update(
            {"outcome": "unreachable", "reasons": ["web_instances_has_no_row_for_the_owner:membership_authority_not_landed"]}
        )
        _validate("replica-lease-takeover", unreachable)

    def test_replica_progress_carries_an_owner_affine_record_that_cannot_pass(self) -> None:
        details = VALID["replica-progress"]()
        affine = cast(dict[str, object], details["owner_affine"])
        affine.update({"outcome": "pass", "reasons": []})
        with pytest.raises(AcceptanceCheckError, match="exec_receipt_schema"):
            _validate("replica-progress", details)
        for mutation in ({"probe": "P4a"}, {"mechanism": "postgresql_and_nfs"}, {"outcome": "fail"}):
            details = VALID["replica-progress"]()
            cast(dict[str, object], details["owner_affine"]).update(mutation)
            with pytest.raises(AcceptanceCheckError, match="exec_receipt_schema"):
                _validate("replica-progress", details)
        details = VALID["replica-progress"]()
        details["owner_affine"] = "owner_affine"
        with pytest.raises(AcceptanceCheckError, match="exec_receipt_schema"):
            _validate("replica-progress", details)
        with pytest.raises(ValueError, match="cannot pass"):
            ProbeResult(probe="P4b", outcome="pass", mechanism="owner_affine")

    def test_the_owned_probe_result_itself_refuses_a_foreign_mechanism(self) -> None:
        """The receipt validator narrows first; the owned type must refuse on its own so the driver cannot construct an overclaim."""

        pairs: list[tuple[Probe, Mechanism]] = [
            ("P1", "owner_affine"),
            ("P2", "session_operation_fence"),
            ("P3", "postgresql_and_nfs"),
            ("P4a", "graceful_stop"),
        ]
        for probe, mechanism in pairs:
            with pytest.raises(ValueError, match="cannot claim mechanism"):
                ProbeResult(probe=probe, outcome="pass", mechanism=mechanism)


# --------------------------------------------------------------------------- envelope extraction and the store


def _line(check: str = "replica-fence-conflict", **overrides: object) -> str:
    payload = _envelope(check, VALID[check](), **overrides)
    encoded = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")
    return f"{EXEC_RECEIPT_PREFIX}{encoded}"


def test_extract_exec_receipt_rejects_malformed_streams_and_wrong_bindings() -> None:
    for stream, check in (
        ("no receipt here\n", "exec_receipt"),
        (f"{_line()}\n{_line()}\n", "exec_receipt"),
        (f"{EXEC_RECEIPT_PREFIX}!!!\n", "exec_receipt"),
        (f"{EXEC_RECEIPT_PREFIX}{base64.urlsafe_b64encode(b'[1]').decode().rstrip('=')}\n", "exec_receipt_schema"),
        (f"{EXEC_RECEIPT_PREFIX}{base64.urlsafe_b64encode(b'{"token": 1}').decode().rstrip('=')}\n", "receipt_store_schema"),
        (f"x{'y' * (2 * 1024 * 1024)}\n", "exec_receipt"),
        (_line(candidate_sha="f" * 40), "candidate_binding"),
        (_line(replica_binding_sha256="c" * 64), "replica_binding"),
        (_line(scenario_id="B"), "scenario_binding"),
        (_line("replica-run-start"), "check_binding"),
        (_line(ok=False), "exec_receipt"),
        (_line(task_arn_sha256=SHA), "exec_receipt_schema"),
    ):
        with pytest.raises(AcceptanceCheckError, match=check):
            extract_exec_receipt(
                stream,
                expected_candidate_sha=CANDIDATE,
                expected_binding=BINDING,
                expected_scenario_id="A",
                expected_check="replica-fence-conflict",
            )


_TARGET = resolve_testcontainer_run_target({})


def _testcontainer_run(*, schema: str = "elspeth.azure-container-apps-testcontainer-run.v1", exit_code: int = 0) -> dict[str, object]:
    return {
        "schema": schema,
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
        "junit_sha256": "d" * 64,
        "recorded_at": "2026-09-05T10:00:00Z",
        # The receipt shape is shared across providers (TestcontainerRunReceipt);
        # only the schema id is Azure's. The database fields come from the same
        # resolver the CLI uses, here its Docker arm (elspeth-0ec6918940).
        "database": _TARGET.database,
        "database_identity_sha256": _TARGET.database_identity_sha256,
    }


JUNIT_SUBJECT = hashlib.sha256(("d" * 64).encode()).hexdigest()


def test_validate_stored_receipt_rejects_foreign_kinds_and_mismatched_bindings() -> None:
    good = validate_stored_receipt(
        _envelope("replica-run-start", VALID["replica-run-start"]()),
        kind="replica-run-start",
        scenario_id="A",
        subject_sha256=BINDING.sha256,
        candidate_sha=CANDIDATE,
    )
    assert good.kind == "replica-run-start" and good.receipt_sha256 == hashlib.sha256(good.canonical_json.encode()).hexdigest()
    for payload, kind, scenario, subject, candidate, check in (
        (_envelope("replica-run-start", VALID["replica-run-start"]()), "verify-s3", "A", BINDING.sha256, CANDIDATE, "receipt_store_schema"),
        (
            _envelope("replica-run-start", VALID["replica-run-start"]()),
            "replica-fence-conflict",
            "A",
            BINDING.sha256,
            CANDIDATE,
            "receipt_store_binding",
        ),
        (
            _envelope("replica-run-start", VALID["replica-run-start"]()),
            "replica-run-start",
            "B",
            BINDING.sha256,
            CANDIDATE,
            "receipt_store_binding",
        ),
        (
            _envelope("replica-run-start", VALID["replica-run-start"]()),
            "replica-run-start",
            "A",
            "e" * 64,
            CANDIDATE,
            "receipt_store_binding",
        ),
        (
            _envelope("replica-run-start", VALID["replica-run-start"]()),
            "replica-run-start",
            "A",
            BINDING.sha256,
            "f" * 40,
            "receipt_store_binding",
        ),
        ([], "replica-run-start", "A", BINDING.sha256, CANDIDATE, "receipt_store_schema"),
        (
            _testcontainer_run(schema="elspeth.aws-ecs-testcontainer-run.v1"),
            TESTCONTAINER_RUN_RECEIPT_KIND,
            "A",
            JUNIT_SUBJECT,
            CANDIDATE,
            "receipt_store_schema",
        ),
        (_testcontainer_run(), TESTCONTAINER_RUN_RECEIPT_KIND, "A", BINDING.sha256, CANDIDATE, "receipt_store_binding"),
    ):
        with pytest.raises(AcceptanceCheckError, match=check):
            validate_stored_receipt(payload, kind=kind, scenario_id=scenario, subject_sha256=subject, candidate_sha=candidate)


def test_testcontainer_run_is_stored_under_the_azure_schema_id_through_the_shared_validator() -> None:
    stored = validate_stored_receipt(
        _testcontainer_run(exit_code=1),
        kind=TESTCONTAINER_RUN_RECEIPT_KIND,
        scenario_id="A",
        subject_sha256=JUNIT_SUBJECT,
        candidate_sha=CANDIDATE,
    )
    document = json.loads(stored.canonical_json)
    assert document["schema"] == "elspeth.azure-container-apps-testcontainer-run.v1"
    assert document["exit_code"] == 1, "a failing run is recorded, not refused; the gate decides"


# --------------------------------------------------------------------------- compatibility record and schema facts


def _record() -> dict[str, object]:
    return {
        "schema": COMPATIBILITY_RECEIPT_SCHEMA,
        "record_id": "change-record-id",
        "acceptance_run_id": "acceptance-run-id",
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
        "expires_at": "2026-09-06T09:00:00Z",
    }


BINDINGS = CompatibilityRecordBindings(
    acceptance_run_id="acceptance-run-id",
    candidate_sha=CANDIDATE,
    candidate_image_digest=f"sha256:{SHA}",
    candidate_revision_sha256="1" * 64,
    candidate_doctor_job_sha256="2" * 64,
)
NOW = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)


def test_compatibility_record_rejects_open_field_sets_and_foreign_facts() -> None:
    validated = validate_compatibility_record(_record(), bindings=BINDINGS, now=NOW)
    assert validated.record_sha256 == hashlib.sha256(json.dumps(_record(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    facts_b = copy.deepcopy(_expected_schema_facts("A"))
    cast(dict[str, int], facts_b["candidate"])["landscape_epoch"] += 1
    mutations: list[tuple[Callable[[dict[str, object]], object], str]] = [
        (lambda r: r.pop("record_id"), "compatibility_record_schema"),
        (lambda r: r.__setitem__("extra", 1), "compatibility_record_schema"),
        (lambda r: r.__setitem__("schema", "elspeth.aws-ecs-compatibility-record.v2"), "compatibility_record_schema"),
        (lambda r: r.__setitem__("scenario_id", "B"), "compatibility_record_schema"),
        (lambda r: r.__setitem__("forward_compatible", 1), "compatibility_record_schema"),
        (lambda r: r.__setitem__("countersigner_identity", "database-operator"), "compatibility_record_schema"),
        (lambda r: r.__setitem__("schema_facts", facts_b), "compatibility_record_binding"),
        (lambda r: r.__setitem__("schema_facts", _expected_schema_facts("B")), "compatibility_record_binding"),
        (lambda r: r.__setitem__("previous_package_version", "0.7.1"), "compatibility_record_binding"),
        (lambda r: r.__setitem__("candidate_revision_sha256", "3" * 64), "compatibility_record_binding"),
        (lambda r: r.__setitem__("backward_compatible", True), "compatibility_record_binding"),
        (lambda r: r.__setitem__("expires_at", "2026-09-05T09:59:00Z"), "compatibility_record_expired"),
        (lambda r: r.__setitem__("approved_at", "2026-09-05T09:45:00Z"), "compatibility_record_expired"),
    ]
    for mutate, check in mutations:
        record = _record()
        mutate(record)
        with pytest.raises(AcceptanceCheckError, match=check):
            validate_compatibility_record(record, bindings=BINDINGS, now=NOW)
    with pytest.raises(AcceptanceCheckError, match="compatibility_record_schema"):
        validate_compatibility_record(["not", "a", "record"], bindings=BINDINGS, now=NOW)


def test_schema_facts_are_byte_equal_with_the_ecs_derivation_through_the_shared_core() -> None:
    assert ecs_receipt_contracts._expected_schema_facts is _expected_schema_facts
    for scenario in ("A", "B"):
        assert json.dumps(_expected_schema_facts(scenario), sort_keys=True) == json.dumps(
            ecs_receipt_contracts._expected_schema_facts(scenario), sort_keys=True
        )
    assert _record()["schema_facts"] == ecs_receipt_contracts._expected_schema_facts("A")


def test_gate_verdict_on_the_validated_record_is_the_shared_predicates() -> None:
    record = _record()
    validate_compatibility_record(record, bindings=BINDINGS, now=NOW)
    assert compatibility_record_gate(record, scenario_id="A").passed is True
    record["rollback_permitted"] = True
    assert compatibility_record_gate(record, scenario_id="A").failed_clauses == ("rollback_permitted",)
    assert compatibility_record_gate(_record(), scenario_id="B").failed_clauses == ("previous_landscape_epoch",)
