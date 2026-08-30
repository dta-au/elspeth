from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.core.canonical import stable_hash
from elspeth.web.composer.pipeline_planner import PipelinePlanResult
from elspeth.web.composer.pipeline_proposal import AbsentBase, PipelineProposal, PlannerSurface
from elspeth.web.composer.redaction import redact_tool_call_arguments
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.coordination.contracts import SessionOperationContext, SessionOperationFenceLost
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    blobs_table,
    composition_proposals_table,
    composition_states_table,
    proposal_blob_effect_receipts_table,
    proposal_events_table,
    session_operation_fences_table,
)
from elspeth.web.sessions.proposal_blob_effects import blob_row_snapshot_payload
from elspeth.web.sessions.protocol import (
    CompositionStateData,
    CompositionStateProvenance,
    CompositionStateRecord,
    StaleComposeStateError,
)
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry


async def _save_composition_state(
    service: SessionServiceImpl,
    session_id: UUID,
    state: CompositionStateData,
    *,
    provenance: CompositionStateProvenance,
) -> CompositionStateRecord:
    context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        return await service.save_composition_state(
            session_id,
            state,
            provenance=provenance,
            session_operation_context=context,
        )
    finally:
        await service._run_sync(service.session_operation_authority.release, context)


@pytest.mark.asyncio
async def test_create_composition_proposal_accepts_live_compose_context() -> None:
    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    service = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.composer-proposal-authority"),
    )
    session_id = (await service.create_session("alice", "Composer proposal authority", "local")).id

    context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        proposal = await service.create_composition_proposal(
            session_id=session_id,
            tool_call_id="call_authorized",
            tool_name="set_pipeline",
            summary="Create the authorized proposal.",
            rationale="Requested by the user.",
            affects=("graph",),
            arguments_json={"sources": {}, "nodes": [], "edges": [], "outputs": []},
            arguments_redacted_json={"sources": {}, "nodes": [], "edges": [], "outputs": []},
            base_state_id=None,
            actor="composer-web:user-alice",
            session_operation_context=context,
        )
    finally:
        await service._run_sync(service.session_operation_authority.release, context)

    assert proposal.session_id == session_id


@pytest.mark.asyncio
async def test_create_pipeline_proposal_accepts_live_compose_context() -> None:
    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    service = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.pipeline-proposal-authority"),
    )
    session_id = (await service.create_session("alice", "Pipeline proposal authority", "local")).id
    proposal = PipelineProposal.create(
        pipeline={"sources": {}, "nodes": [], "edges": [], "outputs": []},
        base=AbsentBase(),
        reviewed_facts={},
        surface=PlannerSurface.FREEFORM,
        repair_count=0,
        skill_hash=stable_hash("planner-skill"),
        covered_deferred_intent_ids=(),
        supersedes_draft_hash=None,
    )
    plan = PipelinePlanResult(
        proposal=proposal,
        tool_call_id="call_pipeline_authorized",
        custody_result="not_required",
        model_identifier="planner-model",
        model_version="planner-model-v1",
        provider="test",
    )
    public_arguments = redact_tool_call_arguments(
        "set_pipeline",
        proposal.pipeline,
        telemetry=NoopRedactionTelemetry(),
    )
    context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        row = await service.create_pipeline_composition_proposal(
            session_id=session_id,
            plan=plan,
            summary="Create the authorized pipeline proposal.",
            rationale="Requested by the user.",
            affects=("graph",),
            arguments_redacted_json=public_arguments,
            actor="composer-web:user-alice",
            composer_model_identifier="planner-model",
            composer_model_version="planner-model-v1",
            composer_provider="test",
            session_operation_context=context,
        )
    finally:
        await service._run_sync(service.session_operation_authority.release, context)

    assert row.session_id == session_id


