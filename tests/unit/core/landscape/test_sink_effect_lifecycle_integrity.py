"""Public lifecycle refusals keep durable plans, attempts and members unchanged."""

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from elspeth.contracts import CallType
from elspeth.contracts.sink_effects import (
    SinkEffectAttemptAction,
    SinkEffectCommitResult,
    SinkEffectDescriptorMode,
    SinkEffectReconcileResult,
    SinkEffectState,
)
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.execution.sink_effect_attempt_results import encode_sink_effect_returned_result
from elspeth.core.landscape.execution.sink_effect_lifecycle import SinkEffectAttemptRequest, SinkEffectAttemptResult
from elspeth.core.landscape.schema import (
    calls_table,
    operations_table,
    sink_effect_attempts_table,
    sink_effect_members_table,
    sink_effects_table,
)
from tests.fixtures.landscape import expire_sink_effect_lease, leader_coordination_token, make_factory, make_landscape_db
from tests.unit.core.landscape.test_sink_effect_finalization import _descriptor, _prepared
from tests.unit.core.landscape.test_sink_effect_lifecycle import _claim, _plan, _reserved


@pytest.fixture
def db_factory():
    db = make_landscape_db()
    try:
        yield db, make_factory(db)
    finally:
        db.close()


def _durable_state(db):
    with db.read_only_connection() as conn:
        return tuple(
            tuple(sorted(tuple(row) for row in conn.execute(select(table)).fetchall()))
            for table in (sink_effects_table, sink_effect_members_table, sink_effect_attempts_table, operations_table, calls_table)
        )


def _attribution(ordinal=0, reason_hash="d" * 64, error_hash="e" * 16):
    return {"ordinal": ordinal, "reason_hash": reason_hash, "error_hash": error_hash}


@pytest.mark.parametrize(
    "evidence, message",
    [
        ({"accepted_ordinals": [0]}, "both be present"),
        ({"diverted_ordinals": []}, "both be present"),
        ({"accepted_ordinals": "0", "diverted_ordinals": []}, "both be lists"),
        ({"accepted_ordinals": [0], "diverted_ordinals": {}}, "both be lists"),
        ({"accepted_ordinals": [True], "diverted_ordinals": []}, "integers"),
        ({"accepted_ordinals": [0.0], "diverted_ordinals": []}, "integers"),
        ({"accepted_ordinals": [-1], "diverted_ordinals": []}, "integers"),
        ({"accepted_ordinals": [0, 0], "diverted_ordinals": []}, "unique ascending"),
        ({"accepted_ordinals": [1, 0], "diverted_ordinals": []}, "unique ascending"),
        ({"accepted_ordinals": [], "diverted_ordinals": [True]}, "integers"),
        ({"accepted_ordinals": [], "diverted_ordinals": []}, "exactly partition"),
        ({"accepted_ordinals": [0], "diverted_ordinals": [0]}, "exactly partition"),
        ({"accepted_ordinals": [1], "diverted_ordinals": []}, "exactly partition"),
        ({"accepted_ordinals": [], "diverted_ordinals": [0], "diversion_attribution": {}}, "must be a list"),
        ({"accepted_ordinals": [], "diverted_ordinals": [0], "diversion_attribution": ["bad"]}, "field set"),
        ({"accepted_ordinals": [], "diverted_ordinals": [0], "diversion_attribution": [{"ordinal": 0}]}, "field set"),
        ({"accepted_ordinals": [], "diverted_ordinals": [0], "diversion_attribution": [_attribution(True)]}, "invalid"),
        ({"accepted_ordinals": [], "diverted_ordinals": [0], "diversion_attribution": [_attribution(1)]}, "invalid"),
        ({"accepted_ordinals": [], "diverted_ordinals": [0], "diversion_attribution": [_attribution(reason_hash=7)]}, "invalid"),
        ({"accepted_ordinals": [], "diverted_ordinals": [0], "diversion_attribution": [_attribution(reason_hash="D" * 64)]}, "invalid"),
        ({"accepted_ordinals": [], "diverted_ordinals": [0], "diversion_attribution": [_attribution(error_hash=7)]}, "invalid"),
        ({"accepted_ordinals": [], "diverted_ordinals": [0], "diversion_attribution": [_attribution(error_hash="e" * 64)]}, "invalid"),
        ({"accepted_ordinals": [], "diverted_ordinals": [0], "diversion_attribution": [_attribution(), _attribution()]}, "invalid"),
        ({"accepted_ordinals": [], "diverted_ordinals": [0]}, "cover every diverted"),
    ],
)
def test_plan_partition_refusal_rolls_back_preparation(db_factory, evidence, message):
    db, factory = db_factory
    effect = _reserved(factory)
    claim = _claim(factory, effect.effect_id, run_id=effect.run_id)
    token = leader_coordination_token(factory, effect.run_id)
    before = _durable_state(db)
    with pytest.raises(LandscapeRecordError, match=message):
        factory.execution.sink_effects.complete_plan(
            effect.effect_id, replace(_plan(effect.effect_id), safe_evidence=evidence), claim=claim, coordination_token=token
        )
    assert _durable_state(db) == before


