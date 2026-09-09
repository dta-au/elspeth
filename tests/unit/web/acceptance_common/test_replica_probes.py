"""Decision tables for the replicas > 1 probes, the closed ``mechanism`` vocabulary, and the driver against fakes.

Plan §9.1 (``test_replica_probes.py``): P1 exactly one 2xx and one 409 with
two distinct ``X-Elspeth-Instance`` values; P2 one run id across both
responses and NO permit-row assertion; P3 a takeover reported before lease
expiry must fail and a ``stopped`` row must downgrade to ``graceful_stop``;
P4a progress on the non-owner replica; P4b records ``owner_affine`` and
cannot record ``pass``.
"""

from __future__ import annotations

import dataclasses
import threading
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest

from elspeth.web._acceptance_common.errors import AcceptanceCheckError, AcceptanceHttpError, AcceptanceInputError
from elspeth.web._acceptance_common.http_client import AcceptanceCredentials, AcceptanceHttpClient
from elspeth.web._acceptance_common.replica_probes import (
    MECHANISMS,
    ORPHAN_CANCELLATION_REASON_PREFIX,
    PROBE_MECHANISMS,
    SESSION_OPERATION_CONFLICT_DETAIL,
    CrossReplicaProgressObservation,
    EvidenceObserver,
    FenceConflictTrial,
    LeaseTakeoverObservation,
    MembershipRow,
    ProbeRequest,
    ProbeResult,
    ReplicaAddress,
    ReplicaController,
    ReplicaProbeDriver,
    ReplicaResponse,
    RunStartTrial,
    decide_cross_replica_progress,
    decide_fence_conflict,
    decide_lease_takeover,
    decide_run_start,
    record_owner_affine_progress,
    replica_response_from_envelope,
)

RA = "web-aaaaaaaa-0000-4000-8000-000000000001"
RB = "web-bbbbbbbb-0000-4000-8000-000000000002"


def _ok(replica: str, instance: str, *, status: int = 200, run_id: str | None = None) -> ReplicaResponse:
    return ReplicaResponse(addressed_to=replica, status=status, instance_id=instance, run_id=run_id)


def _conflict(replica: str, instance: str) -> ReplicaResponse:
    return ReplicaResponse(addressed_to=replica, status=409, instance_id=instance, detail=SESSION_OPERATION_CONFLICT_DETAIL)


def _fence_trial(index: int, **overrides: object) -> FenceConflictTrial:
    winner, loser = (RA, RB) if index % 2 == 0 else (RB, RA)
    trial = FenceConflictTrial(
        responses=(_ok("rA", winner), _conflict("rB", loser)),
        fence_epoch_before=index,
        fence_epoch_after=index + 1,
        fence_owner_after=winner,
        guided_operation_rows=1,
        dispatch_spread_ms=1.5,
    )
    return dataclasses.replace(trial, **overrides)  # type: ignore[arg-type]


class TestVocabulary:
    def test_mechanisms_are_closed_and_every_probe_claims_a_subset(self) -> None:
        assert {
            "session_operation_fence",
            "session_operation_fence_execute",
            "role_revocation_lease_expiry",
            "revision_deactivate_lease_expiry",
            "graceful_stop",
            "postgresql_and_nfs",
            "owner_affine",
        } == MECHANISMS
        assert set().union(*PROBE_MECHANISMS.values()) == MECHANISMS
        assert set(PROBE_MECHANISMS) == {"P1", "P2", "P3", "P4a", "P4b"}

    @pytest.mark.parametrize(
        ("probe", "mechanism"),
        [
            ("P1", "owner_affine"),
            ("P2", "session_operation_fence"),
            ("P3", "postgresql_and_nfs"),
            ("P4a", "graceful_stop"),
            ("P4b", "postgresql_and_nfs"),
        ],
    )
    def test_a_probe_cannot_claim_another_probes_mechanism(self, probe: str, mechanism: str) -> None:
        with pytest.raises(ValueError, match="cannot claim mechanism"):
            ProbeResult(probe=probe, outcome="fail", mechanism=mechanism, reasons=("x",))  # type: ignore[arg-type]

    @pytest.mark.parametrize("outcome", ["pass", "fail", "unreachable"])
    def test_p4b_cannot_be_constructed_with_any_outcome_but_cannot_pass(self, outcome: str) -> None:
        with pytest.raises(ValueError, match="cannot pass"):
            ProbeResult(probe="P4b", outcome=outcome, mechanism="owner_affine", reasons=("x",))  # type: ignore[arg-type]

    def test_unknown_vocabulary_is_refused(self) -> None:
        with pytest.raises(ValueError):
            ProbeResult(probe="P9", outcome="pass", mechanism="session_operation_fence")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            ProbeResult(probe="P1", outcome="maybe", mechanism="session_operation_fence")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            ProbeResult(probe="P1", outcome="pass", mechanism="session_operation_fence", reasons=("a pass carries no reasons",))
        with pytest.raises(ValueError):
            ProbeResult(probe="P1", outcome="fail", mechanism="session_operation_fence", reasons=())

    def test_receipt_details_are_the_closed_five(self) -> None:
        details = record_owner_affine_progress(mitigation="single_revision_sticky_sessions").to_receipt_details()
        assert set(details) == {"probe", "outcome", "mechanism", "reasons", "evidence"}
        assert details["outcome"] == "cannot_pass"
        assert details["mechanism"] == "owner_affine"