@pytest.mark.asyncio
async def test_reject_composition_proposal_accepts_exact_live_proposal_context() -> None:
    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    service = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.composer-proposal-reject-authority"),
    )
    session_id = (await service.create_session("alice", "Composer proposal reject authority", "local")).id
    compose_context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        proposal = await service.create_composition_proposal(
            session_id=session_id,
            tool_call_id="call_reject_authorized",
            tool_name="set_pipeline",
            summary="Create the proposal to reject.",
            rationale="Requested by the user.",
            affects=("graph",),
            arguments_json={"sources": {}, "nodes": [], "edges": [], "outputs": []},
            arguments_redacted_json={"sources": {}, "nodes": [], "edges": [], "outputs": []},
            base_state_id=None,
            actor="composer-web:user-alice",
            session_operation_context=compose_context,
        )
    finally:
        await service._run_sync(service.session_operation_authority.release, compose_context)

    proposal_context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.PROPOSAL,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        rejected = await service.reject_composition_proposal(
            session_id=session_id,
            proposal_id=proposal.id,
            actor="user:alice",
            session_operation_context=proposal_context,
        )
    finally:
        await service._run_sync(service.session_operation_authority.release, proposal_context)

    assert rejected.status == "rejected"
    events = await service.list_proposal_events(session_id)
    assert {event.event_type for event in events} == {"proposal.created", "proposal.rejected"}
    rejected_event = next(event for event in events if event.event_type == "proposal.rejected")
    assert rejected.audit_event_id == rejected_event.id


async def _assert_reject_context_writes_nothing(
    service: SessionServiceImpl,
    *,
    session_id,
    proposal_id,
    context: SessionOperationContext,
) -> None:
    with service._engine.connect() as connection:
        before_proposal = connection.execute(
            select(composition_proposals_table).where(composition_proposals_table.c.id == str(proposal_id))
        ).one()
        before_events = connection.execute(
            select(proposal_events_table).where(proposal_events_table.c.proposal_id == str(proposal_id))
        ).all()

    with pytest.raises(SessionOperationFenceLost):
        await service.reject_composition_proposal(
            session_id=session_id,
            proposal_id=proposal_id,
            actor="user:alice",
            session_operation_context=context,
        )

    with service._engine.connect() as connection:
        after_proposal = connection.execute(
            select(composition_proposals_table).where(composition_proposals_table.c.id == str(proposal_id))
        ).one()
        after_events = connection.execute(
            select(proposal_events_table).where(proposal_events_table.c.proposal_id == str(proposal_id))
        ).all()
    assert tuple(after_proposal) == tuple(before_proposal)
    assert [tuple(row) for row in after_events] == [tuple(row) for row in before_events]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_authority", ["stale", "wrong_kind", "released", "expired"])