def test_valid_diversion_plan_retry_keeps_exact_member_attribution(db_factory):
    db, factory = db_factory
    effect = _reserved(factory)
    claim = _claim(factory, effect.effect_id, run_id=effect.run_id)
    token = leader_coordination_token(factory, effect.run_id)
    plan = replace(
        _plan(effect.effect_id),
        safe_evidence={"accepted_ordinals": [], "diverted_ordinals": [0], "diversion_attribution": [_attribution()]},
    )
    first = factory.execution.sink_effects.complete_plan(effect.effect_id, plan, claim=claim, coordination_token=token)
    before = _durable_state(db)
    assert factory.execution.sink_effects.complete_plan(effect.effect_id, plan, claim=claim, coordination_token=token) == first
    assert _durable_state(db) == before
    with db.read_only_connection() as conn:
        member = conn.execute(select(sink_effect_members_table)).one()
    assert (member.prepared_disposition, member.reason_hash, member.member_state) == ("diverted", "d" * 64, "prepared")


def _commit_result():
    return SinkEffectCommitResult(descriptor=_descriptor(), evidence={"receipt": "winner"}, accepted_ordinals=(0,), diverted_ordinals=())


def _attempt(factory, effect, lease, *, action=SinkEffectAttemptAction.COMMIT, member_ordinal=0):
    return factory.execution.sink_effects.begin_attempt(
        SinkEffectAttemptRequest(
            effect_id=effect.effect_id,
            member_ordinal=member_ordinal,
            generation=lease.generation,
            action=action,
            call_kind=CallType.FILESYSTEM,
            request_hash="f" * 64,
        ),
        coordination_token=leader_coordination_token(factory, effect.run_id),
    )


def _return(factory, effect, attempt, result, *, latency=1.0):
    return factory.execution.sink_effects.record_attempt_result(
        SinkEffectAttemptResult(attempt_id=attempt.attempt_id, evidence=encode_sink_effect_returned_result(result), latency_ms=latency),
        coordination_token=leader_coordination_token(factory, effect.run_id),
    )


@pytest.mark.parametrize("change", ["evidence", "latency", "response_lost", "stale_generation"])
def test_attempt_return_refuses_conflicting_durable_authority(db_factory, change):
    db, factory = db_factory
    effect, _members, lease = _prepared(factory, count=1)
    repo = factory.execution.sink_effects
    attempt = _attempt(factory, effect, lease)
    result = _commit_result()
    token = leader_coordination_token(factory, effect.run_id)
    if change in {"evidence", "latency"}:
        _return(factory, effect, attempt, result)
        result = replace(result, evidence={"receipt": "divergent"}) if change == "evidence" else result
        message = "divergent"
    elif change == "response_lost":
        repo.mark_response_lost(attempt.attempt_id, coordination_token=token)
        message = "cannot return"
    else:
        expire_sink_effect_lease(db.engine, effect.effect_id)
        repo.takeover_expired(effect.effect_id, owner="new-worker", ttl=timedelta(seconds=30), coordination_token=token)
        message = "stale generation"
    before = _durable_state(db)
    with pytest.raises(LandscapeRecordError, match=message):
        _return(factory, effect, attempt, result, latency=2.0 if change == "latency" else 1.0)
    assert _durable_state(db) == before


