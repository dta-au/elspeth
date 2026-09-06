"""Durable sink-effect inspection, plan, lease, and attempt lifecycle."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from elspeth.contracts import CallType
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.sink_effects import (
    SINK_EFFECT_PROTOCOL_VERSION,
    SinkEffectAttemptAction,
    SinkEffectAttemptState,
    SinkEffectCommitResult,
    SinkEffectDescriptorMode,
    SinkEffectInputKind,
    SinkEffectInspectionMode,
    SinkEffectLease,
    SinkEffectPlan,
    SinkEffectReconcileKind,
    SinkEffectReconcileResult,
    SinkEffectState,
)
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.errors import LandscapeRecordError, SinkEffectLeaseLiveError
from elspeth.core.landscape.execution.sink_effect_attempt_results import encode_sink_effect_returned_result
from elspeth.core.landscape.execution.sink_effect_lifecycle import (
    SinkEffectAttemptRequest,
    SinkEffectAttemptResult,
)
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import calls_table, operations_table, sink_effects_table
from tests.fixtures.landscape import expire_sink_effect_lease, make_factory, make_landscape_db
from tests.unit.core.landscape.test_sink_effect_finalization import _descriptor, _prepared
from tests.unit.core.landscape.test_sink_effect_reservation import _pipeline_members, _pipeline_request


@pytest.fixture
def db_factory() -> Iterator[tuple[LandscapeDB, RecorderFactory]]:
    db = make_landscape_db()
    try:
        yield db, make_factory(db)
    finally:
        db.close()


def _reserved(factory: RecorderFactory):
    run_id, sink_id, members = _pipeline_members(factory, 1)
    effect = factory.execution.sink_effects.reserve(_pipeline_request(run_id, sink_id, members)).new_effect
    assert effect is not None
    return effect


def _claim(factory: RecorderFactory, effect_id: str, *, owner: str = "worker-a"):
    return factory.execution.sink_effects.claim_preparation(effect_id, owner=owner, ttl=timedelta(seconds=30))


def _plan(effect_id: str, *, plan_hash: str = "a" * 64) -> SinkEffectPlan:
    return SinkEffectPlan(
        effect_id=effect_id,
        protocol_version=SINK_EFFECT_PROTOCOL_VERSION,
        input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
        descriptor_mode=SinkEffectDescriptorMode.RESULT_DERIVED,
        inspection_mode=SinkEffectInspectionMode.NO_INSPECTION_REQUIRED,
        target="file:///tmp/result.jsonl",
        plan_hash=plan_hash,
        payload_hash="b" * 64,
        expected_descriptor=None,
        safe_evidence={"inspection_reference": "no-inspection-required:v1"},
    )


def test_reserved_effect_cannot_lease_before_complete_plan(db_factory: tuple[LandscapeDB, RecorderFactory]) -> None:
    _db, factory = db_factory
    effect = _reserved(factory)

    with pytest.raises(LandscapeRecordError, match="prepared"):
        factory.execution.sink_effects.acquire_lease(effect.effect_id, owner="worker-a", ttl=timedelta(seconds=30))


@pytest.mark.parametrize(
    "forbidden_values",
    (
        {"reconcile_kind": SinkEffectReconcileKind.UNKNOWN},
        {"result_descriptor_hash": "a" * 64},
        {"publication_performed": False, "publication_evidence_kind": "virtual"},
    ),
)
def test_reserved_effect_rejects_stale_result_fields(
    db_factory: tuple[LandscapeDB, RecorderFactory],
    forbidden_values: dict[str, object],
) -> None:
    db, factory = db_factory
    effect = _reserved(factory)

    with pytest.raises(IntegrityError), db.engine.begin() as conn:
        conn.execute(sink_effects_table.update().where(sink_effects_table.c.effect_id == effect.effect_id).values(**forbidden_values))

    with pytest.raises(ValueError, match="reserved effect"):
        replace(effect, **forbidden_values)


def test_prepared_effect_requires_inspection_and_precondition_witnesses(
    db_factory: tuple[LandscapeDB, RecorderFactory],
) -> None:
    db, factory = db_factory
    effect = _reserved(factory)

    with pytest.raises(IntegrityError), db.engine.begin() as conn:
        conn.execute(
            sink_effects_table.update()
            .where(sink_effects_table.c.effect_id == effect.effect_id)
            .values(
                state="prepared",
                plan_json="{}",
                plan_hash="a" * 64,
                inspection_mode="no_inspection_required",
                descriptor_mode="result_derived",
                prepared_at=effect.created_at,
            )
        )


def test_in_flight_effect_rejects_result_fields_before_finalization(
    db_factory: tuple[LandscapeDB, RecorderFactory],
) -> None:
    db, factory = db_factory
    effect = _reserved(factory)
    repo = factory.execution.sink_effects
    repo.complete_plan(effect.effect_id, _plan(effect.effect_id), claim=_claim(factory, effect.effect_id))
    repo.acquire_lease(effect.effect_id, owner="worker-a", ttl=timedelta(seconds=30))

    with pytest.raises(IntegrityError), db.engine.begin() as conn:
        conn.execute(
            sink_effects_table.update().where(sink_effects_table.c.effect_id == effect.effect_id).values(result_descriptor_hash="a" * 64)
        )


def test_concurrent_plan_cas_accepts_equal_and_rejects_divergent(db_factory: tuple[LandscapeDB, RecorderFactory]) -> None:
    _db, factory = db_factory
    effect = _reserved(factory)
    repo = factory.execution.sink_effects

    claim = _claim(factory, effect.effect_id)
    first = repo.complete_plan(effect.effect_id, _plan(effect.effect_id), claim=claim)
    second = repo.complete_plan(effect.effect_id, _plan(effect.effect_id), claim=claim)

    assert first == second
    assert first.state is SinkEffectState.PREPARED
    with pytest.raises(AuditIntegrityError, match="divergent plan"):
        repo.complete_plan(effect.effect_id, _plan(effect.effect_id, plan_hash="c" * 64), claim=claim)


def test_plan_diversion_requires_exact_durable_attribution(
    db_factory: tuple[LandscapeDB, RecorderFactory],
) -> None:
    _db, factory = db_factory
    effect = _reserved(factory)
    plan = replace(
        _plan(effect.effect_id),
        safe_evidence={
            "accepted_ordinals": [],
            "diverted_ordinals": [0],
        },
    )

    with pytest.raises(LandscapeRecordError, match="attribution must cover every diverted member"):
        factory.execution.sink_effects.complete_plan(
            effect.effect_id,
            plan,
            claim=_claim(factory, effect.effect_id),
        )


def test_inspected_plan_requires_returned_inspection_attempt(db_factory: tuple[LandscapeDB, RecorderFactory]) -> None:
    _db, factory = db_factory
    effect = _reserved(factory)
    inspected = replace(_plan(effect.effect_id), inspection_mode=SinkEffectInspectionMode.INSPECTED)

    with pytest.raises(LandscapeRecordError, match="returned inspect"):
        factory.execution.sink_effects.complete_plan(effect.effect_id, inspected, claim=_claim(factory, effect.effect_id))


def test_preparation_claim_is_exclusive_and_fences_plan_bind(db_factory: tuple[LandscapeDB, RecorderFactory]) -> None:
    """Only the live preparation-claim owner may bind the plan (elspeth-3f87c0c055)."""
    _db, factory = db_factory
    effect = _reserved(factory)
    repo = factory.execution.sink_effects

    claim = repo.claim_preparation(effect.effect_id, owner="worker-a", ttl=timedelta(seconds=30))
    assert claim.generation == 1
    claimed = repo.get_effect(effect.effect_id)
    assert claimed is not None
    assert claimed.state is SinkEffectState.RESERVED
    assert (claimed.lease_owner, claimed.generation) == ("worker-a", 1)

    with pytest.raises(LandscapeRecordError, match="live claim"):
        repo.claim_preparation(effect.effect_id, owner="worker-b", ttl=timedelta(seconds=30))

    forged = SinkEffectLease(effect.effect_id, "worker-b", claim.generation, claim.expires_at)
    with pytest.raises(LandscapeRecordError, match="preparation claim"):
        repo.complete_plan(effect.effect_id, _plan(effect.effect_id), claim=forged)

    prepared = repo.complete_plan(effect.effect_id, _plan(effect.effect_id), claim=claim)
    assert prepared.state is SinkEffectState.PREPARED
    assert prepared.generation == claim.generation
    assert prepared.lease_owner is None

    with pytest.raises(LandscapeRecordError, match="reserved effect"):
        repo.claim_preparation(effect.effect_id, owner="worker-b", ttl=timedelta(seconds=30))


def test_expired_preparation_claim_takeover_fences_stale_binder(db_factory: tuple[LandscapeDB, RecorderFactory]) -> None:
    """A takeover bumps the generation so the stale preparer cannot bind (elspeth-3f87c0c055)."""
    _db, factory = db_factory
    effect = _reserved(factory)
    repo = factory.execution.sink_effects

    stale = repo.claim_preparation(effect.effect_id, owner="worker-a", ttl=timedelta(seconds=30))
    with pytest.raises(LandscapeRecordError, match="live claim"):
        repo.claim_preparation(effect.effect_id, owner="worker-b", ttl=timedelta(seconds=30))
    # The claim lapses the way it does in production: its deadline passes on
    # the Landscape database clock (ADR-047), written into the past here.
    expire_sink_effect_lease(_db.engine, effect.effect_id)
    takeover = repo.claim_preparation(effect.effect_id, owner="worker-b", ttl=timedelta(seconds=30))
    assert takeover.generation == stale.generation + 1

    with pytest.raises(LandscapeRecordError, match="preparation claim"):
        repo.complete_plan(effect.effect_id, _plan(effect.effect_id), claim=stale)
    prepared = repo.complete_plan(effect.effect_id, _plan(effect.effect_id), claim=takeover)
    assert prepared.state is SinkEffectState.PREPARED
    assert prepared.generation == takeover.generation


def test_preparation_claim_heartbeat_extends_reserved_claim(db_factory: tuple[LandscapeDB, RecorderFactory]) -> None:
    _db, factory = db_factory
    effect = _reserved(factory)
    repo = factory.execution.sink_effects
    claim = repo.claim_preparation(effect.effect_id, owner="worker-a", ttl=timedelta(seconds=30))

    renewed = repo.heartbeat_lease(
        effect.effect_id,
        owner=claim.owner,
        generation=claim.generation,
        ttl=timedelta(seconds=60),
    )

    assert renewed.owner == claim.owner
    assert renewed.generation == claim.generation
    assert renewed.expires_at > claim.expires_at
    assert repo.get_effect(effect.effect_id).state is SinkEffectState.RESERVED  # type: ignore[union-attr]


def test_lease_takeover_increments_generation_and_fences_stale_results(db_factory: tuple[LandscapeDB, RecorderFactory]) -> None:
    _db, factory = db_factory
    effect = _reserved(factory)
    repo = factory.execution.sink_effects
    repo.complete_plan(effect.effect_id, _plan(effect.effect_id), claim=_claim(factory, effect.effect_id))
    first = repo.acquire_lease(effect.effect_id, owner="worker-a", ttl=timedelta(seconds=30))
    abandoned = repo.begin_attempt(
        SinkEffectAttemptRequest(
            effect_id=effect.effect_id,
            member_ordinal=None,
            generation=first.generation,
            action=SinkEffectAttemptAction.COMMIT,
            call_kind=CallType.FILESYSTEM,
            request_hash="c" * 64,
        )
    )
    with pytest.raises(LandscapeRecordError, match="has not expired"):
        repo.takeover_expired(effect.effect_id, owner="worker-b", ttl=timedelta(seconds=30))
    expire_sink_effect_lease(_db.engine, effect.effect_id)
    second = repo.takeover_expired(effect.effect_id, owner="worker-b", ttl=timedelta(seconds=30))
    assert second.generation == first.generation + 1

    with pytest.raises(LandscapeRecordError, match="stale lease authority"):
        repo.mark_response_lost(abandoned.attempt_id, recovery_lease=first)
    recovered = repo.mark_response_lost(abandoned.attempt_id, recovery_lease=second)
    assert recovered.state is SinkEffectAttemptState.RESPONSE_LOST

    with pytest.raises(LandscapeRecordError, match="stale generation"):
        repo.begin_attempt(
            SinkEffectAttemptRequest(
                effect_id=effect.effect_id,
                member_ordinal=None,
                generation=first.generation,
                action=SinkEffectAttemptAction.COMMIT,
                call_kind=CallType.FILESYSTEM,
                request_hash="d" * 64,
            )
        )


def test_abandoned_commit_intent_becomes_response_lost_with_stable_call_index(
    db_factory: tuple[LandscapeDB, RecorderFactory],
) -> None:
    db, factory = db_factory
    effect = _reserved(factory)
    repo = factory.execution.sink_effects
    repo.complete_plan(effect.effect_id, _plan(effect.effect_id), claim=_claim(factory, effect.effect_id))
    lease = repo.acquire_lease(effect.effect_id, owner="worker-a", ttl=timedelta(seconds=30))
    attempt = repo.begin_attempt(
        SinkEffectAttemptRequest(
            effect_id=effect.effect_id,
            member_ordinal=None,
            generation=lease.generation,
            action=SinkEffectAttemptAction.COMMIT,
            call_kind=CallType.FILESYSTEM,
            request_hash="e" * 64,
        )
    )

    recovered = repo.mark_response_lost(attempt.attempt_id)

    assert recovered.state is SinkEffectAttemptState.RESPONSE_LOST
    with db.read_only_connection() as conn:
        operation_id = conn.scalar(select(operations_table.c.operation_id).where(operations_table.c.sink_effect_id == effect.effect_id))
        calls = conn.execute(
            select(calls_table.c.call_index, calls_table.c.status, calls_table.c.error_json)
            .where(calls_table.c.operation_id == operation_id)
            .order_by(calls_table.c.call_index)
        ).fetchall()
    assert [(row.call_index, row.status) for row in calls] == [(0, "error")]
    assert "response_lost" in (calls[0].error_json or "")


def test_attempt_return_records_one_success_call_and_is_idempotent(db_factory: tuple[LandscapeDB, RecorderFactory]) -> None:
    db, factory = db_factory
    effect = _reserved(factory)
    repo = factory.execution.sink_effects
    repo.complete_plan(effect.effect_id, _plan(effect.effect_id), claim=_claim(factory, effect.effect_id))
    lease = repo.acquire_lease(effect.effect_id, owner="worker-a", ttl=timedelta(seconds=30))
    attempt = repo.begin_attempt(
        SinkEffectAttemptRequest(
            effect_id=effect.effect_id,
            member_ordinal=None,
            generation=lease.generation,
            action=SinkEffectAttemptAction.COMMIT,
            call_kind=CallType.FILESYSTEM,
            request_hash="f" * 64,
        )
    )
    envelope = encode_sink_effect_returned_result(
        SinkEffectCommitResult(descriptor=_descriptor(), evidence={"result": "exact"}, accepted_ordinals=(0,), diverted_ordinals=())
    )
    result = SinkEffectAttemptResult(attempt_id=attempt.attempt_id, evidence=envelope, latency_ms=2.5)

    first = repo.record_attempt_result(result)
    second = repo.record_attempt_result(result)

    assert first == second
    assert first.state is SinkEffectAttemptState.RETURNED
    with db.read_only_connection() as conn:
        operation_id = conn.scalar(select(operations_table.c.operation_id).where(operations_table.c.sink_effect_id == effect.effect_id))
        assert conn.scalar(select(calls_table.c.call_index).where(calls_table.c.operation_id == operation_id)) == 0


def test_attempt_calls_record_adapter_declared_transport(db_factory: tuple[LandscapeDB, RecorderFactory]) -> None:
    db, factory = db_factory
    effect = _reserved(factory)
    repo = factory.execution.sink_effects
    claim = _claim(factory, effect.effect_id)
    repo.complete_plan(effect.effect_id, _plan(effect.effect_id), claim=claim)
    inspect = repo.begin_attempt(
        SinkEffectAttemptRequest(
            effect_id=effect.effect_id,
            member_ordinal=None,
            generation=claim.generation,
            action=SinkEffectAttemptAction.INSPECT,
            call_kind=CallType.FILESYSTEM,
            request_hash="c" * 64,
        )
    )
    assert inspect.call_kind == "filesystem"
    repo.mark_response_lost(inspect.attempt_id)
    lease = repo.acquire_lease(effect.effect_id, owner="worker-a", ttl=timedelta(seconds=30))
    commit = repo.begin_attempt(
        SinkEffectAttemptRequest(
            effect_id=effect.effect_id,
            member_ordinal=None,
            generation=lease.generation,
            action=SinkEffectAttemptAction.COMMIT,
            call_kind=CallType.HTTP,
            request_hash="d" * 64,
        )
    )
    commit_envelope = encode_sink_effect_returned_result(
        SinkEffectCommitResult(descriptor=_descriptor(), evidence={"result": "exact"}, accepted_ordinals=(0,), diverted_ordinals=())
    )
    repo.record_attempt_result(SinkEffectAttemptResult(attempt_id=commit.attempt_id, evidence=commit_envelope, latency_ms=1.0))

    with db.read_only_connection() as conn:
        operation_id = conn.scalar(select(operations_table.c.operation_id).where(operations_table.c.sink_effect_id == effect.effect_id))
        calls = conn.execute(
            select(calls_table.c.call_type, calls_table.c.status)
            .where(calls_table.c.operation_id == operation_id)
            .order_by(calls_table.c.call_index)
        ).fetchall()
    assert [(row.call_type, row.status) for row in calls] == [("filesystem", "error"), ("http", "success")]


def test_attempt_result_refuses_non_envelope_evidence(db_factory: tuple[LandscapeDB, RecorderFactory]) -> None:
    _db, factory = db_factory
    effect = _reserved(factory)
    repo = factory.execution.sink_effects
    repo.complete_plan(effect.effect_id, _plan(effect.effect_id), claim=_claim(factory, effect.effect_id))
    lease = repo.acquire_lease(effect.effect_id, owner="worker-a", ttl=timedelta(seconds=30))
    attempt = repo.begin_attempt(
        SinkEffectAttemptRequest(
            effect_id=effect.effect_id,
            member_ordinal=None,
            generation=lease.generation,
            action=SinkEffectAttemptAction.COMMIT,
            call_kind=CallType.FILESYSTEM,
            request_hash="f" * 64,
        )
    )

    with pytest.raises(LandscapeRecordError, match="exact evidence"):
        repo.record_attempt_result(SinkEffectAttemptResult(attempt_id=attempt.attempt_id, evidence={"result": "raw"}, latency_ms=1.0))

    reloaded = next(item for item in repo.get_attempts(effect.effect_id) if item.attempt_id == attempt.attempt_id)
    assert reloaded.state is SinkEffectAttemptState.INTENT


def _returned_member_attempt(
    factory: RecorderFactory,
    effect,
    lease,
    result,
    *,
    action: SinkEffectAttemptAction,
    request_hash: str,
):
    repo = factory.execution.sink_effects
    attempt = repo.begin_attempt(
        SinkEffectAttemptRequest(
            effect_id=effect.effect_id,
            member_ordinal=0,
            generation=lease.generation,
            action=action,
            call_kind=CallType.FILESYSTEM,
            request_hash=request_hash,
        )
    )
    repo.record_attempt_result(
        SinkEffectAttemptResult(
            attempt_id=attempt.attempt_id,
            evidence=encode_sink_effect_returned_result(result),
            latency_ms=1.0,
        )
    )
    return attempt


def test_finalized_member_replay_rejects_divergent_evidence(db_factory: tuple[LandscapeDB, RecorderFactory]) -> None:
    _db, factory = db_factory
    effect, _members, lease = _prepared(factory, count=1)
    repo = factory.execution.sink_effects
    winner = SinkEffectCommitResult(descriptor=_descriptor(), evidence={"marker": "winner"}, accepted_ordinals=(0,), diverted_ordinals=())
    first = _returned_member_attempt(factory, effect, lease, winner, action=SinkEffectAttemptAction.COMMIT, request_hash="c" * 64)
    repo.complete_member_result(first.attempt_id, winner, lease=lease)
    repo.complete_member_result(first.attempt_id, winner, lease=lease)

    divergent = SinkEffectCommitResult(
        descriptor=_descriptor(), evidence={"marker": "divergent"}, accepted_ordinals=(0,), diverted_ordinals=()
    )
    second = _returned_member_attempt(factory, effect, lease, divergent, action=SinkEffectAttemptAction.COMMIT, request_hash="e" * 64)
    with pytest.raises(LandscapeRecordError, match="evidence is divergent"):
        repo.complete_member_result(second.attempt_id, divergent, lease=lease)


def test_finalized_member_replay_rejects_divergent_descriptor(db_factory: tuple[LandscapeDB, RecorderFactory]) -> None:
    _db, factory = db_factory
    effect, _members, lease = _prepared(factory, count=1)
    repo = factory.execution.sink_effects
    winner = SinkEffectCommitResult(descriptor=_descriptor(), evidence={"marker": "winner"}, accepted_ordinals=(0,), diverted_ordinals=())
    first = _returned_member_attempt(factory, effect, lease, winner, action=SinkEffectAttemptAction.COMMIT, request_hash="c" * 64)
    repo.complete_member_result(first.attempt_id, winner, lease=lease)

    divergent = SinkEffectCommitResult(
        descriptor=_descriptor(content_hash="e" * 64), evidence={"marker": "winner"}, accepted_ordinals=(0,), diverted_ordinals=()
    )
    second = _returned_member_attempt(factory, effect, lease, divergent, action=SinkEffectAttemptAction.COMMIT, request_hash="e" * 64)
    with pytest.raises(LandscapeRecordError, match="descriptor is divergent"):
        repo.complete_member_result(second.attempt_id, divergent, lease=lease)


def test_finalized_member_replay_fails_closed_without_descriptor(db_factory: tuple[LandscapeDB, RecorderFactory]) -> None:
    _db, factory = db_factory
    effect, _members, lease = _prepared(factory, count=1)
    repo = factory.execution.sink_effects
    winner = SinkEffectCommitResult(descriptor=_descriptor(), evidence={"marker": "winner"}, accepted_ordinals=(0,), diverted_ordinals=())
    first = _returned_member_attempt(factory, effect, lease, winner, action=SinkEffectAttemptAction.COMMIT, request_hash="c" * 64)
    repo.complete_member_result(first.attempt_id, winner, lease=lease)

    not_applied = SinkEffectReconcileResult.not_applied(evidence={"probe": "target-absent"})
    reconcile = _returned_member_attempt(
        factory, effect, lease, not_applied, action=SinkEffectAttemptAction.RECONCILE, request_hash="f" * 64
    )
    with pytest.raises(LandscapeRecordError, match="descriptor"):
        repo.complete_member_result(reconcile.attempt_id, not_applied, lease=lease)


def test_lease_decisions_ignore_the_process_clock_and_follow_database_time(
    db_factory: tuple[LandscapeDB, RecorderFactory],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C6 stage 4 control (ADR-047): every sink-effect lease decision is made against Landscape database time.

    The process clock is patched a year ahead of the deadline it would have
    stamped; a repository that still consulted it would stamp the deadline
    from 2040 and then judge it expired at 2041. Against database time the
    lease is live, the takeover is refused, and every stamped deadline lands
    inside the database's own window.
    """
    from datetime import UTC, datetime

    from elspeth.core.landscape.execution import sink_effect_lifecycle
    from tests.fixtures.landscape import assert_deadline_within, landscape_database_now

    db, factory = db_factory
    effect = _reserved(factory)
    repo = factory.execution.sink_effects
    ttl = timedelta(seconds=30)

    monkeypatch.setattr(sink_effect_lifecycle, "now", lambda: datetime(2040, 1, 1, tzinfo=UTC))
    claim = repo.claim_preparation(effect.effect_id, owner="worker-a", ttl=ttl)
    assert_deadline_within(claim.expires_at, landscape_database_now(db.engine) + ttl)
    repo.complete_plan(effect.effect_id, _plan(effect.effect_id), claim=claim)
    lease = repo.acquire_lease(effect.effect_id, owner="worker-a", ttl=ttl)
    assert_deadline_within(lease.expires_at, landscape_database_now(db.engine) + ttl)

    monkeypatch.setattr(sink_effect_lifecycle, "now", lambda: datetime(2041, 1, 1, tzinfo=UTC))
    # The refusal is the typed one the executor's lease wait maps, and it is
    # still a LandscapeRecordError for every caller that catches that.
    with pytest.raises(SinkEffectLeaseLiveError, match="has not expired"):
        repo.takeover_expired(effect.effect_id, owner="worker-b", ttl=ttl)
    contended = _reserved(factory)
    repo.claim_preparation(contended.effect_id, owner="worker-a", ttl=ttl)
    with pytest.raises(SinkEffectLeaseLiveError, match="live claim"):
        repo.claim_preparation(contended.effect_id, owner="worker-b", ttl=ttl)
    assert issubclass(SinkEffectLeaseLiveError, LandscapeRecordError)
    renewed = repo.heartbeat_lease(effect.effect_id, owner="worker-a", generation=lease.generation, ttl=ttl)
    assert_deadline_within(renewed.expires_at, landscape_database_now(db.engine) + ttl)
    assert repo.lease_validity_seconds(effect.effect_id) is not None

    # Only the database clock lapses a lease: written into its past, the same
    # takeover the 2041 process clock could not buy is granted.
    expire_sink_effect_lease(db.engine, effect.effect_id)
    assert repo.lease_validity_seconds(effect.effect_id) is None
    taken = repo.takeover_expired(effect.effect_id, owner="worker-b", ttl=ttl)
    assert taken.generation == lease.generation + 1
    assert_deadline_within(taken.expires_at, landscape_database_now(db.engine) + ttl)