async def test_reject_composition_proposal_invalid_authority_writes_nothing(invalid_authority: str) -> None:
    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    first = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.composer-proposal-reject-invalid-authority"),
        owner_instance_id="proposal-reject-first",
    )
    second = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.composer-proposal-reject-successor"),
        owner_instance_id="proposal-reject-second",
    )
    session_id = (await first.create_session("alice", "Invalid proposal reject authority", "local")).id
    compose_context = await first._run_sync(
        lambda: first.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=first.session_operation_owner_instance_id,
            lease_seconds=first.session_operation_lease_seconds,
        )
    )
    try:
        proposal = await first.create_composition_proposal(
            session_id=session_id,
            tool_call_id=f"call_reject_{invalid_authority}",
            tool_name="set_pipeline",
            summary="Create a proposal whose invalid reject authority must write nothing.",
            rationale="Authority regression coverage.",
            affects=("graph",),
            arguments_json={"sources": {}, "nodes": [], "edges": [], "outputs": []},
            arguments_redacted_json={"sources": {}, "nodes": [], "edges": [], "outputs": []},
            base_state_id=None,
            actor="composer-web:user-alice",
            session_operation_context=compose_context,
        )
    finally:
        await first._run_sync(first.session_operation_authority.release, compose_context)

    context = await first._run_sync(
        lambda: first.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.PROPOSAL,
            owner_instance_id=first.session_operation_owner_instance_id,
            lease_seconds=first.session_operation_lease_seconds,
        )
    )
    successor: SessionOperationContext | None = None
    if invalid_authority == "wrong_kind":
        invalid_context = replace(context, operation_kind=SessionOperationKind.COMPOSE)
    elif invalid_authority == "released":
        await first._run_sync(first.session_operation_authority.release, context)
        invalid_context = context
    else:
        with engine.begin() as connection:
            connection.execute(
                update(session_operation_fences_table)
                .where(session_operation_fences_table.c.session_id == str(session_id))
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        invalid_context = context
        if invalid_authority == "stale":
            successor = await second._run_sync(
                lambda: second.session_operation_authority.acquire(
                    session_id=session_id,
                    operation_kind=SessionOperationKind.PROPOSAL,
                    owner_instance_id=second.session_operation_owner_instance_id,
                    lease_seconds=second.session_operation_lease_seconds,
                )
            )

    try:
        await _assert_reject_context_writes_nothing(
            first,
            session_id=session_id,
            proposal_id=proposal.id,
            context=invalid_context,
        )
    finally:
        if successor is not None:
            await second._run_sync(second.session_operation_authority.release, successor)
        elif invalid_authority == "wrong_kind":
            await first._run_sync(first.session_operation_authority.release, context)


@pytest.mark.asyncio
async def test_stale_compose_predecessor_creates_no_proposal_rows_after_takeover() -> None:
    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    first = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.composer-proposal-predecessor"),
        owner_instance_id="composer-proposal-first",
    )
    second = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.composer-proposal-successor"),
        owner_instance_id="composer-proposal-second",
    )
    session_id = (await first.create_session("alice", "Composer proposal takeover", "local")).id
    predecessor = await first._run_sync(
        lambda: first.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=first.session_operation_owner_instance_id,
            lease_seconds=first.session_operation_lease_seconds,
        )
    )
    with engine.begin() as conn:
        conn.execute(
            update(session_operation_fences_table)
            .where(session_operation_fences_table.c.session_id == str(session_id))
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    successor = await second._run_sync(
        lambda: second.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=second.session_operation_owner_instance_id,
            lease_seconds=second.session_operation_lease_seconds,
        )
    )
    try:
        with pytest.raises(SessionOperationFenceLost):
            await first.create_composition_proposal(
                session_id=session_id,
                tool_call_id="call_stale_predecessor",
                tool_name="set_pipeline",
                summary="This stale proposal must not exist.",
                rationale="The predecessor lost authority.",
                affects=("graph",),
                arguments_json={"sources": {}, "nodes": [], "edges": [], "outputs": []},
                arguments_redacted_json={"sources": {}, "nodes": [], "edges": [], "outputs": []},
                base_state_id=None,
                actor="composer-web:user-alice",
                session_operation_context=predecessor,
            )
    finally:
        await second._run_sync(second.session_operation_authority.release, successor)

    with engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count()).select_from(proposal_events_table).where(proposal_events_table.c.session_id == str(session_id))
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                select(func.count())
                .select_from(composition_proposals_table)
                .where(composition_proposals_table.c.session_id == str(session_id))
            ).scalar_one()
            == 0
        )


async def _create_ordinary_accept_proposal(
    service: SessionServiceImpl,
    *,
    session_id,
    base_state_id=None,
):
    compose_context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        return await service.create_composition_proposal(
            session_id=session_id,
            tool_call_id="call_accept_authorized",
            tool_name="set_pipeline",
            summary="Accept the ordinary proposal atomically.",
            rationale="Atomic proposal acceptance regression coverage.",
            affects=("graph",),
            arguments_json={"sources": {}, "nodes": [], "edges": [], "outputs": []},
            arguments_redacted_json={"sources": {}, "nodes": [], "edges": [], "outputs": []},
            base_state_id=base_state_id,
            actor="composer-web:user-alice",
            session_operation_context=compose_context,
        )
    finally:
        await service._run_sync(service.session_operation_authority.release, compose_context)