@pytest.mark.parametrize("method", ["record", "complete", "lost"])
def test_absent_attempt_refuses_without_mutation(db_factory, method):
    db, factory = db_factory
    effect, _members, lease = _prepared(factory, count=1)
    repo = factory.execution.sink_effects
    token = leader_coordination_token(factory, effect.run_id)
    before = _durable_state(db)
    with pytest.raises(LandscapeRecordError, match="does not exist"):
        if method == "record":
            repo.record_attempt_result(SinkEffectAttemptResult(attempt_id="e" * 64, evidence={}, latency_ms=1.0), coordination_token=token)
        elif method == "complete":
            repo.complete_member_result("e" * 64, _commit_result(), lease=lease, coordination_token=token)
        else:
            repo.mark_response_lost("e" * 64, coordination_token=token)
    assert _durable_state(db) == before


@pytest.mark.parametrize("change", ["effect_scope", "unreturned", "wrong_action", "wrong_evidence", "wrong_owner", "expired"])
def test_member_completion_requires_exact_return_and_live_lease(db_factory, change):
    db, factory = db_factory
    effect, _members, lease = _prepared(factory, count=1)
    repo = factory.execution.sink_effects
    result = _commit_result()
    attempt = _attempt(
        factory,
        effect,
        lease,
        member_ordinal=None if change == "effect_scope" else 0,
        action=SinkEffectAttemptAction.RECONCILE if change == "wrong_action" else SinkEffectAttemptAction.COMMIT,
    )
    if change != "unreturned":
        _return(
            factory,
            effect,
            attempt,
            SinkEffectReconcileResult.unknown(evidence={"probe": "unknown"}) if change == "wrong_action" else result,
        )
    if change == "wrong_evidence":
        result = replace(result, evidence={"receipt": "impostor"})
    if change == "wrong_owner":
        lease = replace(lease, owner="other-worker")
    if change == "expired":
        expire_sink_effect_lease(db.engine, effect.effect_id)
    token = leader_coordination_token(factory, effect.run_id)
    before = _durable_state(db)
    message = {
        "effect_scope": "member-scoped",
        "unreturned": "returned member",
        "wrong_action": "returned member",
        "wrong_evidence": "differs from",
        "wrong_owner": "stale lease",
        "expired": "stale lease",
    }[change]
    with pytest.raises(LandscapeRecordError, match=message):
        repo.complete_member_result(attempt.attempt_id, result, lease=lease, coordination_token=token)
    assert _durable_state(db) == before


@pytest.mark.parametrize("kind, expected", [("unknown", SinkEffectState.IN_FLIGHT), ("not_applied", SinkEffectState.PREPARED)])
def test_member_reconciliation_retains_uncertainty_or_reopens_only_member(db_factory, kind, expected):
    db, factory = db_factory
    effect, _members, lease = _prepared(factory, count=1)
    repo = factory.execution.sink_effects
    result = (
        SinkEffectReconcileResult.unknown(evidence={"probe": "uncertain"})
        if kind == "unknown"
        else SinkEffectReconcileResult.not_applied(evidence={"probe": "absent"})
    )
    attempt = _attempt(factory, effect, lease, action=SinkEffectAttemptAction.RECONCILE)
    _return(factory, effect, attempt, result)
    repo.complete_member_result(
        attempt.attempt_id, result, lease=lease, coordination_token=leader_coordination_token(factory, effect.run_id)
    )
    with db.read_only_connection() as conn:
        member = conn.execute(select(sink_effect_members_table)).one()
    assert member.member_state == expected.value
    assert member.descriptor_hash is None
    assert repo.get_effect(effect.effect_id).state is SinkEffectState.IN_FLIGHT


