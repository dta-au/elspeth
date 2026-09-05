"""Replica probes on Container Apps (plan §9.1, ``test_replica_probes.py``): decision tables against RECORDED transports.

Two fake replicas sit behind one httpx transport keyed by label URL; each
answers with its own ``X-Elspeth-Instance``. The shared driver fires the pairs
through the Container Apps controller (label-URL addressing) and the shared
decision tables score what was recorded:

- P1: exactly one 2xx and one 409 per trial, two distinct instance values.
- P2: one run id across both responses; the trial type has no permit-row field.
- P3: a takeover reported before lease expiry fails; a ``stopped`` row
  downgrades to ``graceful_stop`` and cannot be recorded as a pass.
- P4a: progress on the non-owner replica must pass; P4b cannot.

The controller's partition primitive is checked against a fake session in the
exact order platform facts §4.2 requires (the runtime session stays open across
the admin ``NOLOGIN``), and the SQL observer against a recorded reader.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from elspeth.web._acceptance_common.errors import AcceptanceCheckError, AcceptanceInputError
from elspeth.web._acceptance_common.http_client import AcceptanceCredentials, AcceptanceHttpClient
from elspeth.web._acceptance_common.replica_probes import (
    SESSION_OPERATION_CONFLICT_DETAIL,
    CrossReplicaProgressObservation,
    LeaseTakeoverObservation,
    MembershipRow,
    ProbeRequest,
    ReplicaAddress,
    ReplicaProbeDriver,
    RunStartTrial,
    decide_cross_replica_progress,
    decide_fence_conflict,
    decide_lease_takeover,
    decide_run_start,
    record_owner_affine_progress,
    replica_response_from_envelope,
)
from elspeth.web._azure_container_apps_acceptance.controller import (
    BACKEND_PID_SQL,
    INGRESS_REQUEST_TIMEOUT_SECONDS,
    PROBE_TOPOLOGY,
    TERMINATE_OWN_ROLE_BACKENDS_SQL,
    ContainerAppsReplicaController,
    IngressTopology,
    LabelWeight,
    PlatformCommands,
    PostgresEvidenceObserver,
    ProbeReplica,
    RoleRevocationPartition,
    SqlReader,
    SqlSession,
    label_url,
    login_sql,
    nologin_sql,
    require_even_label_split,
)
from elspeth.web._azure_container_apps_acceptance.evidence import lease_takeover_for_receipt
from elspeth.web._azure_container_apps_acceptance.receipt_contracts import EXEC_RECEIPT_DESCRIPTOR

RA = "postgresql-aaaaaaaa-0000-4000-8000-000000000001"
RB = "postgresql-bbbbbbbb-0000-4000-8000-000000000002"
DOMAIN = "kindsea-1a2b3c4d.australiaeast.azurecontainerapps.io"
LA = f"https://elspeth-web---a.{DOMAIN}"
LB = f"https://elspeth-web---b.{DOMAIN}"


# --------------------------------------------------------------------------- recorded transports


class _RecordedReplicas:
    """Two replicas behind one transport: the first request of each pair wins the fence, the other gets the 409 body."""

    def __init__(self, *, both_win: bool = False, same_instance: bool = False) -> None:
        self._lock = threading.Lock()
        self._winner: str | None = None
        self._both_win = both_win
        self.instances = {LA: RA, LB: RA if same_instance else RB}
        self.epoch = 3
        self.owner: str | None = None
        self.run_counter = 0

    def reset(self) -> None:
        self._winner = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        origin = f"{request.url.scheme}://{request.url.host}"
        instance = self.instances[origin]
        headers = {"X-Elspeth-Instance": instance}
        if request.url.path == "/api/system/status":
            return httpx.Response(200, json={"instance_id": instance}, headers=headers)
        with self._lock:
            if self._winner is None or self._both_win:
                self._winner = instance
                self.epoch += 1
                self.owner = instance
                if request.url.path.endswith("/execute"):
                    self.run_counter += 1
                    return httpx.Response(202, json={"run_id": f"run-{self.run_counter}"}, headers=headers)
                return httpx.Response(200, json={"status": "accepted"}, headers=headers)
        return httpx.Response(409, json={"detail": SESSION_OPERATION_CONFLICT_DETAIL}, headers=headers)


class _FakeReader(SqlReader):
    def __init__(self, replicas: _RecordedReplicas) -> None:
        self._replicas = replicas
        self.statements: list[str] = []
        self.landscape_missing = False

    def scalar(self, statement: str, **parameters: object) -> object:
        self.statements.append(statement)
        if "operation_epoch" in statement:
            return self._replicas.epoch
        if "owner_instance_id" in statement:
            return self._replicas.owner
        if "clock_timestamp" in statement:
            return datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
        if "count(*) FROM guided_operations" in statement:
            return 1
        raise AssertionError(statement)

    def rows(self, statement: str, **parameters: object) -> tuple[tuple[object, ...], ...]:
        self.statements.append(statement)
        if statement.startswith("SELECT id FROM runs"):
            return ((f"run-{self._replicas.run_counter}",),) if self._replicas.run_counter else ()
        if "landscape_run_id" in statement:
            return ((f"landscape-{self._replicas.run_counter}",),)
        if statement.startswith("SELECT run_id FROM runs"):
            return () if self.landscape_missing else ((parameters["run_id"],),)
        if "web_instances" in statement:
            return ((RA, "active", datetime(2026, 9, 5, 10, 0, 30, tzinfo=UTC)),) if parameters["instance_id"] == RA else ()
        raise AssertionError(statement)


class _FakePlatform(PlatformCommands):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, argv: Sequence[str]) -> bytes:
        self.calls.append(list(argv))
        return b"{}"


class _FakeSession(SqlSession):
    def __init__(self, log: list[str], name: str, *, pid: int = 4242, terminated: int = 9) -> None:
        self._log = log
        self._name = name
        self._pid = pid
        self._terminated = terminated
        self.closed = False

    def execute_scalar(self, statement: str) -> object:
        self._log.append(f"{self._name}: {statement}")
        if statement == BACKEND_PID_SQL:
            return self._pid
        if statement == TERMINATE_OWN_ROLE_BACKENDS_SQL:
            return self._terminated
        return None

    def close(self) -> None:
        self._log.append(f"{self._name}: close")
        self.closed = True


def _partition(log: list[str]) -> RoleRevocationPartition:
    return RoleRevocationPartition(
        admin=lambda: _FakeSession(log, "admin"),
        roles={"elspeth_runtime_a": lambda: _FakeSession(log, "runtime_a"), "elspeth_runtime_b": lambda: _FakeSession(log, "runtime_b")},
    )


def _controller(platform: _FakePlatform | None = None, log: list[str] | None = None) -> ContainerAppsReplicaController:
    return ContainerAppsReplicaController(
        app_name="elspeth-web",
        resource_group="elspeth-acc-run1",
        replicas=(
            ProbeReplica(address=ReplicaAddress("a", LA), revision="elspeth-web--abc-a", role="elspeth_runtime_a"),
            ProbeReplica(address=ReplicaAddress("b", LB), revision="elspeth-web--abc-b", role="elspeth_runtime_b"),
        ),
        partition=_partition(log if log is not None else []),
        platform=platform if platform is not None else _FakePlatform(),
    )


def _driver(replicas: _RecordedReplicas) -> tuple[ReplicaProbeDriver, _FakeReader]:
    transport = httpx.MockTransport(replicas.handle)
    reader = _FakeReader(replicas)

    def client_factory(origin: str) -> AcceptanceHttpClient:
        return AcceptanceHttpClient(
            origin=origin, credentials=AcceptanceCredentials(mode="bearer", bearer_token="token"), transport=transport
        )

    observer = PostgresEvidenceObserver(sessions=reader, landscape=reader)
    return ReplicaProbeDriver(controller=_controller(), observer=observer, client_factory=client_factory), reader


# --------------------------------------------------------------------------- P1 / P2 against recorded transports


class TestFenceConflictRecorded:
    def test_twenty_recorded_pairs_score_a_pass_with_two_distinct_instances(self) -> None:
        replicas = _RecordedReplicas()
        driver, _reader = _driver(replicas)
        trials = []
        for _ in range(20):
            replicas.reset()
            trials.append(
                driver.fence_conflict_trial(
                    "session-1", ProbeRequest("POST", "/api/sessions/session-1/guided/respond", {"turn_token": "0" * 64})
                )
            )
        for trial in trials:
            assert sorted(response.status for response in trial.responses) == [200, 409]
            assert {response.instance_id for response in trial.responses} == {RA, RB}
        result = decide_fence_conflict(trials)
        # The recorded transport's winner is whichever thread arrives first; only a run
        # where both replicas won at least once is a pass, exactly as the decision demands.
        winners = {next(response.instance_id for response in trial.responses if response.succeeded) for trial in trials}
        assert result.outcome == ("pass" if winners == {RA, RB} else "fail")
        assert result.mechanism == "session_operation_fence"
        EXEC_RECEIPT_DESCRIPTOR.detail_validators["replica-fence-conflict"](result.to_receipt_details())

    def test_a_double_dispatch_fails_the_probe(self) -> None:
        replicas = _RecordedReplicas(both_win=True)
        driver, _reader = _driver(replicas)
        trial = driver.fence_conflict_trial("session-1", ProbeRequest("POST", "/api/sessions/session-1/guided/respond", {}))
        assert [response.status for response in trial.responses] == [200, 200]
        result = decide_fence_conflict([trial], required_trials=1)
        assert result.outcome == "fail" and "trial[0]:not_one_success_and_one_fence_refusal" in result.reasons

    def test_two_labels_answered_by_one_instance_fail_the_probe(self) -> None:
        replicas = _RecordedReplicas(same_instance=True)
        driver, _reader = _driver(replicas)
        trial = driver.fence_conflict_trial("session-1", ProbeRequest("POST", "/api/sessions/session-1/guided/respond", {}))
        result = decide_fence_conflict([trial], required_trials=1)
        assert "trial[0]:instances_not_distinct" in result.reasons


class TestRunStartRecorded:
    def test_one_run_id_across_both_responses_and_no_permit_row_field(self) -> None:
        replicas = _RecordedReplicas()
        driver, reader = _driver(replicas)
        trial = driver.run_start_trial("session-1", ProbeRequest("POST", "/api/sessions/session-1/execute", {}))
        accepted = [response for response in trial.responses if response.status == 202]
        refused = [response for response in trial.responses if response.refused_by_fence]
        assert len(accepted) == 1 and len(refused) == 1 and accepted[0].run_id == "run-1"
        assert trial.runs_row_ids == ("run-1",) and trial.landscape_run_ids == ("landscape-1",)
        assert {field.name for field in dataclasses.fields(RunStartTrial)} == {
            "responses",
            "runs_row_ids",
            "landscape_run_ids",
            "dispatch_spread_ms",
        }
        assert not any("run_start_permits" in statement for statement in reader.statements)
        assert decide_run_start([trial], required_trials=1).outcome == "pass"

    def test_a_landscape_run_that_does_not_exist_is_not_counted(self) -> None:
        replicas = _RecordedReplicas()
        driver, reader = _driver(replicas)
        reader.landscape_missing = True
        trial = driver.run_start_trial("session-1", ProbeRequest("POST", "/api/sessions/session-1/execute", {}))
        assert trial.landscape_run_ids == ()
        assert "trial[0]:landscape_runs:0!=1" in decide_run_start([trial], required_trials=1).reasons


# --------------------------------------------------------------------------- P3 / P4 decision tables


def _takeover(**overrides: object) -> LeaseTakeoverObservation:
    expiry = datetime(2026, 9, 5, 10, 0, 30, tzinfo=UTC)
    observation = LeaseTakeoverObservation(
        primitive="role_revocation",
        owner_instance_id=RA,
        survivor_instance_id=RB,
        owner_row=MembershipRow(instance_id=RA, state="active", lease_expires_at=expiry),
        before_expiry=replica_response_from_envelope(
            addressed_to="b", status=409, instance_id=RB, body={"detail": SESSION_OPERATION_CONFLICT_DETAIL}
        ),
        after_expiry=replica_response_from_envelope(addressed_to="b", status=200, instance_id=RB, body={}),
        takeover_observed_at=expiry + timedelta(seconds=35),
        cancelled_run_reason="Orphaned by periodic cleanup — no active executor thread",
        fence_owner_after=RB,
        duplicate_sink_effects=0,
    )
    return dataclasses.replace(observation, **overrides)


class TestLeaseTakeover:
    def test_a_dead_owner_takeover_after_expiry_passes_and_validates_as_a_receipt(self) -> None:
        result = lease_takeover_for_receipt(decide_lease_takeover(_takeover()))
        assert result.outcome == "pass" and result.mechanism == "role_revocation_lease_expiry"
        EXEC_RECEIPT_DESCRIPTOR.detail_validators["replica-lease-takeover"](result.to_receipt_details())

    def test_a_takeover_reported_before_lease_expiry_must_fail(self) -> None:
        early = decide_lease_takeover(_takeover(takeover_observed_at=datetime(2026, 9, 5, 10, 0, 29, tzinfo=UTC)))
        assert early.outcome == "fail" and "takeover_observed_before_lease_expiry" in early.reasons
        at_expiry = decide_lease_takeover(_takeover(takeover_observed_at=datetime(2026, 9, 5, 10, 0, 30, tzinfo=UTC)))
        assert at_expiry.outcome == "fail"

    @pytest.mark.parametrize("state", ["stopped", "draining"])
    def test_a_stopped_row_downgrades_to_graceful_stop_and_cannot_record_pass(self, state: str) -> None:
        row = MembershipRow(instance_id=RA, state=state, lease_expires_at=datetime(2026, 9, 5, 10, 0, 30, tzinfo=UTC))
        recorded = lease_takeover_for_receipt(decide_lease_takeover(_takeover(owner_row=row)))
        assert recorded.mechanism == "graceful_stop" and recorded.outcome == "fail"
        EXEC_RECEIPT_DESCRIPTOR.detail_validators["replica-lease-takeover"](recorded.to_receipt_details())
        with pytest.raises(AcceptanceCheckError, match="exec_receipt_schema"):
            EXEC_RECEIPT_DESCRIPTOR.detail_validators["replica-lease-takeover"](
                {**recorded.to_receipt_details(), "outcome": "pass", "reasons": []}
            )

    def test_the_secondary_primitive_is_named_and_an_absent_row_is_unreachable(self) -> None:
        assert decide_lease_takeover(_takeover(primitive="revision_deactivate")).mechanism == "revision_deactivate_lease_expiry"
        unreachable = decide_lease_takeover(_takeover(owner_row=None))
        assert unreachable.outcome == "unreachable"
        EXEC_RECEIPT_DESCRIPTOR.detail_validators["replica-lease-takeover"](unreachable.to_receipt_details())


class TestCrossReplicaProgress:
    def _observation(self, **overrides: object) -> CrossReplicaProgressObservation:
        observation = CrossReplicaProgressObservation(
            owner_instance_id=RA,
            reader_instance_id=RB,
            poll_interval_seconds=2.0,
            status_visible_after_seconds=0.5,
            outputs_visible_after_seconds=1.0,
            messages_visible_after_seconds=1.5,
            blob_sha256_via_owner="a" * 64,
            blob_sha256_via_reader="a" * 64,
            terminal_status_on_reader="completed",
        )
        return dataclasses.replace(observation, **overrides)

    def test_progress_on_the_non_owner_replica_must_pass(self) -> None:
        result = decide_cross_replica_progress(self._observation())
        assert result.outcome == "pass" and result.mechanism == "postgresql_and_nfs"
        assert decide_cross_replica_progress(self._observation(reader_instance_id=RA)).outcome == "fail"
        assert decide_cross_replica_progress(self._observation(blob_sha256_via_reader="b" * 64)).outcome == "fail"
        assert decide_cross_replica_progress(self._observation(messages_visible_after_seconds=2.5)).outcome == "fail"

    def test_owner_affine_is_recorded_and_structurally_cannot_pass(self) -> None:
        owner_affine = record_owner_affine_progress(mitigation="single_revision_sticky_sessions")
        assert owner_affine.outcome == "cannot_pass" and owner_affine.mechanism == "owner_affine"
        p4a = decide_cross_replica_progress(self._observation())
        EXEC_RECEIPT_DESCRIPTOR.detail_validators["replica-progress"](
            {**p4a.to_receipt_details(), "owner_affine": owner_affine.to_receipt_details()}
        )
        with pytest.raises(AcceptanceCheckError, match="exec_receipt_schema"):
            EXEC_RECEIPT_DESCRIPTOR.detail_validators["replica-progress"](
                {**p4a.to_receipt_details(), "owner_affine": {**owner_affine.to_receipt_details(), "outcome": "pass", "reasons": []}}
            )


# --------------------------------------------------------------------------- the controller port


class TestController:
    def test_label_urls_use_the_triple_dash_form(self) -> None:
        assert label_url(app_name="elspeth-web", label="a", default_domain=DOMAIN) == LA
        with pytest.raises(AcceptanceInputError):
            label_url(app_name="Elspeth", label="a", default_domain=DOMAIN)

    def test_replicas_are_the_two_label_addresses(self) -> None:
        assert _controller().replicas() == (ReplicaAddress("a", LA), ReplicaAddress("b", LB))
        with pytest.raises(AcceptanceInputError):
            ContainerAppsReplicaController(
                app_name="elspeth-web",
                resource_group="rg",
                replicas=(
                    ProbeReplica(address=ReplicaAddress("a", LA), revision="elspeth-web--abc-a", role="elspeth_runtime_a"),
                    ProbeReplica(address=ReplicaAddress("b", LB), revision="elspeth-web--abc-b", role="elspeth_runtime_a"),
                ),
                partition=_partition([]),
                platform=_FakePlatform(),
            )

    def test_partition_keeps_the_runtime_session_open_across_the_admin_nologin(self) -> None:
        log: list[str] = []
        record = _partition(log).partition("elspeth_runtime_a")
        assert log == [
            f"runtime_a: {BACKEND_PID_SQL}",
            f"admin: {nologin_sql('elspeth_runtime_a')}",
            "admin: close",
            f"runtime_a: {TERMINATE_OWN_ROLE_BACKENDS_SQL}",
            "runtime_a: close",
        ]
        assert record.own_backend_pid == 4242 and record.terminated_backends == 9
        assert nologin_sql("elspeth_runtime_a") == 'ALTER ROLE "elspeth_runtime_a" NOLOGIN'
        assert login_sql("elspeth_runtime_b") == 'ALTER ROLE "elspeth_runtime_b" LOGIN'
        assert "pg_signal_backend" not in TERMINATE_OWN_ROLE_BACKENDS_SQL and "current_user" in TERMINATE_OWN_ROLE_BACKENDS_SQL

    def test_partition_refuses_an_unknown_role_and_injection_shaped_names(self) -> None:
        for role in ("elspeth_runtime_a; DROP ROLE x", "postgres", "elspeth_runtime_"):
            with pytest.raises((AcceptanceInputError, KeyError)):
                _partition([]).partition(role)

    def test_partition_owner_stop_owner_and_restore_owner_map_labels_to_roles_and_revisions(self) -> None:
        log: list[str] = []
        platform = _FakePlatform()
        controller = _controller(platform, log)
        controller.partition_owner("a")
        assert log[1] == f"admin: {nologin_sql('elspeth_runtime_a')}"
        controller.stop_owner("a")
        assert platform.calls == [
            [
                "az",
                "containerapp",
                "revision",
                "deactivate",
                "--name",
                "elspeth-web",
                "--resource-group",
                "elspeth-acc-run1",
                "--revision",
                "elspeth-web--abc-a",
            ]
        ]
        controller.restore_owner("a")
        assert log[-2] == f"admin: {login_sql('elspeth_runtime_a')}"
        assert platform.calls[-1][3] == "activate"
        with pytest.raises(AcceptanceInputError):
            controller.partition_owner("c")

    def test_probe_topology_refuses_sticky_outside_single_revision_mode_and_demands_50_50(self) -> None:
        assert IngressTopology(active_revisions_mode="Multiple", session_affinity="none") == PROBE_TOPOLOGY
        with pytest.raises(AcceptanceInputError):
            IngressTopology(active_revisions_mode="Multiple", session_affinity="sticky")
        IngressTopology(active_revisions_mode="Single", session_affinity="sticky")
        require_even_label_split((LabelWeight("a", 50), LabelWeight("b", 50)), labels=("a", "b"))
        for weights in (
            (LabelWeight("a", 100), LabelWeight("b", 0)),
            (LabelWeight("a", 50), LabelWeight("c", 50)),
            (LabelWeight("a", 50),),
        ):
            with pytest.raises(AcceptanceCheckError, match="probe_topology"):
                require_even_label_split(weights, labels=("a", "b"))

    def test_the_ingress_timeout_is_the_platform_constant(self) -> None:
        assert INGRESS_REQUEST_TIMEOUT_SECONDS == 240


class TestObserver:
    def test_guided_operation_rows_needs_the_epoch_reading_taken_before_the_trial(self) -> None:
        replicas = _RecordedReplicas()
        reader = _FakeReader(replicas)
        observer = PostgresEvidenceObserver(sessions=reader, landscape=reader)
        with pytest.raises(AcceptanceInputError):
            observer.guided_operation_rows("session-1", since_epoch=3)
        assert observer.fence_epoch("session-1") == 3
        assert observer.guided_operation_rows("session-1", since_epoch=3) == 1
        with pytest.raises(AcceptanceInputError):
            observer.guided_operation_rows("session-1", since_epoch=2)

    def test_membership_row_reads_the_owner_row_or_none(self) -> None:
        reader = _FakeReader(_RecordedReplicas())
        observer = PostgresEvidenceObserver(sessions=reader, landscape=reader)
        assert observer.membership_row(RA) == MembershipRow(
            instance_id=RA, state="active", lease_expires_at=datetime(2026, 9, 5, 10, 0, 30, tzinfo=UTC)
        )
        assert observer.membership_row(RB) is None