class TestFenceConflict:
    def test_twenty_clean_trials_pass(self) -> None:
        result = decide_fence_conflict([_fence_trial(index) for index in range(20)])
        assert result.outcome == "pass"
        assert result.mechanism == "session_operation_fence"
        assert result.evidence == {"trials": 20, "distinct_winners": 2}

    def test_trial_count_is_enforced(self) -> None:
        result = decide_fence_conflict([_fence_trial(index) for index in range(19)])
        assert result.outcome == "fail"
        assert "trial_count:19!=20" in result.reasons

    def test_double_dispatch_fails(self) -> None:
        trials = [_fence_trial(index) for index in range(20)]
        trials[3] = dataclasses.replace(trials[3], responses=(_ok("rA", RA), _ok("rB", RB)))
        result = decide_fence_conflict(trials)
        assert result.outcome == "fail"
        assert "trial[3]:not_one_success_and_one_fence_refusal" in result.reasons

    def test_a_409_without_the_fence_body_is_not_a_fence_refusal(self) -> None:
        trials = [_fence_trial(index) for index in range(20)]
        other_409 = ReplicaResponse(addressed_to="rB", status=409, instance_id=RB, detail="Blob already exists")
        trials[0] = dataclasses.replace(trials[0], responses=(_ok("rA", RA), other_409))
        assert "trial[0]:not_one_success_and_one_fence_refusal" in decide_fence_conflict(trials).reasons

    def test_same_instance_answering_both_fails(self) -> None:
        trials = [_fence_trial(index) for index in range(20)]
        trials[5] = dataclasses.replace(trials[5], responses=(_ok("rA", RA), _conflict("rB", RA)))
        assert "trial[5]:instances_not_distinct" in decide_fence_conflict(trials).reasons

    def test_missing_instance_header_fails(self) -> None:
        trials = [_fence_trial(index) for index in range(20)]
        trials[5] = dataclasses.replace(trials[5], responses=(_ok("rA", None), _conflict("rB", RB)))
        assert "trial[5]:instances_not_distinct" in decide_fence_conflict(trials).reasons

    def test_fence_epoch_must_advance_by_exactly_one(self) -> None:
        trials = [_fence_trial(index) for index in range(20)]
        trials[7] = dataclasses.replace(trials[7], fence_epoch_after=trials[7].fence_epoch_before + 2)
        assert "trial[7]:fence_epoch_not_advanced_by_one" in decide_fence_conflict(trials).reasons

    def test_fence_owner_must_be_the_winner(self) -> None:
        trials = [_fence_trial(index) for index in range(20)]
        trials[2] = dataclasses.replace(trials[2], fence_owner_after=RB)  # trial 2's winner is RA
        assert "trial[2]:fence_owner_is_not_the_winner" in decide_fence_conflict(trials).reasons

    def test_exactly_one_guided_operation_row(self) -> None:
        trials = [_fence_trial(index) for index in range(20)]
        trials[9] = dataclasses.replace(trials[9], guided_operation_rows=2)
        assert "trial[9]:guided_operation_rows:2!=1" in decide_fence_conflict(trials).reasons

    def test_dispatch_window_is_enforced(self) -> None:
        trials = [_fence_trial(index) for index in range(20)]
        trials[1] = dataclasses.replace(trials[1], dispatch_spread_ms=5.5)
        assert "trial[1]:dispatch_spread_ms:5.500>5.0" in decide_fence_conflict(trials).reasons

    def test_one_replica_winning_every_trial_fails(self) -> None:
        trials = [_fence_trial(index * 2) for index in range(20)]  # RA wins every trial
        assert "winners_not_distinct_across_run" in decide_fence_conflict(trials).reasons