@pytest.mark.parametrize(
    "evidence, message",
    [
        ({}, "requires diversion attribution"),
        ({"diversion_attribution": {}}, "requires diversion attribution"),
        ({"diversion_attribution": ["bad"]}, "invalid"),
        ({"diversion_attribution": [{"ordinal": 0}]}, "invalid"),
        ({"diversion_attribution": [_attribution(reason_hash="bad")]}, "invalid"),
        ({"diversion_attribution": [_attribution(error_hash="bad")]}, "invalid"),
        ({"diversion_attribution": [_attribution(False)]}, "invalid"),
        ({"diversion_attribution": [_attribution(0.0)]}, "invalid"),
        ({"diversion_attribution": [_attribution(-1)]}, "invalid"),
        ({"diversion_attribution": [_attribution("0")]}, "invalid"),
        ({"diversion_attribution": [_attribution(1)]}, "does not cover"),
    ],
)
def test_diverted_member_completion_refuses_missing_exact_attribution(db_factory, evidence, message):
    db, factory = db_factory
    effect, _members, lease = _prepared(factory, count=1)
    repo = factory.execution.sink_effects
    result = replace(_commit_result(), accepted_ordinals=(), diverted_ordinals=(0,), evidence=evidence)
    attempt = _attempt(factory, effect, lease)
    _return(factory, effect, attempt, result)
    token = leader_coordination_token(factory, effect.run_id)
    before = _durable_state(db)
    with pytest.raises(LandscapeRecordError, match=message):
        repo.complete_member_result(attempt.attempt_id, result, lease=lease, coordination_token=token)
    assert _durable_state(db) == before


def test_diverted_member_completion_accepts_exact_integer_attribution(db_factory):
    db, factory = db_factory
    effect, _members, lease = _prepared(factory, count=1)
    repo = factory.execution.sink_effects
    result = replace(_commit_result(), accepted_ordinals=(), diverted_ordinals=(0,), evidence={"diversion_attribution": [_attribution()]})
    attempt = _attempt(factory, effect, lease)
    _return(factory, effect, attempt, result)
    repo.complete_member_result(
        attempt.attempt_id, result, lease=lease, coordination_token=leader_coordination_token(factory, effect.run_id)
    )
    with db.read_only_connection() as conn:
        member = conn.execute(select(sink_effect_members_table)).one()
    assert (member.member_state, member.prepared_disposition, member.reason_hash) == ("finalized", "diverted", "d" * 64)


def test_response_lost_exact_retry_is_idempotent_and_returned_attempt_cannot_be_reclassified(db_factory):
    db, factory = db_factory
    effect, _members, lease = _prepared(factory, count=1)
    repo = factory.execution.sink_effects
    attempt = _attempt(factory, effect, lease)
    token = leader_coordination_token(factory, effect.run_id)
    first = repo.mark_response_lost(attempt.attempt_id, coordination_token=token)
    before = _durable_state(db)
    assert repo.mark_response_lost(attempt.attempt_id, coordination_token=token) == first
    assert _durable_state(db) == before
    returned = _attempt(factory, effect, lease)
    _return(factory, effect, returned, _commit_result())
    before = _durable_state(db)
    with pytest.raises(LandscapeRecordError, match="cannot become response-lost"):
        repo.mark_response_lost(returned.attempt_id, coordination_token=token)
    assert _durable_state(db) == before


@pytest.mark.parametrize("action", [SinkEffectAttemptAction.INSPECT, SinkEffectAttemptAction.COMMIT])
def test_attempt_action_requires_compatible_effect_state(db_factory, action):
    db, factory = db_factory
    effect = _reserved(factory)
    repo = factory.execution.sink_effects
    if action is SinkEffectAttemptAction.INSPECT:
        claim = _claim(factory, effect.effect_id, run_id=effect.run_id)
        repo.complete_plan(
            effect.effect_id, _plan(effect.effect_id), claim=claim, coordination_token=leader_coordination_token(factory, effect.run_id)
        )
        lease = repo.acquire_lease(
            effect.effect_id,
            owner="worker-a",
            ttl=timedelta(seconds=30),
            coordination_token=leader_coordination_token(factory, effect.run_id),
        )
        generation = lease.generation
        message = "reserved or prepared"
    else:
        generation = effect.generation
        message = "in-flight"
    token = leader_coordination_token(factory, effect.run_id)
    before = _durable_state(db)
    with pytest.raises(LandscapeRecordError, match=message):
        repo.begin_attempt(
            SinkEffectAttemptRequest(
                effect_id=effect.effect_id,
                member_ordinal=None,
                generation=generation,
                action=action,
                call_kind=CallType.FILESYSTEM,
                request_hash="a" * 64,
            ),
            coordination_token=token,
        )
    assert _durable_state(db) == before