def _retag_as_blob_only_proposal(service: SessionServiceImpl, proposal_id) -> None:
    with service._engine.begin() as conn:
        proposal = conn.execute(select(composition_proposals_table).where(composition_proposals_table.c.id == str(proposal_id))).one()
        blob_id = uuid4()
        now = datetime.now(UTC)
        arguments = {"blob_id": str(blob_id), "content": "approved content"}
        conn.execute(
            blobs_table.insert().values(
                id=str(blob_id),
                session_id=proposal.session_id,
                filename="approved.txt",
                mime_type="text/plain",
                size_bytes=len(b"approved content"),
                content_hash="a" * 64,
                storage_path=f"/tmp/{blob_id}.txt",
                created_at=now,
                created_by="user",
                source_description=None,
                status="ready",
                creation_modality="verbatim",
            )
        )
        conn.execute(
            update(composition_proposals_table)
            .where(composition_proposals_table.c.id == str(proposal_id))
            .values(
                tool_name="update_blob",
                arguments_json=arguments,
                arguments_redacted_json=arguments,
            )
        )
        conn.execute(
            update(proposal_events_table)
            .where(proposal_events_table.c.proposal_id == str(proposal_id))
            .where(proposal_events_table.c.event_type == "proposal.created")
            .values(
                payload={
                    "schema": "tool_proposal_created.v1",
                    "tool_call_id": "call_accept_authorized",
                    "tool_name": "update_blob",
                    "status": "pending",
                }
            )
        )
        blob_row = conn.execute(select(blobs_table).where(blobs_table.c.id == str(blob_id))).one()
        result_snapshot = blob_row_snapshot_payload(blob_row)
        conn.execute(
            proposal_blob_effect_receipts_table.insert().values(
                proposal_id=str(proposal_id),
                session_id=proposal.session_id,
                tool_name="update_blob",
                blob_id=str(blob_id),
                arguments_hash=stable_hash(arguments),
                result_blob_snapshot=result_snapshot,
                result_blob_snapshot_hash=stable_hash(result_snapshot),
                accepted_event_id=None,
                created_at=now,
                accepted_at=None,
            )
        )


@pytest.mark.asyncio
async def test_accept_ordinary_proposal_atomically_inserts_state_event_and_pending_cas() -> None:
    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    service = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.composer-proposal-accept-authority"),
    )
    session_id = (await service.create_session("alice", "Composer proposal accept authority", "local")).id
    proposal = await _create_ordinary_accept_proposal(service, session_id=session_id)
    proposal_context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.PROPOSAL,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        committed = await service.accept_composition_proposal(
            session_id=session_id,
            proposal_id=proposal.id,
            expected_current_state_id=None,
            state=CompositionStateData(metadata_={"name": "accepted"}, is_valid=True),
            actor="user:alice",
            session_operation_context=proposal_context,
        )
    finally:
        await service._run_sync(service.session_operation_authority.release, proposal_context)

    assert committed.status == "committed"
    assert committed.committed_state_id is not None
    with engine.connect() as conn:
        state_rows = conn.execute(select(composition_states_table).where(composition_states_table.c.session_id == str(session_id))).all()
        accepted_rows = conn.execute(
            select(proposal_events_table)
            .where(proposal_events_table.c.proposal_id == str(proposal.id))
            .where(proposal_events_table.c.event_type == "proposal.accepted")
        ).all()
        committed_row = conn.execute(select(composition_proposals_table).where(composition_proposals_table.c.id == str(proposal.id))).one()

    assert len(state_rows) == 1
    assert len(accepted_rows) == 1
    state_row = state_rows[0]
    accepted_row = accepted_rows[0]
    assert state_row.id == str(committed.committed_state_id)
    assert accepted_row.proposal_id == str(proposal.id)
    assert accepted_row.actor == "user:alice"
    assert accepted_row.payload == {"committed_state_id": state_row.id}
    assert committed_row.committed_state_id == state_row.id
    assert committed_row.audit_event_id == accepted_row.id
    assert committed_row.status == "committed"
    assert committed_row.updated_at == accepted_row.created_at == state_row.created_at