class TestRunStart:
    def _trial(self, index: int) -> RunStartTrial:
        winner, loser = (RA, RB) if index % 2 == 0 else (RB, RA)
        run_id = f"run-{index}"
        return RunStartTrial(
            responses=(_ok("rA", winner, status=202, run_id=run_id), _conflict("rB", loser)),
            runs_row_ids=(run_id,),
            landscape_run_ids=(f"landscape-{index}",),
            dispatch_spread_ms=2.0,
        )

    def test_twenty_clean_trials_pass_with_one_run_each(self) -> None:
        result = decide_run_start([self._trial(index) for index in range(20)])
        assert result.outcome == "pass"
        assert result.mechanism == "session_operation_fence_execute"

    def test_the_trial_type_has_no_permit_row_field(self) -> None:
        """Nothing in the tree writes run_start_permits, so a receipt cannot assert one."""
        assert {field.name for field in dataclasses.fields(RunStartTrial)} == {
            "responses",
            "runs_row_ids",
            "landscape_run_ids",
            "dispatch_spread_ms",
        }
        assert not any("permit" in field.name for field in dataclasses.fields(RunStartTrial))

    def test_two_runs_rows_fail(self) -> None:
        trials = [self._trial(index) for index in range(20)]
        trials[4] = dataclasses.replace(trials[4], runs_row_ids=("run-4", "run-4-duplicate"))
        assert "trial[4]:runs_rows:2!=1_or_not_the_accepted_run" in decide_run_start(trials).reasons

    def test_runs_row_must_be_the_accepted_run(self) -> None:
        trials = [self._trial(index) for index in range(20)]
        trials[4] = dataclasses.replace(trials[4], runs_row_ids=("some-other-run",))
        assert "trial[4]:runs_rows:1!=1_or_not_the_accepted_run" in decide_run_start(trials).reasons

    def test_two_landscape_runs_fail(self) -> None:
        trials = [self._trial(index) for index in range(20)]
        trials[6] = dataclasses.replace(trials[6], landscape_run_ids=("a", "b"))
        assert "trial[6]:landscape_runs:2!=1" in decide_run_start(trials).reasons

    def test_two_accepted_runs_fail(self) -> None:
        trials = [self._trial(index) for index in range(20)]
        trials[0] = dataclasses.replace(trials[0], responses=(_ok("rA", RA, status=202, run_id="x"), _ok("rB", RB, status=202, run_id="y")))
        assert "trial[0]:not_one_accepted_run_and_one_fence_refusal" in decide_run_start(trials).reasons

    def test_accepted_without_a_run_id_is_not_accepted(self) -> None:
        trials = [self._trial(index) for index in range(20)]
        trials[0] = dataclasses.replace(trials[0], responses=(_ok("rA", RA, status=202, run_id=None), _conflict("rB", RB)))
        assert "trial[0]:not_one_accepted_run_and_one_fence_refusal" in decide_run_start(trials).reasons


def _takeover(**overrides: object) -> LeaseTakeoverObservation:
    expiry = datetime(2026, 9, 5, 10, 0, 30, tzinfo=UTC)
    observation = LeaseTakeoverObservation(
        primitive="role_revocation",
        owner_instance_id=RA,
        survivor_instance_id=RB,
        owner_row=MembershipRow(instance_id=RA, state="active", lease_expires_at=expiry),
        before_expiry=_conflict("rB", RB),
        after_expiry=_ok("rB", RB),
        takeover_observed_at=expiry + timedelta(seconds=5),
        cancelled_run_reason="Orphaned by periodic cleanup — no active executor thread [landscape-reconciliation:pending]",
        fence_owner_after=RB,
        duplicate_sink_effects=0,
    )
    return dataclasses.replace(observation, **overrides)  # type: ignore[arg-type]