def test_no_publication_plan_cannot_begin_commit(db_factory):
    db, factory = db_factory
    effect = _reserved(factory)
    repo = factory.execution.sink_effects
    claim = _claim(factory, effect.effect_id, run_id=effect.run_id)
    repo.complete_plan(
        effect.effect_id,
        replace(
            _plan(effect.effect_id),
            descriptor_mode=SinkEffectDescriptorMode.NO_PUBLICATION,
            expected_descriptor=_descriptor(),
            safe_evidence={"publication_kind": "virtual"},
        ),
        claim=claim,
        coordination_token=leader_coordination_token(factory, effect.run_id),
    )
    lease = repo.acquire_lease(
        effect.effect_id, owner="worker-a", ttl=timedelta(seconds=30), coordination_token=leader_coordination_token(factory, effect.run_id)
    )
    before = _durable_state(db)
    with pytest.raises(LandscapeRecordError, match="no-publication"):
        _attempt(factory, effect, lease)
    assert _durable_state(db) == before


@pytest.mark.parametrize(
    "owner, ttl, message",
    [
        ("", timedelta(seconds=30), "non-empty"),
        ("w" * 129, timedelta(seconds=30), "128 characters"),
        ("worker-a", timedelta(0), "positive"),
        ("worker-a", timedelta(seconds=-1), "positive"),
    ],
)
def test_invalid_preparation_identity_or_duration_has_no_durable_effect(db_factory, owner, ttl, message):
    db, factory = db_factory
    effect = _reserved(factory)
    token = leader_coordination_token(factory, effect.run_id)
    before = _durable_state(db)
    with pytest.raises(ValueError, match=message):
        factory.execution.sink_effects.claim_preparation(effect.effect_id, owner=owner, ttl=ttl, coordination_token=token)
    assert _durable_state(db) == before


def test_preparation_claim_cannot_write_another_runs_effect(db_factory):
    db, factory = db_factory
    effect = _reserved(factory)
    other_effect = _reserved(factory)
    token = leader_coordination_token(factory, other_effect.run_id)
    before = _durable_state(db)
    with pytest.raises(LandscapeRecordError, match="not the coordination token"):
        factory.execution.sink_effects.claim_preparation(
            effect.effect_id, owner="worker-a", ttl=timedelta(seconds=30), coordination_token=token
        )
    assert _durable_state(db) == before


def test_missing_effect_cannot_be_claimed_and_has_no_lease_advice(db_factory):
    db, factory = db_factory
    effect = _reserved(factory)
    repo = factory.execution.sink_effects
    token = leader_coordination_token(factory, effect.run_id)
    before = _durable_state(db)
    with pytest.raises(LandscapeRecordError, match="does not exist"):
        repo.claim_preparation("e" * 64, owner="worker-a", ttl=timedelta(seconds=30), coordination_token=token)
    assert repo.lease_validity_seconds("e" * 64) is None
    assert repo.lease_validity_seconds(effect.effect_id) is None
    with pytest.raises(ValueError, match="digest"):
        repo.lease_validity_seconds("invalid-digest")
    assert _durable_state(db) == before


def test_attempt_requires_member_in_its_own_effect(db_factory):
    db, factory = db_factory
    effect, _members, lease = _prepared(factory, count=1)
    before = _durable_state(db)
    with pytest.raises(LandscapeRecordError, match="missing member"):
        _attempt(factory, effect, lease, member_ordinal=1)
    assert _durable_state(db) == before


def test_replacing_successor_cannot_prepare_before_predecessor_finalization(db_factory):
    from tests.unit.core.landscape.test_sink_effect_reservation import _pipeline_members, _pipeline_request

    db, factory = db_factory
    run_id, sink_id, members = _pipeline_members(factory, 2)
    repo = factory.execution.sink_effects
    token = leader_coordination_token(factory, run_id)
    first = repo.reserve(_pipeline_request(run_id, sink_id, members[:1], replacing_target=True), coordination_token=token).new_effect
    successor = repo.reserve(_pipeline_request(run_id, sink_id, members[1:], replacing_target=True), coordination_token=token).new_effect
    assert first is not None and successor is not None
    assert successor.predecessor_effect_id == first.effect_id
    before = _durable_state(db)
    with pytest.raises(LandscapeRecordError, match="predecessor must be finalized"):
        repo.claim_preparation(successor.effect_id, owner="worker-a", ttl=timedelta(seconds=30), coordination_token=token)
    assert _durable_state(db) == before
