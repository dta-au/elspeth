"""Original-plan custody and parity at the real fenced interpretation writer."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import structlog
from sqlalchemy import insert, select, update
from sqlalchemy.pool import StaticPool

from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind, InterpretationSurfaceOrigin
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.repository import PendingInterpretationWritePlan, _RepositoryInterpretationMutations
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import approvals_table, composition_states_table, interpretation_events_table, sessions_table
from elspeth.web.sessions.pending_interpretation import (
    _PreparedPendingInterpretation,
    _SessionPendingInterpretationPlanner,
    reprove_pending_interpretation_plan,
)
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.composition_state import composition_state_data
from tests.unit.web.sessions.session_test_authority import FencedSessionServiceHarness
from tests.unit.web.sessions.test_interpretation_events_service import (
    _compose_operation,
    _insert_session,
    _save_composition_state,
    _structured_llm_node,
)


@pytest.fixture
def engine():
    instance = create_session_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    initialize_session_schema(instance)
    yield instance
    instance.dispose()


@pytest.fixture
def service(engine):
    return FencedSessionServiceHarness(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))


async def _seed(service, engine, *, disabled: bool):
    sid = uuid4()
    with engine.begin() as conn:
        _insert_session(conn, str(sid))
        conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(interpretation_review_disabled=disabled))
    saved = await _save_composition_state(
        service,
        sid,
        composition_state_data(nodes=[_structured_llm_node()], edges=[], outputs=[]),
        provenance="tool_call",
    )
    return sid, saved


async def _prepare(service, sid, saved, context):
    prepared = await service._prepare_or_create_pending_interpretation_event(
        session_id=sid,
        composition_state_id=saved.id,
        affected_node_id="llm_transform_1",
        tool_call_id=f"plan:{uuid4()}",
        user_term="cool",
        kind=InterpretationKind.VAGUE_TERM,
        llm_draft="A draft of cool",
        model_identifier="test-model",
        model_version="test-version",
        provider="test",
        composer_skill_hash="a" * 64,
        surface_origin=InterpretationSurfaceOrigin.COMPOSER_LLM,
        session_operation_context=context,
        _prepare_only=True,
    )
    assert type(prepared) is _PreparedPendingInterpretation
    return prepared


def _plan(transaction, prepared):
    writer = transaction.interpretations
    assert type(writer) is _RepositoryInterpretationMutations
    return writer.plan_pending(prepared.command, prepared.validator)


def _apply(transaction, plan):
    writer = transaction.interpretations
    assert type(writer) is _RepositoryInterpretationMutations
    return writer.apply_pending_plan(plan)


def _rows(engine, sid):
    with engine.connect() as conn:
        return (
            tuple(conn.execute(select(interpretation_events_table).where(interpretation_events_table.c.session_id == str(sid))).all()),
            tuple(conn.execute(select(composition_states_table).where(composition_states_table.c.session_id == str(sid))).all()),
            tuple(conn.execute(select(approvals_table).where(approvals_table.c.session_id == str(sid))).all()),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [False, True])
async def test_pending_plan_has_no_dml_and_applies_canonical_decision(service, engine, disabled):
    sid, saved = await _seed(service, engine, disabled=disabled)
    async with _compose_operation(service, sid) as context:
        prepared = await _prepare(service, sid, saved, context)
        before = _rows(engine, sid)
        plan = service.session_operation_authority.mutate(context, lambda transaction: _plan(transaction, prepared))
        assert type(plan) is PendingInterpretationWritePlan
        assert _rows(engine, sid) == before
        assert not plan.requires_approval_supersession
        canonical = _SessionPendingInterpretationPlanner.plan(
            prepared.command,
            plan.policy_plan.snapshot,
            prepared.validator,
            appended_state_id=plan.policy_plan.appended_state_id,
        )
        assert canonical == plan.policy_plan.decision
        reprove_pending_interpretation_plan(plan.policy_plan)
        assert _rows(engine, sid) == before
        result = service.session_operation_authority.mutate(context, lambda transaction: _apply(transaction, plan))
        assert result.event.id == canonical.result_event_id
        assert result.event.choice is (InterpretationChoice.OPTED_OUT if disabled else InterpretationChoice.PENDING)
        assert result.produced_state == plan.appended_record
        after = _rows(engine, sid)
        with pytest.raises(AuditIntegrityError, match="source changed"):
            service.session_operation_authority.mutate(context, lambda transaction: _apply(transaction, plan))
        assert _rows(engine, sid) == after


@pytest.mark.asyncio
async def test_pending_plan_rejects_copied_or_changed_source_before_dml(service, engine):
    sid, saved = await _seed(service, engine, disabled=False)
    async with _compose_operation(service, sid) as context:
        prepared = await _prepare(service, sid, saved, context)
        plan = service.session_operation_authority.mutate(context, lambda transaction: _plan(transaction, prepared))
        before = _rows(engine, sid)
        with pytest.raises(AuditIntegrityError, match="original fenced plan"):
            service.session_operation_authority.mutate(context, lambda transaction: _apply(transaction, replace(plan)))
        assert _rows(engine, sid) == before
        with engine.begin() as conn:
            conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(interpretation_review_disabled=True))
        with pytest.raises(AuditIntegrityError, match="source changed"):
            service.session_operation_authority.mutate(context, lambda transaction: _apply(transaction, plan))
        assert _rows(engine, sid) == before


@pytest.mark.asyncio
async def test_opt_out_plan_exposes_open_approval_without_mutation(service, engine):
    sid, saved = await _seed(service, engine, disabled=True)
    approval_id = str(uuid4())
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="plan-approver")
        conn.execute(
            insert(approvals_table).values(
                approval_id=approval_id,
                session_id=str(sid),
                state_id=str(saved.id),
                binding_json={},
                requested_by_identity_id="alice",
                approver_identity_id="plan-approver",
                requested_at=datetime.now(UTC),
            )
        )
    async with _compose_operation(service, sid) as context:
        prepared = await _prepare(service, sid, saved, context)
        before = _rows(engine, sid)
        plan = service.session_operation_authority.mutate(context, lambda transaction: _plan(transaction, prepared))
        assert plan.requires_approval_supersession
        assert plan.open_approval_ids == (approval_id,)
        assert plan.appended_record is not None
        assert _rows(engine, sid) == before
        # Inspection grants no V4 refusal/approval and must never call the
        # legacy cross-store audit writer. Application is deliberately absent.


@pytest.mark.asyncio
async def test_pure_reproof_rejects_changed_validation_candidate(service, engine):
    sid, saved = await _seed(service, engine, disabled=True)
    async with _compose_operation(service, sid) as context:
        prepared = await _prepare(service, sid, saved, context)
        plan = service.session_operation_authority.mutate(context, lambda transaction: _plan(transaction, prepared))
        observation = plan.policy_plan.validation
        assert observation is not None
        changed = replace(observation, candidate=replace(observation.candidate, digest="0" * 64))
        with pytest.raises(AuditIntegrityError, match="original validation candidate"):
            reprove_pending_interpretation_plan(replace(plan.policy_plan, validation=changed))


@pytest.mark.asyncio
async def test_opt_out_plan_pins_reconciliation_and_dead_site_retirements(service, engine):
    sid, saved = await _seed(service, engine, disabled=False)
    async with _compose_operation(service, sid) as context:
        prepared = await _prepare(service, sid, saved, context)
        first = service.session_operation_authority.mutate(context, lambda transaction: _plan(transaction, prepared))
        original = service.session_operation_authority.mutate(context, lambda transaction: _apply(transaction, first))
        zombie_id = str(uuid4())
        with engine.begin() as conn:
            existing = conn.execute(
                select(interpretation_events_table).where(interpretation_events_table.c.id == str(original.event.id))
            ).one()
            # A historical pending card whose node no longer exists is exactly
            # the dead-site sweep's input; retain every other real row field.
            zombie = dict(existing._mapping)
            zombie.update(id=zombie_id, affected_node_id="removed-node", tool_call_id=f"historical:{uuid4()}")
            conn.execute(insert(interpretation_events_table).values(**zombie))
            conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(interpretation_review_disabled=True))
        before = _rows(engine, sid)
        next_prepared = await _prepare(service, sid, saved, context)
        plan = service.session_operation_authority.mutate(context, lambda transaction: _plan(transaction, next_prepared))
        assert original.event.id in plan.policy_plan.decision.superseded_event_ids
        assert plan.retired_dead_site_ids == (zombie_id,)
        assert _rows(engine, sid) == before
        service.session_operation_authority.mutate(context, lambda transaction: _apply(transaction, plan))
        with engine.connect() as conn:
            choices = dict(
                conn.execute(
                    select(interpretation_events_table.c.id, interpretation_events_table.c.choice).where(
                        interpretation_events_table.c.id.in_([str(original.event.id), zombie_id])
                    )
                ).all()
            )
        assert choices == {str(original.event.id): InterpretationChoice.SUPERSEDED.value, zombie_id: InterpretationChoice.SUPERSEDED.value}


@pytest.mark.asyncio
async def test_original_orphan_observation_retains_sql_cohort_and_refuses_copy(service, engine):
    from elspeth.web.composer.interpretation_surfacing import InterpretationSurfacing, require_original_pending_review_observation
    from elspeth.web.sessions.converters import state_from_record

    sid, saved = await _seed(service, engine, disabled=False)
    state = state_from_record(saved)
    owner = InterpretationSurfacing(
        sessions_service=service,
        per_term_cap=3,
        per_session_day_cap=20,
        model_identifier="test-model",
        provider="test",
        composer_skill_hash="a" * 64,
    )
    before = await owner.observe_pending_interpretation_review_sites(state, session_id=str(sid))
    assert before.events == ()
    assert before.missing_sites == (("llm_transform_1", "cool", InterpretationKind.VAGUE_TERM),)
    async with _compose_operation(service, sid) as context:
        prepared = await _prepare(service, sid, saved, context)
        plan = service.session_operation_authority.mutate(context, lambda transaction: _plan(transaction, prepared))
        event = service.session_operation_authority.mutate(context, lambda transaction: _apply(transaction, plan)).event
    observed = await owner.observe_pending_interpretation_review_sites(state, session_id=str(sid))
    assert observed.events == (event,)
    assert observed.missing_sites == ()
    assert observed.source_sha256 != before.source_sha256
    require_original_pending_review_observation(observed, owner=owner, state=state, session_id=str(sid))
    with pytest.raises(AuditIntegrityError, match="original SQL cohort"):
        require_original_pending_review_observation(replace(observed), owner=owner, state=state, session_id=str(sid))