def test_a_sub_second_lease_is_live_for_its_whole_ttl_when_stamped_late_in_a_database_second(
    db_factory: tuple[LandscapeDB, RecorderFactory],
) -> None:
    """C6-34-FIX: a lease is live for at least the TTL asked for, wherever in a database second it is stamped.

    ADR-047 puts the stamp (``database_now + ttl``) and every later liveness
    test (``lease_expires_at >= database_now``) on the same whole-second SQLite
    clock. A raw sum discards the stamp instant's own fraction, so a 0.2 s
    lease stamped 0.95 s into second S lapsed 50 ms later and the finalizer
    then refused its OWN live lease ("sink effect finalization lease has
    expired"). Rounding the TTL up to the clock's resolution restores the
    guarantee; whole-second TTLs, which is every TTL in ``src``, are
    unaffected.
    """
    import time

    from elspeth.core.landscape.database_clock import read_landscape_transaction_time
    from elspeth.core.landscape.execution.sink_effect_lifecycle import lease_is_live
    from tests.fixtures.landscape import late_in_a_database_second

    db, factory = db_factory
    repo = factory.execution.sink_effects
    effect = _reserved(factory)
    ttl = timedelta(milliseconds=200)

    claim = late_in_a_database_second(
        db.engine,
        lambda _database_now: repo.claim_preparation(effect.effect_id, owner="worker-a", ttl=ttl),
    )
    stamped_at = time.monotonic()

    def _still_live() -> bool:
        with db.write_connection() as conn:
            database_now = read_landscape_transaction_time(conn)
            return lease_is_live(conn, effect.effect_id, sink_effects_table.c.lease_expires_at >= database_now)

    assert _still_live(), f"the claim was not live at the instant it was stamped (deadline {claim.expires_at.isoformat()})"
    # Measure the lifetime the lease actually delivers. Sampling and load can
    # only ever OVER-report it, so a slow box cannot turn this assertion red;
    # it can only mask the defect, which the mutant in the ledger covers.
    give_up = time.monotonic() + 5.0
    while time.monotonic() < give_up and _still_live():
        time.sleep(0.005)
    delivered = time.monotonic() - stamped_at
    assert delivered >= ttl.total_seconds(), (
        f"a lease claimed late in a database second delivered {delivered:.3f}s of its {ttl.total_seconds():.3f}s TTL "
        f"(deadline {claim.expires_at.isoformat()})"
    )


@pytest.mark.parametrize(
    ("ttl", "expected"),
    [
        # SQLite's whole second: the corner that let a 0.2 s lease lapse.
        (timedelta(milliseconds=200), timedelta(seconds=1)),
        (timedelta(milliseconds=1), timedelta(seconds=1)),
        # A fractional TTL above the resolution rounds UP, never down.
        (timedelta(seconds=1, milliseconds=1), timedelta(seconds=2)),
        # Whole seconds are untouched, so no production deadline moves: the
        # only sink-effect TTL in ``src`` is the coordinator's 5 minutes.
        (timedelta(seconds=1), timedelta(seconds=1)),
        (timedelta(minutes=5), timedelta(minutes=5)),
    ],
)
def test_aligned_lease_ttl_rounds_up_to_the_database_clock_resolution(
    db_factory: tuple[LandscapeDB, RecorderFactory],
    ttl: timedelta,
    expected: timedelta,
) -> None:
    """The rounding is a pure duration: same clock resolution in, same offset out."""
    from elspeth.core.landscape.execution.sink_effect_lifecycle import _aligned_lease_ttl

    db, _factory = db_factory
    with db.engine.connect() as conn:
        assert _aligned_lease_ttl(conn, ttl) == expected
