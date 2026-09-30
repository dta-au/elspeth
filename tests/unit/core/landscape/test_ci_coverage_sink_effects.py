"""Sink-effect refusal proofs preserve the atomic publication audit boundary."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.sql.dml import Update

from elspeth.contracts import CallType
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.sink_effects import (
    SinkEffectAttemptAction,
    SinkEffectAttemptRequest,
    SinkEffectAttemptResult,
    SinkEffectAttemptState,
    SinkEffectCommitResult,
    SinkEffectDescriptorMode,
    SinkEffectFinalizationMember,
    SinkEffectFinalizeRequest,
    SinkEffectInputKind,
    SinkEffectMemberCandidate,
    SinkEffectReconcileKind,
    SinkEffectReconcileResult,
    SinkEffectRole,
    SinkEffectState,
)
from elspeth.core.canonical import canonical_json
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.execution.sink_effect_attempt_results import encode_sink_effect_returned_result
from elspeth.core.landscape.execution.sink_effect_identity import compute_sink_effect_target_hash, resolve_sink_effect_members
from elspeth.core.landscape.execution.sink_effect_reservation import SinkEffectReservationRequest
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import (
    artifacts_table,
    calls_table,
    node_states_table,
    operations_table,
    sink_effect_attempts_table,
    sink_effect_members_table,
    sink_effect_streams_table,
    sink_effects_table,
    token_outcomes_table,
)
from tests.fixtures.landscape import expire_sink_effect_lease, leader_coordination_token, make_factory, make_landscape_db
from tests.unit.core.landscape.test_sink_effect_finalization import _descriptor, _prepared, _request
from tests.unit.core.landscape.test_sink_effect_lifecycle import _claim, _plan, _reserved
from tests.unit.core.landscape.test_sink_effect_reservation import _insert_snapshot, _pipeline_members, _pipeline_request


@pytest.fixture
def db_factory() -> Iterator[tuple[LandscapeDB, RecorderFactory]]:
    db = make_landscape_db()
    try:
        yield db, make_factory(db)
    finally:
        db.close()


def _assert_no_finalization(db: LandscapeDB, effect_id: str) -> None:
    """A refusal rolls back state completion as well as artifact/outcome writes."""
    with db.read_only_connection() as conn:
        assert conn.scalar(select(func.count()).select_from(artifacts_table)) == 0
        assert conn.scalar(select(func.count()).select_from(token_outcomes_table)) == 0
        assert conn.scalar(select(sink_effects_table.c.state).where(sink_effects_table.c.effect_id == effect_id)) != "finalized"
        assert conn.scalar(select(operations_table.c.status).where(operations_table.c.sink_effect_id == effect_id)) == "open"
        assert conn.scalar(select(func.count()).select_from(node_states_table).where(node_states_table.c.status == "completed")) == 0


def test_explicit_reservation_matches_request_and_refuses_mixed_arguments(db_factory: tuple[LandscapeDB, RecorderFactory]) -> None:
    db, factory = db_factory
    run_id, sink_id, members = _pipeline_members(factory, 1)
    request = _pipeline_request(run_id, sink_id, members)
    token = leader_coordination_token(factory, run_id)
    repo = factory.execution.sink_effects
    with pytest.raises(TypeError, match="cannot be combined"):
        repo.reserve(request, sink_node_id=sink_id, coordination_token=token)
    with pytest.raises(TypeError, match="explicit reservation requires"):
        repo.reserve(sink_node_id=sink_id, coordination_token=token)
    reserved = repo.reserve(
        sink_node_id=sink_id,
        role=request.role,
        input_kind=request.input_kind,
        requested_target_hash=request.requested_target_hash,
        config_hash=request.config_hash,
        members=members,
        coordination_token=token,
    )
    assert reserved.new_effect is not None
    assert repo.reserve(request, coordination_token=token).open_effect_ids == (reserved.new_effect.effect_id,)
    assert repo.get_members("f" * 64) == ()
    assert repo.get_stream("f" * 64) is None
    assert repo.get_effect("f" * 64) is None
    with db.read_only_connection() as conn:
        assert conn.scalar(select(func.count()).select_from(operations_table)) == 1


@pytest.mark.parametrize("corruption", ["payload", "missing_state", "row_identity", "stream_shape"])
def test_reservation_refuses_divergent_current_witness(db_factory: tuple[LandscapeDB, RecorderFactory], corruption: str) -> None:
    db, factory = db_factory
    run_id, sink_id, members = _pipeline_members(factory, 1)
    request = _pipeline_request(run_id, sink_id, members)
    token = leader_coordination_token(factory, run_id)
    if corruption == "payload":
        with db.engine.begin() as conn:
            conn.execute(update(node_states_table).where(node_states_table.c.token_id == members[0].token_id).values(input_hash="f" * 64))
        message = "payload is divergent"
    elif corruption == "missing_state":
        with db.engine.begin() as conn:
            conn.execute(delete(node_states_table).where(node_states_table.c.token_id == members[0].token_id))
        message = "no current sink-node state"
    elif corruption == "row_identity":
        request = replace(request, members=(replace(members[0], row_id="missing-row"),))
        message = "row/run identity"
    else:
        repo = factory.execution.sink_effects
        original = repo.reserve(request, coordination_token=token).new_effect
        assert original is not None
        with pytest.raises(ValueError, match="divergent replacing-target stream"):
            repo.reserve(replace(request, replacing_target=True), coordination_token=token)
        with db.read_only_connection() as conn:
            assert conn.scalar(select(func.count()).select_from(sink_effects_table)) == 1
            assert conn.scalar(select(func.count()).select_from(sink_effect_streams_table)) == 0
        return
    with pytest.raises(ValueError, match=message):
        factory.execution.sink_effects.reserve(request, coordination_token=token)
    with db.read_only_connection() as conn:
        assert conn.scalar(select(func.count()).select_from(sink_effects_table)) == 0
        assert conn.scalar(select(func.count()).select_from(operations_table)) == 0


def test_stream_bound_members_cannot_be_reused_as_nonreplacing(db_factory: tuple[LandscapeDB, RecorderFactory]) -> None:
    db, factory = db_factory
    run_id, sink_id, members = _pipeline_members(factory, 1)
    request = _pipeline_request(run_id, sink_id, members, replacing_target=True)
    token = leader_coordination_token(factory, run_id)
    first = factory.execution.sink_effects.reserve(request, coordination_token=token)
    with pytest.raises(ValueError, match="stream-bound but request is not replacing"):
        factory.execution.sink_effects.reserve(replace(request, replacing_target=False), coordination_token=token)
    assert first.new_effect is not None
    with db.read_only_connection() as conn:
        assert conn.scalar(select(sink_effect_streams_table.c.next_sequence)) == 1


def test_reservation_and_finalization_reject_another_runs_leader(db_factory: tuple[LandscapeDB, RecorderFactory]) -> None:
    db, factory = db_factory
    effect, members, lease = _prepared(factory, count=1)
    request = _request(factory, effect, members, lease)
    other_run, _sink, _members = _pipeline_members(factory, 1)
    wrong_token = leader_coordination_token(factory, other_run)
    with pytest.raises(ValueError, match="different run"):
        factory.execution.sink_effects.reserve(
            _pipeline_request(effect.run_id, effect.sink_node_id, members), coordination_token=wrong_token
        )
    with pytest.raises(LandscapeRecordError, match="not the coordination token's run"):
        factory.execution.sink_effects.finalize(request, coordination_token=wrong_token)
    with pytest.raises(LandscapeRecordError, match="not the coordination token's run"):
        factory.execution.sink_effects.heartbeat_lease(
            effect.effect_id, owner=lease.owner, generation=lease.generation, ttl=timedelta(seconds=30), coordination_token=wrong_token
        )
    _assert_no_finalization(db, effect.effect_id)


@pytest.mark.parametrize(
    ("evidence", "message"),
    [
        ({"accepted_ordinals": [0]}, "both be present"),
        ({"accepted_ordinals": "0", "diverted_ordinals": []}, "both be lists"),
        ({"accepted_ordinals": [0, 0], "diverted_ordinals": []}, "unique ascending"),
        ({"accepted_ordinals": [True], "diverted_ordinals": []}, "unique ascending"),
        ({"accepted_ordinals": [], "diverted_ordinals": []}, "exactly partition"),
        ({"accepted_ordinals": [], "diverted_ordinals": [0], "diversion_attribution": {}}, "attribution must be a list"),
        ({"accepted_ordinals": [], "diverted_ordinals": [0], "diversion_attribution": [{}]}, "divergent field set"),
        (
            {
                "accepted_ordinals": [],
                "diverted_ordinals": [0],
                "diversion_attribution": [{"ordinal": 0, "reason_hash": "x", "error_hash": "b" * 16}],
            },
            "attribution is invalid",
        ),
    ],
)
def test_malformed_plan_partition_rolls_back_the_plan_and_members(
    db_factory: tuple[LandscapeDB, RecorderFactory], evidence: dict[str, object], message: str
) -> None:
    db, factory = db_factory
    effect = _reserved(factory)
    claim = _claim(factory, effect.effect_id, run_id=effect.run_id)
    with pytest.raises(LandscapeRecordError, match=message):
        factory.execution.sink_effects.complete_plan(
            effect.effect_id,
            replace(_plan(effect.effect_id), safe_evidence=evidence),
            claim=claim,
            coordination_token=leader_coordination_token(factory, effect.run_id),
        )
    reloaded = factory.execution.sink_effects.get_effect(effect.effect_id)
    assert reloaded is not None and reloaded.state is SinkEffectState.RESERVED and reloaded.plan_json is None
    with db.read_only_connection() as conn:
        assert conn.scalar(select(sink_effect_members_table.c.prepared_disposition)) is None


@pytest.mark.parametrize("owner", [" ", "x" * 129])
def test_invalid_preparer_identity_leaves_generation_unclaimed(db_factory: tuple[LandscapeDB, RecorderFactory], owner: str) -> None:
    _db, factory = db_factory
    effect = _reserved(factory)
    with pytest.raises(ValueError, match="lease owner"):
        factory.execution.sink_effects.claim_preparation(
            effect.effect_id, owner=owner, ttl=timedelta(seconds=30), coordination_token=leader_coordination_token(factory, effect.run_id)
        )
    winner = factory.execution.sink_effects.get_effect(effect.effect_id)
    assert winner is not None and winner.generation == 0 and winner.lease_owner is None


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1)])
def test_invalid_preparation_ttl_leaves_generation_unclaimed(db_factory: tuple[LandscapeDB, RecorderFactory], ttl: timedelta) -> None:
    _db, factory = db_factory
    effect = _reserved(factory)
    with pytest.raises(ValueError, match="positive timedelta"):
        factory.execution.sink_effects.claim_preparation(
            effect.effect_id, owner="worker", ttl=ttl, coordination_token=leader_coordination_token(factory, effect.run_id)
        )
    assert factory.execution.sink_effects.lease_validity_seconds(effect.effect_id) is None


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("expired", "lease has expired"),
        ("payload", "input does not match"),
        ("plan_json", "not valid JSON"),
        ("plan_list", "must be an object"),
        ("plan_identity", "disagrees with ledger fields"),
        ("missing_attempt", "requires an exact returned attempt"),
        ("unknown_attempt", "does not belong"),
        ("future_attempt", "not an exact returned result"),
        ("attempt_action", "action disagrees"),
        ("commit_reconcile_kind", "must not claim a reconcile result"),
        ("partition", "exactly partition durable membership"),
    ],
)
def test_finalization_refusal_rolls_back_every_audit_write(
    db_factory: tuple[LandscapeDB, RecorderFactory], mutation: str, message: str
) -> None:
    db, factory = db_factory
    effect, members, lease = _prepared(factory, count=1)
    request = _request(factory, effect, members, lease)
    if mutation == "expired":
        expire_sink_effect_lease(db.engine, effect.effect_id)
    elif mutation == "payload":
        with db.engine.begin() as conn:
            conn.execute(update(node_states_table).values(input_hash="f" * 64))
    elif mutation in {"plan_json", "plan_list", "plan_identity"}:
        value = {
            "plan_json": "{",
            "plan_list": "[]",
            "plan_identity": canonical_json(
                {"effect_id": "f" * 64, "input_kind": effect.input_kind.value, "plan_hash": "a" * 64, "descriptor_mode": "precomputed"}
            ),
        }[mutation]
        with db.engine.begin() as conn:
            conn.execute(update(sink_effects_table).values(plan_json=value))
    elif mutation == "missing_attempt":
        request = replace(request, attempt_id=None)
    elif mutation == "unknown_attempt":
        request = replace(request, attempt_id="f" * 64)
    elif mutation == "future_attempt":
        with db.engine.begin() as conn:
            conn.execute(update(sink_effect_attempts_table).values(generation=lease.generation + 1))
    elif mutation == "attempt_action":
        with db.engine.begin() as conn:
            conn.execute(update(sink_effect_attempts_table).values(action="reconcile"))
    elif mutation == "commit_reconcile_kind":
        request = replace(request, reconcile_kind=SinkEffectReconcileKind.UNKNOWN)
    else:
        request = replace(request, accepted_ordinals=(), members=())
    with pytest.raises(LandscapeRecordError, match=message):
        factory.execution.sink_effects.finalize(request, coordination_token=leader_coordination_token(factory, effect.run_id))
    _assert_no_finalization(db, effect.effect_id)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("generation", "different generation"),
        ("descriptor", "divergent descriptor"),
        ("publication", "divergent publication evidence"),
        ("artifact", "artifact winner is divergent"),
        ("outcome", "missing member state/outcome"),
        ("attempt", "original returned attempt"),
    ],
)
def test_finalized_retry_refuses_to_rewrite_a_divergent_winner(
    db_factory: tuple[LandscapeDB, RecorderFactory], mutation: str, message: str
) -> None:
    db, factory = db_factory
    effect, members, lease = _prepared(factory, count=1)
    request = _request(factory, effect, members, lease)
    first = factory.execution.sink_effects.finalize(request, coordination_token=leader_coordination_token(factory, effect.run_id))
    if mutation == "generation":
        request = replace(request, generation=request.generation + 1)
    elif mutation == "publication":
        request = replace(
            request, publication_evidence_kind="reconciled", reconcile_kind=SinkEffectReconcileKind.APPLIED_WITH_EXACT_DESCRIPTOR
        )
    elif mutation == "attempt":
        request = replace(request, attempt_id=None)
    else:
        with db.engine.begin() as conn:
            if mutation == "descriptor":
                conn.execute(update(sink_effects_table).values(result_descriptor_hash="f" * 64))
            elif mutation == "artifact":
                conn.execute(update(artifacts_table).values(content_hash="f" * 64))
            else:
                conn.execute(delete(token_outcomes_table))
    with pytest.raises(LandscapeRecordError, match=message):
        factory.execution.sink_effects.finalize(request, coordination_token=leader_coordination_token(factory, effect.run_id))
    with db.read_only_connection() as conn:
        assert conn.scalar(select(func.count()).select_from(artifacts_table)) == 1
        assert conn.scalar(select(artifacts_table.c.artifact_id)) == first.artifact.artifact_id
        assert conn.scalar(select(operations_table.c.status)) == "completed"


def _member_attempt(factory: RecorderFactory, effect, lease, result, *, ordinal: int | None = 0, returned: bool = True):
    action = SinkEffectAttemptAction.COMMIT if isinstance(result, SinkEffectCommitResult) else SinkEffectAttemptAction.RECONCILE
    token = leader_coordination_token(factory, effect.run_id)
    attempt = factory.execution.sink_effects.begin_attempt(
        SinkEffectAttemptRequest(
            effect_id=effect.effect_id,
            member_ordinal=ordinal,
            generation=lease.generation,
            action=action,
            call_kind=CallType.FILESYSTEM,
            request_hash="c" * 64,
        ),
        coordination_token=token,
    )
    if returned:
        factory.execution.sink_effects.record_attempt_result(
            SinkEffectAttemptResult(attempt_id=attempt.attempt_id, evidence=encode_sink_effect_returned_result(result), latency_ms=1.0),
            coordination_token=token,
        )
    return attempt


@pytest.mark.parametrize("kind", [SinkEffectReconcileKind.UNKNOWN, SinkEffectReconcileKind.NOT_APPLIED])
def test_member_reconciliation_records_uncertainty_without_finalizing(
    db_factory: tuple[LandscapeDB, RecorderFactory], kind: SinkEffectReconcileKind
) -> None:
    _db, factory = db_factory
    effect, _members, lease = _prepared(factory, count=1)
    result = SinkEffectReconcileResult(kind=kind, evidence={"probe": kind.value})
    attempt = _member_attempt(factory, effect, lease, result)
    factory.execution.sink_effects.complete_member_result(
        attempt.attempt_id, result, lease=lease, coordination_token=leader_coordination_token(factory, effect.run_id)
    )
    member = factory.execution.sink_effects.get_members(effect.effect_id)[0]
    expected = SinkEffectState.IN_FLIGHT if kind is SinkEffectReconcileKind.UNKNOWN else SinkEffectState.PREPARED
    assert member.member_state is expected
    assert member.descriptor_hash is None
    assert member.evidence_hash is not None
    assert factory.execution.sink_effects.get_effect(effect.effect_id).state is SinkEffectState.IN_FLIGHT


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("lease", "stale lease authority"),
        ("group_attempt", "member-scoped attempt"),
        ("intent", "returned member attempt"),
        ("future", "impossible future generation"),
        ("evidence", "differs from the durable returned attempt"),
    ],
)
def test_member_completion_requires_the_returned_result_and_current_owner(
    db_factory: tuple[LandscapeDB, RecorderFactory], mutation: str, message: str
) -> None:
    db, factory = db_factory
    effect, _members, lease = _prepared(factory, count=1)
    result = SinkEffectCommitResult(descriptor=_descriptor(), evidence={"winner": "exact"}, accepted_ordinals=(0,), diverted_ordinals=())
    attempt = _member_attempt(
        factory, effect, lease, result, ordinal=None if mutation == "group_attempt" else 0, returned=mutation != "intent"
    )
    if mutation == "lease":
        lease = replace(lease, owner="other-owner")
    elif mutation == "future":
        with db.engine.begin() as conn:
            conn.execute(update(sink_effect_attempts_table).values(generation=lease.generation + 1))
    elif mutation == "evidence":
        result = replace(result, evidence={"winner": "different"})
    with pytest.raises(LandscapeRecordError, match=message):
        factory.execution.sink_effects.complete_member_result(
            attempt.attempt_id, result, lease=lease, coordination_token=leader_coordination_token(factory, effect.run_id)
        )
    assert factory.execution.sink_effects.get_members(effect.effect_id)[0].member_state is not SinkEffectState.FINALIZED


@pytest.mark.parametrize(
    ("attribution", "message"),
    [
        (None, "requires diversion attribution"),
        ([{}], "attribution is invalid"),
        ([{"ordinal": 1, "reason_hash": "a" * 64, "error_hash": "b" * 16}], "does not cover the member ordinal"),
    ],
)
def test_unattributed_member_diversion_is_refused(
    db_factory: tuple[LandscapeDB, RecorderFactory], attribution: object, message: str
) -> None:
    _db, factory = db_factory
    effect, _members, lease = _prepared(factory, count=1)
    evidence = {} if attribution is None else {"diversion_attribution": attribution}
    result = SinkEffectCommitResult(descriptor=_descriptor(), evidence=evidence, accepted_ordinals=(), diverted_ordinals=(0,))
    attempt = _member_attempt(factory, effect, lease, result)
    with pytest.raises(LandscapeRecordError, match=message):
        factory.execution.sink_effects.complete_member_result(
            attempt.attempt_id, result, lease=lease, coordination_token=leader_coordination_token(factory, effect.run_id)
        )
    member = factory.execution.sink_effects.get_members(effect.effect_id)[0]
    assert member.prepared_disposition == "accepted" and member.reason_hash is None


def test_response_loss_and_returned_attempt_are_mutually_exclusive(db_factory: tuple[LandscapeDB, RecorderFactory]) -> None:
    db, factory = db_factory
    effect, _members, lease = _prepared(factory, count=1)
    result = SinkEffectCommitResult(descriptor=_descriptor(), evidence={"winner": "exact"}, accepted_ordinals=(0,), diverted_ordinals=())
    attempt = _member_attempt(factory, effect, lease, result, returned=False)
    repo = factory.execution.sink_effects
    token = leader_coordination_token(factory, effect.run_id)
    first = repo.mark_response_lost(attempt.attempt_id, coordination_token=token)
    assert repo.mark_response_lost(attempt.attempt_id, coordination_token=token) == first
    with pytest.raises(LandscapeRecordError, match="cannot return from state"):
        repo.record_attempt_result(
            SinkEffectAttemptResult(attempt_id=attempt.attempt_id, evidence=encode_sink_effect_returned_result(result), latency_ms=1.0),
            coordination_token=token,
        )
    second = _member_attempt(factory, effect, lease, result)
    with pytest.raises(LandscapeRecordError, match="cannot become response-lost"):
        repo.mark_response_lost(second.attempt_id, coordination_token=token)
    with pytest.raises(LandscapeRecordError, match="divergent from the durable winner"):
        repo.record_attempt_result(
            SinkEffectAttemptResult(attempt_id=second.attempt_id, evidence=encode_sink_effect_returned_result(result), latency_ms=2.0),
            coordination_token=token,
        )
    assert first.state is SinkEffectAttemptState.RESPONSE_LOST
    with db.read_only_connection() as conn:
        assert conn.scalar(select(func.count()).select_from(calls_table)) == 2


@pytest.mark.parametrize(
    ("target", "error", "message"),
    [
        ({"value": 2**54}, ValueError, "safe range"),
        ({"value": float("inf")}, ValueError, "must be finite"),
        ({"value": SinkEffectRole.PRIMARY}, TypeError, "exact wire strings"),
        ({1: "value"}, TypeError, "exact string keys"),
        ({"value": {1, 2}}, TypeError, "unsupported identity value"),
    ],
)
def test_noncanonical_target_identity_cannot_enter_the_ledger(target, error, message: str) -> None:
    with pytest.raises(error, match=message):
        compute_sink_effect_target_hash(target)


def test_target_identity_preserves_finite_fractional_configuration() -> None:
    assert compute_sink_effect_target_hash({"path": "out", "rate": 0.5}) != compute_sink_effect_target_hash({"path": "out", "rate": 1.5})


def test_resolving_members_refuses_empty_duplicate_and_missing_candidates(db_factory: tuple[LandscapeDB, RecorderFactory]) -> None:
    _db, factory = db_factory
    _run_id, _sink_id, members = _pipeline_members(factory, 1)
    candidate = SinkEffectMemberCandidate(token_id=members[0].token_id, row=members[0].row)
    with pytest.raises(ValueError, match="non-empty"):
        resolve_sink_effect_members(factory, [])
    with pytest.raises(AuditIntegrityError, match="IDs must be unique"):
        resolve_sink_effect_members(factory, [candidate, candidate])
    with pytest.raises(AuditIntegrityError, match="is missing"):
        resolve_sink_effect_members(factory, [replace(candidate, token_id="absent")])
    assert resolve_sink_effect_members(factory, [candidate])[0].token_id == members[0].token_id


@pytest.mark.parametrize("kind", ["missing", "stale", "response_lost"])
def test_returned_result_cannot_reanimate_a_missing_or_superseded_attempt(
    db_factory: tuple[LandscapeDB, RecorderFactory], kind: str
) -> None:
    db, factory = db_factory
    effect, _members, lease = _prepared(factory, count=1)
    result = SinkEffectCommitResult(descriptor=_descriptor(), evidence={}, accepted_ordinals=(0,), diverted_ordinals=())
    attempt = _member_attempt(factory, effect, lease, result, returned=False)
    repo = factory.execution.sink_effects
    token = leader_coordination_token(factory, effect.run_id)
    if kind == "missing":
        attempt_id = "f" * 64
        message = "does not exist"
    elif kind == "stale":
        attempt_id = attempt.attempt_id
        expire_sink_effect_lease(db.engine, effect.effect_id)
        repo.takeover_expired(effect.effect_id, owner="replacement", ttl=timedelta(seconds=30), coordination_token=token)
        message = "stale generation"
        with pytest.raises(LandscapeRecordError, match=message):
            repo.mark_response_lost(attempt_id, coordination_token=token)
    else:
        attempt_id = attempt.attempt_id
        repo.mark_response_lost(attempt_id, coordination_token=token)
        message = "cannot return"
    with pytest.raises(LandscapeRecordError, match=message):
        repo.record_attempt_result(
            SinkEffectAttemptResult(attempt_id=attempt_id, evidence=encode_sink_effect_returned_result(result), latency_ms=1.0),
            coordination_token=token,
        )
    assert repo.get_attempts(effect.effect_id)[0].state is not SinkEffectAttemptState.RETURNED


def _no_publication(factory: RecorderFactory):
    from elspeth.contracts import TerminalOutcome, TerminalPath

    effect = _reserved(factory)
    claim = _claim(factory, effect.effect_id, run_id=effect.run_id)
    plan = replace(
        _plan(effect.effect_id),
        descriptor_mode=SinkEffectDescriptorMode.NO_PUBLICATION,
        expected_descriptor=_descriptor(),
        safe_evidence={"publication_kind": "inherited"},
    )
    factory.execution.sink_effects.complete_plan(
        effect.effect_id, plan, claim=claim, coordination_token=leader_coordination_token(factory, effect.run_id)
    )
    request = SinkEffectFinalizeRequest(
        effect_id=effect.effect_id,
        lease_owner=None,
        generation=claim.generation,
        descriptor=_descriptor(),
        publication_performed=False,
        publication_evidence_kind="inherited",
        accepted_ordinals=(0,),
        diverted_ordinals=(),
        evidence={"inherited_from": "verified-previous-publication"},
        members=(
            SinkEffectFinalizationMember(
                ordinal=0,
                output_data={"ordinal": 0},
                duration_ms=1.0,
                outcome=TerminalOutcome.SUCCESS,
                path=TerminalPath.DEFAULT_FLOW,
                sink_name="sink",
            ),
        ),
    )
    return effect, request


def test_no_publication_retries_return_one_artifact_without_any_external_attempt(db_factory: tuple[LandscapeDB, RecorderFactory]) -> None:
    db, factory = db_factory
    effect, request = _no_publication(factory)
    repo = factory.execution.sink_effects
    token = leader_coordination_token(factory, effect.run_id)
    first = repo.finalize(request, coordination_token=token)
    assert repo.finalize(request, coordination_token=token) == first
    assert first.artifact.publication_performed is False
    assert first.artifact.publication_evidence_kind == "inherited"
    with pytest.raises(LandscapeRecordError, match="cannot be taken over"):
        repo.takeover_expired(effect.effect_id, owner="other", ttl=timedelta(seconds=30), coordination_token=token)
    with pytest.raises(LandscapeRecordError, match="winner cannot carry an attempt"):
        repo.finalize(replace(request, attempt_id="f" * 64), coordination_token=token)
    with db.read_only_connection() as conn:
        assert conn.scalar(select(func.count()).select_from(sink_effect_attempts_table)) == 0
        assert conn.scalar(select(func.count()).select_from(calls_table)) == 0
        assert conn.scalar(select(func.count()).select_from(artifacts_table)) == 1


@pytest.mark.parametrize("violation", ["owner", "attempt", "published"])
def test_no_publication_cannot_claim_external_publication_authority(
    db_factory: tuple[LandscapeDB, RecorderFactory], violation: str
) -> None:
    db, factory = db_factory
    effect, request = _no_publication(factory)
    if violation == "owner":
        request = replace(request, lease_owner="worker-a")
        message = "must not claim lease ownership"
    elif violation == "attempt":
        request = replace(request, attempt_id="f" * 64)
        message = "forbids an external attempt"
    else:
        request = replace(request, publication_performed=True, publication_evidence_kind="returned")
        message = "requires inherited or virtual"
    with pytest.raises(LandscapeRecordError, match=message):
        factory.execution.sink_effects.finalize(request, coordination_token=leader_coordination_token(factory, effect.run_id))
    _assert_no_finalization(db, effect.effect_id)


@pytest.mark.parametrize(
    ("attribution", "message"),
    [
        ({}, "must be a list"),
        ([], "cover every diverted member"),
        ([{"ordinal": 1, "reason_hash": "bad", "error_hash": "b" * 16}], "is invalid"),
    ],
)
def test_result_derived_partition_requires_complete_canonical_diversion_attribution(
    db_factory: tuple[LandscapeDB, RecorderFactory], attribution: object, message: str
) -> None:
    db, factory = db_factory
    effect, members, lease = _prepared(factory, count=2, descriptor_mode=SinkEffectDescriptorMode.RESULT_DERIVED)
    descriptor = _descriptor()
    evidence = {
        "accepted_ordinals": [0],
        "diverted_ordinals": [1],
        "diversion_attribution": attribution,
        "descriptor": {
            "artifact_type": descriptor.artifact_type,
            "content_hash": descriptor.content_hash,
            "metadata": None,
            "path_or_uri": descriptor.path_or_uri,
            "size_bytes": descriptor.size_bytes,
        },
    }
    base = _request(factory, effect, members, lease, evidence=evidence)
    request = replace(base, accepted_ordinals=(0,), diverted_ordinals=(1,), members=(base.members[0],))
    with pytest.raises(LandscapeRecordError, match=message):
        factory.execution.sink_effects.finalize(request, coordination_token=leader_coordination_token(factory, effect.run_id))
    _assert_no_finalization(db, effect.effect_id)


def test_database_rejection_after_artifact_registration_rolls_back_then_retries_atomically(
    db_factory: tuple[LandscapeDB, RecorderFactory],
) -> None:
    db, factory = db_factory
    effect, members, lease = _prepared(factory, count=1)
    request = _request(factory, effect, members, lease)
    rejected: list[str] = []

    def refuse_completion(_conn, statement, _multiparams, _params, _execution_options) -> None:
        if isinstance(statement, Update) and statement.table.name == operations_table.name:
            rejected.append(statement.table.name)
            raise OperationalError("operation completion", {}, RuntimeError("injected database rejection"))

    event.listen(db.engine, "before_execute", refuse_completion)
    try:
        with pytest.raises(LandscapeRecordError, match="database rejected atomic audit write") as caught:
            factory.execution.sink_effects.finalize(request, coordination_token=leader_coordination_token(factory, effect.run_id))
        assert isinstance(caught.value.__cause__, OperationalError)
    finally:
        event.remove(db.engine, "before_execute", refuse_completion)
    assert rejected == ["operations"]
    _assert_no_finalization(db, effect.effect_id)
    winner = factory.execution.sink_effects.finalize(request, coordination_token=leader_coordination_token(factory, effect.run_id))
    assert winner.effect.state is SinkEffectState.FINALIZED
    assert len(winner.outcome_ids) == len(winner.state_ids) == 1


@pytest.mark.parametrize("divergence", ["descriptor", "evidence", "digest"])
def test_member_scoped_finalization_binds_to_the_returned_member_winner(
    db_factory: tuple[LandscapeDB, RecorderFactory], divergence: str
) -> None:
    db, factory = db_factory
    effect, members, lease = _prepared(factory, count=1)
    returned = SinkEffectCommitResult(
        descriptor=_descriptor(content_hash="f" * 64) if divergence == "descriptor" else _descriptor(),
        evidence={"result": "different"} if divergence == "evidence" else {"result": "exact"},
        accepted_ordinals=(0,),
        diverted_ordinals=(),
    )
    member_attempt = _member_attempt(factory, effect, lease, returned)
    request = replace(_request(factory, effect, members, lease), attempt_id=member_attempt.attempt_id)
    if divergence == "digest":
        with db.engine.begin() as conn:
            conn.execute(
                update(sink_effect_attempts_table)
                .where(sink_effect_attempts_table.c.attempt_id == member_attempt.attempt_id)
                .values(evidence_hash="f" * 64)
            )
        message = "evidence is missing or divergent"
    else:
        message = f"{divergence} differs from the returned attempt winner"
    with pytest.raises(LandscapeRecordError, match=message):
        factory.execution.sink_effects.finalize(request, coordination_token=leader_coordination_token(factory, effect.run_id))
    _assert_no_finalization(db, effect.effect_id)


def test_replacing_export_reservation_allocates_one_stream_position_for_idempotent_retries(
    db_factory: tuple[LandscapeDB, RecorderFactory],
) -> None:
    db, factory = db_factory
    run_id, sink_id, _members = _pipeline_members(factory, 1)
    snapshot_id = _insert_snapshot(db, run_id)
    request = SinkEffectReservationRequest(
        run_id=run_id,
        sink_node_id=sink_id,
        role=SinkEffectRole.PRIMARY,
        input_kind=SinkEffectInputKind.AUDIT_EXPORT_SNAPSHOT,
        requested_target_hash="c" * 64,
        config_hash="c" * 64,
        members=(),
        audit_export_snapshot_id=snapshot_id,
        replacing_target=True,
        primary_effect_id=None,
    )
    repo = factory.execution.sink_effects
    token = leader_coordination_token(factory, run_id)
    first = repo.reserve(request, coordination_token=token)
    assert first.new_effect is not None
    second = repo.reserve(request, coordination_token=token)
    assert second.open_effect_ids == (first.new_effect.effect_id,)
    stream = repo.get_stream(first.new_effect.stream_id)
    assert stream is not None and stream.next_sequence == 1 and stream.tail_effect_id == first.new_effect.effect_id
    with pytest.raises(ValueError, match="target identity differs"):
        repo.reserve(replace(request, requested_target_hash="d" * 64, config_hash="d" * 64), coordination_token=token)
    with pytest.raises(ValueError, match="stream-bound but request is not replacing"):
        repo.reserve(replace(request, replacing_target=False), coordination_token=token)
    claim = repo.claim_preparation(first.new_effect.effect_id, owner="exporter", ttl=timedelta(seconds=30), coordination_token=token)
    repo.complete_plan(
        first.new_effect.effect_id,
        replace(
            _plan(first.new_effect.effect_id),
            input_kind=SinkEffectInputKind.AUDIT_EXPORT_SNAPSHOT,
            descriptor_mode=SinkEffectDescriptorMode.NO_PUBLICATION,
            expected_descriptor=_descriptor(),
            safe_evidence={"publication_kind": "inherited"},
        ),
        claim=claim,
        coordination_token=token,
    )
    finalized = repo.finalize(
        SinkEffectFinalizeRequest(
            effect_id=first.new_effect.effect_id,
            lease_owner=None,
            generation=claim.generation,
            descriptor=_descriptor(),
            publication_performed=False,
            publication_evidence_kind="inherited",
            accepted_ordinals=(),
            diverted_ordinals=(),
            evidence={"prior_export": "exact"},
            members=(),
        ),
        coordination_token=token,
    )
    retry = repo.reserve(request, coordination_token=token)
    assert retry.new_effect is None and retry.open_effect_ids == ()
    assert retry.finalized_effect_ids == (finalized.effect.effect_id,)
    stream_after = repo.get_stream(first.new_effect.stream_id)
    assert stream_after is not None and stream_after.next_sequence == 1 and stream_after.head_effect_id == finalized.effect.effect_id


def _export_request(run_id: str, sink_id: str, snapshot_id: str, target_hash: str = "c" * 64) -> SinkEffectReservationRequest:
    return SinkEffectReservationRequest(
        run_id=run_id,
        sink_node_id=sink_id,
        role=SinkEffectRole.PRIMARY,
        input_kind=SinkEffectInputKind.AUDIT_EXPORT_SNAPSHOT,
        requested_target_hash=target_hash,
        config_hash=target_hash,
        members=(),
        audit_export_snapshot_id=snapshot_id,
        replacing_target=True,
        primary_effect_id=None,
    )


@pytest.mark.parametrize("corruption", ["next_sequence", "tail", "self_predecessor"])
def test_export_retry_refuses_constraint_valid_stream_corruption_without_repair(
    db_factory: tuple[LandscapeDB, RecorderFactory], corruption: str
) -> None:
    db, factory = db_factory
    run_id, sink_id, _members = _pipeline_members(factory, 1)
    request = _export_request(run_id, sink_id, _insert_snapshot(db, run_id))
    token = leader_coordination_token(factory, run_id)
    repo = factory.execution.sink_effects
    effect = repo.reserve(request, coordination_token=token).new_effect
    assert effect is not None
    assert repo.reserve(request, coordination_token=token).open_effect_ids == (effect.effect_id,)
    with db.engine.begin() as conn:
        if corruption == "next_sequence":
            conn.execute(update(sink_effect_streams_table).values(next_sequence=0))
        elif corruption == "tail":
            conn.execute(update(sink_effect_streams_table).values(tail_effect_id=None))
        else:
            conn.execute(update(sink_effect_streams_table).values(next_sequence=2))
            conn.execute(update(sink_effects_table).values(stream_sequence=1, predecessor_effect_id=effect.effect_id))
    with pytest.raises(ValueError, match="disagrees with its durable effect winner"):
        repo.reserve(request, coordination_token=token)
    with db.read_only_connection() as conn:
        assert conn.scalar(select(func.count()).select_from(sink_effects_table)) == 1
        assert conn.scalar(select(func.count()).select_from(operations_table)) == 1
        if corruption == "next_sequence":
            assert conn.scalar(select(sink_effect_streams_table.c.next_sequence)) == 0
        elif corruption == "tail":
            assert conn.scalar(select(sink_effect_streams_table.c.tail_effect_id)) is None
        else:
            assert conn.scalar(select(sink_effects_table.c.predecessor_effect_id)) == effect.effect_id


def test_export_retry_refuses_a_missing_stream_without_recreating_it(db_factory: tuple[LandscapeDB, RecorderFactory]) -> None:
    db, factory = db_factory
    run_id, sink_id, _members = _pipeline_members(factory, 1)
    request = _export_request(run_id, sink_id, _insert_snapshot(db, run_id))
    token = leader_coordination_token(factory, run_id)
    repo = factory.execution.sink_effects
    effect = repo.reserve(request, coordination_token=token).new_effect
    assert effect is not None
    # Model damage in the audit store that SQLite's normal FK enforcement
    # prevents. Restoring enforcement does not retroactively mend that damage.
    raw = db.engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("PRAGMA foreign_keys = OFF")
        assert cursor.execute("PRAGMA foreign_keys").fetchone() == (0,)
        cursor.execute("DELETE FROM sink_effect_streams")
        raw.commit()
        cursor.execute("PRAGMA foreign_keys = ON")
        assert cursor.execute("PRAGMA foreign_keys").fetchone() == (1,)
        cursor.close()
    finally:
        raw.close()
    with pytest.raises(ValueError, match="replacing sink effect stream is missing"):
        repo.reserve(request, coordination_token=token)
    with db.read_only_connection() as conn:
        assert conn.scalar(select(func.count()).select_from(sink_effect_streams_table)) == 0
        assert conn.scalar(select(func.count()).select_from(sink_effects_table)) == 1


def test_export_retry_preserves_a_real_pipeline_predecessor_at_nonzero_position(db_factory: tuple[LandscapeDB, RecorderFactory]) -> None:
    db, factory = db_factory
    primary, members, lease = _prepared(factory, count=1, replacing_target=True)
    repo = factory.execution.sink_effects
    token = leader_coordination_token(factory, primary.run_id)
    primary_result = repo.finalize(_request(factory, primary, members, lease), coordination_token=token)
    stream = repo.get_stream(primary.stream_id)
    assert stream is not None
    request = _export_request(primary.run_id, primary.sink_node_id, _insert_snapshot(db, primary.run_id), stream.requested_target_hash)
    exported = repo.reserve(request, coordination_token=token).new_effect
    assert exported is not None
    assert exported.stream_sequence == 1 and exported.predecessor_effect_id == primary_result.effect.effect_id
    assert repo.reserve(request, coordination_token=token).open_effect_ids == (exported.effect_id,)
    stream_after = repo.get_stream(primary.stream_id)
    assert stream_after is not None and stream_after.next_sequence == 2 and stream_after.tail_effect_id == exported.effect_id