class TestLeaseTakeover:
    def test_dead_owner_takeover_after_expiry_passes_with_the_primary_mechanism(self) -> None:
        result = decide_lease_takeover(_takeover())
        assert result.outcome == "pass"
        assert result.mechanism == "role_revocation_lease_expiry"
        assert result.evidence["owner_row_state"] == "active"
        assert result.evidence["lease_expires_at"] == "2026-09-05T10:00:30Z"

    def test_secondary_primitive_names_its_own_mechanism(self) -> None:
        assert decide_lease_takeover(_takeover(primitive="revision_deactivate")).mechanism == "revision_deactivate_lease_expiry"

    def test_takeover_before_lease_expiry_fails(self) -> None:
        """The mutation case: a survivor that took over early is a fence defect, not a pass."""
        early = _takeover(takeover_observed_at=datetime(2026, 9, 5, 10, 0, 29, tzinfo=UTC))
        result = decide_lease_takeover(early)
        assert result.outcome == "fail"
        assert "takeover_observed_before_lease_expiry" in result.reasons
        at_expiry = _takeover(takeover_observed_at=datetime(2026, 9, 5, 10, 0, 30, tzinfo=UTC))
        assert "takeover_observed_before_lease_expiry" in decide_lease_takeover(at_expiry).reasons

    @pytest.mark.parametrize("state", ["stopped", "draining"])
    def test_a_stopped_or_draining_owner_row_downgrades_to_graceful_stop(self, state: str) -> None:
        row = MembershipRow(instance_id=RA, state=state, lease_expires_at=datetime(2026, 9, 5, 10, 0, 30, tzinfo=UTC))
        result = decide_lease_takeover(_takeover(owner_row=row))
        assert result.mechanism == "graceful_stop"
        assert result.outcome == "pass"
        assert result.evidence["owner_row_state"] == state

    def test_no_membership_row_is_unreachable_not_fail(self) -> None:
        result = decide_lease_takeover(_takeover(owner_row=None))
        assert result.outcome == "unreachable"
        assert result.mechanism == "role_revocation_lease_expiry"
        assert result.reasons == ("web_instances_has_no_row_for_the_owner:membership_authority_not_landed",)

    def test_survivor_admitted_before_expiry_fails(self) -> None:
        assert "survivor_not_refused_before_lease_expiry" in decide_lease_takeover(_takeover(before_expiry=_ok("rB", RB))).reasons

    def test_startup_sweep_wording_is_not_a_survivor_takeover(self) -> None:
        result = decide_lease_takeover(_takeover(cancelled_run_reason="Orphaned by server restart — no active process"))
        assert "owner_run_not_cancelled_with_the_orphan_reason" in result.reasons
        assert decide_lease_takeover(_takeover(cancelled_run_reason=None)).outcome == "fail"
        assert ORPHAN_CANCELLATION_REASON_PREFIX == "Orphaned by periodic cleanup"

    def test_fence_owner_must_move_to_the_survivor(self) -> None:
        assert "fence_owner_did_not_move_to_the_survivor" in decide_lease_takeover(_takeover(fence_owner_after=RA)).reasons

    def test_duplicate_sink_effects_fail(self) -> None:
        assert "duplicate_sink_effects:1" in decide_lease_takeover(_takeover(duplicate_sink_effects=1)).reasons

    def test_after_expiry_response_must_come_from_the_survivor(self) -> None:
        assert "after_expiry_response_not_from_survivor" in decide_lease_takeover(_takeover(after_expiry=_ok("rB", RA))).reasons

    def test_naive_datetimes_are_refused(self) -> None:
        naive_takeover = datetime.fromisoformat("2026-09-05T10:00:35")
        naive_expiry = datetime.fromisoformat("2026-09-05T10:00:30")
        assert naive_takeover.tzinfo is None and naive_expiry.tzinfo is None
        with pytest.raises(AcceptanceCheckError, match="probe_timestamp"):
            decide_lease_takeover(_takeover(takeover_observed_at=naive_takeover))
        with pytest.raises(AcceptanceCheckError, match="probe_timestamp"):
            decide_lease_takeover(_takeover(owner_row=MembershipRow(instance_id=RA, state="active", lease_expires_at=naive_expiry)))

    def test_non_utc_aware_datetimes_compare_in_utc(self) -> None:
        """The database session timezone cannot move the verdict: 12:00:35+02:00 is 10:00:35Z."""
        plus_two = timezone(timedelta(hours=2))
        row = MembershipRow(instance_id=RA, state="active", lease_expires_at=datetime(2026, 9, 5, 12, 0, 30, tzinfo=plus_two))
        result = decide_lease_takeover(_takeover(owner_row=row, takeover_observed_at=datetime(2026, 9, 5, 12, 0, 35, tzinfo=plus_two)))
        assert result.outcome == "pass"
        assert result.evidence["lease_expires_at"] == "2026-09-05T10:00:30Z"
        assert result.evidence["takeover_observed_at"] == "2026-09-05T10:00:35Z"