@pytest.mark.asyncio
async def test_accept_ordinary_proposal_rolls_back_state_and_event_when_pending_cas_fails() -> None:
    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    service = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.composer-proposal-accept-rollback"),
    )
    session_id = (await service.create_session("alice", "Composer proposal rollback", "local")).id
    proposal = await _create_ordinary_accept_proposal(service, session_id=session_id)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            f"""
            CREATE TRIGGER fail_ordinary_proposal_commit
            BEFORE UPDATE OF status ON composition_proposals
            WHEN NEW.id = '{proposal.id}' AND NEW.status = 'committed'
            BEGIN
                SELECT RAISE(ABORT, 'deliberate proposal CAS failure');
            END
            """
        )

    proposal_context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.PROPOSAL,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        with pytest.raises(IntegrityError, match="deliberate proposal CAS failure"):
            await service.accept_composition_proposal(
                session_id=session_id,
                proposal_id=proposal.id,
                expected_current_state_id=None,
                state=CompositionStateData(metadata_={"name": "must roll back"}, is_valid=True),
                actor="user:alice",
                session_operation_context=proposal_context,
            )
    finally:
        await service._run_sync(service.session_operation_authority.release, proposal_context)

    with engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count()).select_from(composition_states_table).where(composition_states_table.c.session_id == str(session_id))
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                select(func.count())
                .select_from(proposal_events_table)
                .where(
                    proposal_events_table.c.proposal_id == str(proposal.id),
                    proposal_events_table.c.event_type == "proposal.accepted",
                )
            ).scalar_one()
            == 0
        )
        row = conn.execute(select(composition_proposals_table).where(composition_proposals_table.c.id == str(proposal.id))).one()
    assert row.status == "pending"
    assert row.committed_state_id is None
    assert row.audit_event_id == str(proposal.audit_event_id)


@pytest.mark.asyncio
async def test_accept_ordinary_proposal_stale_predecessor_writes_nothing() -> None:
    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    service = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.composer-proposal-accept-stale-head"),
    )
    session_id = (await service.create_session("alice", "Composer proposal stale predecessor", "local")).id
    proposal = await _create_ordinary_accept_proposal(service, session_id=session_id)
    winner_state = await _save_composition_state(
        service,
        session_id,
        CompositionStateData(metadata_={"name": "winner"}, is_valid=True),
        provenance="tool_call",
    )
    proposal_context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.PROPOSAL,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        with pytest.raises(StaleComposeStateError, match="current composition state changed"):
            await service.accept_composition_proposal(
                session_id=session_id,
                proposal_id=proposal.id,
                expected_current_state_id=None,
                state=CompositionStateData(metadata_={"name": "loser"}, is_valid=True),
                actor="user:alice",
                session_operation_context=proposal_context,
            )
    finally:
        await service._run_sync(service.session_operation_authority.release, proposal_context)

    with engine.connect() as conn:
        state_ids = (
            conn.execute(select(composition_states_table.c.id).where(composition_states_table.c.session_id == str(session_id)))
            .scalars()
            .all()
        )
        accepted_count = conn.execute(
            select(func.count())
            .select_from(proposal_events_table)
            .where(
                proposal_events_table.c.proposal_id == str(proposal.id),
                proposal_events_table.c.event_type == "proposal.accepted",
            )
        ).scalar_one()
        row = conn.execute(select(composition_proposals_table).where(composition_proposals_table.c.id == str(proposal.id))).one()
    assert state_ids == [str(winner_state.id)]
    assert accepted_count == 0
    assert row.status == "pending"