class TestCrossReplicaProgress:
    def _observation(self, **overrides: object) -> CrossReplicaProgressObservation:
        observation = CrossReplicaProgressObservation(
            owner_instance_id=RA,
            reader_instance_id=RB,
            poll_interval_seconds=2.0,
            status_visible_after_seconds=0.4,
            outputs_visible_after_seconds=1.1,
            messages_visible_after_seconds=0.9,
            blob_sha256_via_owner="a" * 64,
            blob_sha256_via_reader="a" * 64,
            terminal_status_on_reader="completed",
        )
        return dataclasses.replace(observation, **overrides)  # type: ignore[arg-type]

    def test_visible_within_one_poll_passes(self) -> None:
        result = decide_cross_replica_progress(self._observation())
        assert result.outcome == "pass"
        assert result.mechanism == "postgresql_and_nfs"

    def test_late_visibility_fails(self) -> None:
        assert (
            "outputs_not_visible_within_one_poll_interval"
            in decide_cross_replica_progress(self._observation(outputs_visible_after_seconds=2.5)).reasons
        )

    def test_blob_bytes_must_agree(self) -> None:
        assert (
            "blob_bytes_differ_across_replicas" in decide_cross_replica_progress(self._observation(blob_sha256_via_reader="b" * 64)).reasons
        )

    def test_terminal_status_must_be_observed_on_the_reader(self) -> None:
        assert (
            "terminal_status_not_observed_on_reader"
            in decide_cross_replica_progress(self._observation(terminal_status_on_reader="running")).reasons
        )

    def test_same_instance_is_not_cross_replica(self) -> None:
        assert "owner_and_reader_are_the_same_instance" in decide_cross_replica_progress(self._observation(reader_instance_id=RA)).reasons


class TestOwnerAffineProgress:
    def test_records_owner_affine_and_the_mitigation(self) -> None:
        result = record_owner_affine_progress(mitigation="single_revision_sticky_sessions")
        assert result.probe == "P4b"
        assert result.outcome == "cannot_pass"
        assert result.mechanism == "owner_affine"
        assert result.evidence == {"mitigation": "single_revision_sticky_sessions"}


@pytest.mark.parametrize(
    ("body", "expected_detail", "expected_run_id"),
    [
        ({"detail": SESSION_OPERATION_CONFLICT_DETAIL}, SESSION_OPERATION_CONFLICT_DETAIL, None),
        ({"run_id": "run-1", "status": "queued"}, None, "run-1"),
        ({"detail": "x" * 257}, None, None),
        ({"detail": 12, "run_id": ["run-1"]}, None, None),
        ({"detail": ""}, None, None),
        ([{"detail": "list"}], None, None),
        ("Session operation is already active", None, None),
        (None, None, None),
    ],
)
def test_replica_response_projection_takes_only_bounded_detail_and_run_id(
    body: object, expected_detail: str | None, expected_run_id: str | None
) -> None:
    response = replica_response_from_envelope(addressed_to="rA", status=409, instance_id=RA, body=body)
    assert response == ReplicaResponse(addressed_to="rA", status=409, instance_id=RA, detail=expected_detail, run_id=expected_run_id)


class _FakeController(ReplicaController):
    def __init__(self, first: ReplicaAddress, second: ReplicaAddress) -> None:
        self._replicas = (first, second)
        self.calls: list[tuple[str, str]] = []

    def replicas(self) -> tuple[ReplicaAddress, ReplicaAddress]:
        return self._replicas

    def partition_owner(self, replica: str) -> None:
        self.calls.append(("partition", replica))

    def stop_owner(self, replica: str) -> None:
        self.calls.append(("stop", replica))

    def restore_owner(self, replica: str) -> None:
        self.calls.append(("restore", replica))


class _FakeObserver(EvidenceObserver):
    def __init__(self) -> None:
        self.epoch = 3
        self.owner: str | None = None

    def fence_epoch(self, session_id: str) -> int:
        return self.epoch

    def fence_owner(self, session_id: str) -> str | None:
        return self.owner

    def guided_operation_rows(self, session_id: str, *, since_epoch: int) -> int:
        return 1

    def runs_row_ids(self, session_id: str) -> tuple[str, ...]:
        return ("run-1",)

    def landscape_run_ids(self, session_id: str) -> tuple[str, ...]:
        return ("landscape-1",)

    def membership_row(self, instance_id: str) -> MembershipRow | None:
        return None