@pytest.mark.asyncio
async def test_accept_ordinary_proposal_requires_absent_base_to_match_locked_head() -> None:
    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    service = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.composer-proposal-accept-absent-base"),
    )
    session_id = (await service.create_session("alice", "Absent proposal base", "local")).id
    proposal = await _create_ordinary_accept_proposal(service, session_id=session_id)
    winner_state = await _save_composition_state(
        service,
        session_id,
        CompositionStateData(metadata_={"name": "winner"}, is_valid=True),
        provenance="tool_call",
    )
    proposal_context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.PROPOSAL,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        with pytest.raises(StaleComposeStateError, match="proposal base no longer matches"):
            await service.accept_composition_proposal(
                session_id=session_id,
                proposal_id=proposal.id,
                expected_current_state_id=winner_state.id,
                state=CompositionStateData(metadata_={"name": "loser"}, is_valid=True),
                actor="user:alice",
                session_operation_context=proposal_context,
            )
    finally:
        await service._run_sync(service.session_operation_authority.release, proposal_context)

    with engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count()).select_from(composition_states_table).where(composition_states_table.c.session_id == str(session_id))
            ).scalar_one()
            == 1
        )
        assert (
            conn.execute(
                select(func.count())
                .select_from(proposal_events_table)
                .where(
                    proposal_events_table.c.proposal_id == str(proposal.id),
                    proposal_events_table.c.event_type == "proposal.accepted",
                )
            ).scalar_one()
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["non_blob_missing_state", "blob_existing_with_new_state"])
async def test_accept_ordinary_proposal_rejects_tool_state_shape_mismatch(case: str) -> None:
    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    service = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.composer-proposal-accept-state-shape"),
    )
    session_id = (await service.create_session("alice", "Proposal state shape", "local")).id
    current = await _save_composition_state(
        service,
        session_id,
        CompositionStateData(metadata_={"name": "current"}, is_valid=True),
        provenance="tool_call",
    )
    proposal = await _create_ordinary_accept_proposal(
        service,
        session_id=session_id,
        base_state_id=current.id,
    )
    state = None
    expected_message = "non-blob ordinary proposal acceptance requires a new state"
    if case == "blob_existing_with_new_state":
        _retag_as_blob_only_proposal(service, proposal.id)
        state = CompositionStateData(metadata_={"name": "must not be inserted"}, is_valid=True)
        expected_message = "blob-only ordinary proposal with an existing state must bind that state"

    proposal_context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.PROPOSAL,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        with pytest.raises(ValueError, match=expected_message):
            await service.accept_composition_proposal(
                session_id=session_id,
                proposal_id=proposal.id,
                expected_current_state_id=current.id,
                state=state,
                actor="user:alice",
                session_operation_context=proposal_context,
            )
    finally:
        await service._run_sync(service.session_operation_authority.release, proposal_context)

    with engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count()).select_from(composition_states_table).where(composition_states_table.c.session_id == str(session_id))
            ).scalar_one()
            == 1
        )
        row = conn.execute(select(composition_proposals_table).where(composition_proposals_table.c.id == str(proposal.id))).one()
    assert row.status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_head", [False, True])
async def test_accept_blob_only_proposal_binds_existing_or_inserts_initial_snapshot(existing_head: bool) -> None:
    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    service = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.composer-proposal-accept-blob-state"),
    )
    session_id = (await service.create_session("alice", "Blob proposal state binding", "local")).id
    current = None
    if existing_head:
        current = await _save_composition_state(
            service,
            session_id,
            CompositionStateData(metadata_={"name": "current"}, is_valid=True),
            provenance="tool_call",
        )
    proposal = await _create_ordinary_accept_proposal(
        service,
        session_id=session_id,
        base_state_id=current.id if current is not None else None,
    )
    _retag_as_blob_only_proposal(service, proposal.id)
    initial_state = None if current is not None else CompositionStateData(metadata_={"name": "initial"}, is_valid=True)
    proposal_context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.PROPOSAL,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        committed = await service.accept_composition_proposal(
            session_id=session_id,
            proposal_id=proposal.id,
            expected_current_state_id=current.id if current is not None else None,
            state=initial_state,
            actor="user:alice",
            session_operation_context=proposal_context,
        )
    finally:
        await service._run_sync(service.session_operation_authority.release, proposal_context)

    with engine.connect() as conn:
        state_rows = conn.execute(select(composition_states_table).where(composition_states_table.c.session_id == str(session_id))).all()
        accepted_event = conn.execute(
            select(proposal_events_table)
            .where(proposal_events_table.c.proposal_id == str(proposal.id))
            .where(proposal_events_table.c.event_type == "proposal.accepted")
        ).one()
    assert len(state_rows) == 1
    assert committed.committed_state_id == UUID(state_rows[0].id)
    if current is not None:
        assert committed.committed_state_id == current.id
    assert accepted_event.payload == {"committed_state_id": state_rows[0].id}
    assert committed.audit_event_id == UUID(accepted_event.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_authority", ["stale", "wrong_kind", "wrong_session", "released", "expired"])
async def test_accept_ordinary_proposal_invalid_authority_writes_nothing(invalid_authority: str) -> None:
    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    first = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.composer-proposal-accept-invalid-authority"),
        owner_instance_id="proposal-accept-first",
    )
    second = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.composer-proposal-accept-successor"),
        owner_instance_id="proposal-accept-second",
    )
    session_id = (await first.create_session("alice", "Invalid proposal accept authority", "local")).id
    other_session_id = (await first.create_session("alice", "Wrong proposal accept session", "local")).id
    proposal = await _create_ordinary_accept_proposal(first, session_id=session_id)
    context_session_id = other_session_id if invalid_authority == "wrong_session" else session_id
    context = await first._run_sync(
        lambda: first.session_operation_authority.acquire(
            session_id=context_session_id,
            operation_kind=SessionOperationKind.PROPOSAL,
            owner_instance_id=first.session_operation_owner_instance_id,
            lease_seconds=first.session_operation_lease_seconds,
        )
    )
    successor = None
    if invalid_authority == "wrong_kind":
        invalid_context = replace(context, operation_kind=SessionOperationKind.COMPOSE)
    elif invalid_authority == "released":
        await first._run_sync(first.session_operation_authority.release, context)
        invalid_context = context
    elif invalid_authority in {"stale", "expired"}:
        with engine.begin() as connection:
            connection.execute(
                update(session_operation_fences_table)
                .where(session_operation_fences_table.c.session_id == str(session_id))
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        invalid_context = context
        if invalid_authority == "stale":
            successor = await second._run_sync(
                lambda: second.session_operation_authority.acquire(
                    session_id=session_id,
                    operation_kind=SessionOperationKind.PROPOSAL,
                    owner_instance_id=second.session_operation_owner_instance_id,
                    lease_seconds=second.session_operation_lease_seconds,
                )
            )
    else:
        invalid_context = context

    try:
        with pytest.raises(SessionOperationFenceLost):
            await first.accept_composition_proposal(
                session_id=session_id,
                proposal_id=proposal.id,
                expected_current_state_id=None,
                state=CompositionStateData(metadata_={"name": "must not persist"}, is_valid=True),
                actor="user:alice",
                session_operation_context=invalid_context,
            )
    finally:
        if successor is not None:
            await second._run_sync(second.session_operation_authority.release, successor)
        elif invalid_authority in {"wrong_kind", "wrong_session"}:
            await first._run_sync(first.session_operation_authority.release, context)

    with engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count()).select_from(composition_states_table).where(composition_states_table.c.session_id == str(session_id))
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                select(func.count())
                .select_from(proposal_events_table)
                .where(
                    proposal_events_table.c.proposal_id == str(proposal.id),
                    proposal_events_table.c.event_type == "proposal.accepted",
                )
            ).scalar_one()
            == 0
        )
        row = conn.execute(select(composition_proposals_table).where(composition_proposals_table.c.id == str(proposal.id))).one()
    assert row.status == "pending"
    assert row.committed_state_id is None