class _RecordedReplicas:
    """Two fake replicas behind one httpx transport: the first request to arrive wins the fence."""

    def __init__(self, observer: _FakeObserver) -> None:
        self._observer = observer
        self._lock = threading.Lock()
        self._winner: str | None = None
        self.instances = {"https://app---la.example.test": RA, "https://app---lb.example.test": RB}

    def handle(self, request: httpx.Request) -> httpx.Response:
        origin = f"{request.url.scheme}://{request.url.host}"
        instance = self.instances[origin]
        with self._lock:
            if self._winner is None:
                self._winner = instance
                self._observer.epoch += 1
                self._observer.owner = instance
                return httpx.Response(202, json={"run_id": "run-1"}, headers={"X-Elspeth-Instance": instance})
        return httpx.Response(409, json={"detail": SESSION_OPERATION_CONFLICT_DETAIL}, headers={"X-Elspeth-Instance": instance})


def _driver(observer: _FakeObserver, replicas: _RecordedReplicas) -> ReplicaProbeDriver:
    transport = httpx.MockTransport(replicas.handle)

    def client_factory(origin: str) -> AcceptanceHttpClient:
        return AcceptanceHttpClient(
            origin=origin, credentials=AcceptanceCredentials(mode="bearer", bearer_token="token"), transport=transport
        )

    controller = _FakeController(
        ReplicaAddress("la", "https://app---la.example.test"), ReplicaAddress("lb", "https://app---lb.example.test")
    )
    return ReplicaProbeDriver(controller=controller, observer=observer, client_factory=client_factory)


class TestDriver:
    def test_fence_conflict_trial_records_both_instances_and_the_fence_facts(self) -> None:
        observer = _FakeObserver()
        driver = _driver(observer, _RecordedReplicas(observer))
        trial = driver.fence_conflict_trial("session-1", ProbeRequest("POST", "/api/sessions/session-1/guided/respond", {"text": "go"}))
        statuses = sorted(response.status for response in trial.responses)
        assert statuses == [202, 409]
        assert {response.instance_id for response in trial.responses} == {RA, RB}
        assert trial.fence_epoch_after == trial.fence_epoch_before + 1
        assert trial.fence_owner_after == next(response.instance_id for response in trial.responses if response.status == 202)
        assert trial.guided_operation_rows == 1
        assert trial.dispatch_spread_ms >= 0

    def test_run_start_trial_reads_the_run_rows(self) -> None:
        observer = _FakeObserver()
        driver = _driver(observer, _RecordedReplicas(observer))
        trial = driver.run_start_trial("session-1", ProbeRequest("POST", "/api/sessions/session-1/execute", {}))
        accepted = [response for response in trial.responses if response.status == 202]
        assert len(accepted) == 1 and accepted[0].run_id == "run-1"
        assert trial.runs_row_ids == ("run-1",)
        assert trial.landscape_run_ids == ("landscape-1",)

    def test_two_replicas_must_be_distinct(self) -> None:
        observer = _FakeObserver()
        replicas = _RecordedReplicas(observer)
        transport = httpx.MockTransport(replicas.handle)
        same = ReplicaAddress("la", "https://app---la.example.test")
        driver = ReplicaProbeDriver(
            controller=_FakeController(same, same),
            observer=observer,
            client_factory=lambda origin: AcceptanceHttpClient(
                origin=origin, credentials=AcceptanceCredentials(mode="bearer", bearer_token="t"), transport=transport
            ),
        )
        with pytest.raises(AcceptanceInputError):
            driver.fire_pair(ProbeRequest("GET", "/api/health", None), expected_statuses={200})

    def test_malformed_instance_header_is_refused_by_the_client(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={}, headers={"X-Elspeth-Instance": "bad\theader"})

        client = AcceptanceHttpClient(
            origin="https://app---la.example.test",
            credentials=AcceptanceCredentials(mode="bearer", bearer_token="t"),
            transport=httpx.MockTransport(handle),
        )
        with pytest.raises(AcceptanceHttpError, match="malformed instance header"):
            client.request_json_with_instance("GET", "/api/health", expected_statuses={200})

    def test_absent_instance_header_reads_as_none(self) -> None:
        client = AcceptanceHttpClient(
            origin="https://app---la.example.test",
            credentials=AcceptanceCredentials(mode="bearer", bearer_token="t"),
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "ok"})),
        )
        assert client.request_json_with_instance("GET", "/api/health", expected_statuses={200}) == (200, None, {"status": "ok"})
