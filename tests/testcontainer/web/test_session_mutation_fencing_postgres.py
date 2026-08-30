"""Always-run PostgreSQL proofs for distributed session mutation fencing."""

from __future__ import annotations

import asyncio
import errno
import os
import shutil
import threading
import traceback
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import Engine, event, func, insert, select, update
from sqlalchemy.engine import Connection

from elspeth.contracts.advisory_locks import ELSPETH_SESSIONS_LOCK_CLASSID
from elspeth.web.blobs.protocol import BlobForkFenceLostError
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.coordination import repository as coordination_repository
from elspeth.web.coordination.contracts import (
    ArchiveDeleteReconciliation,
    FenceLossReason,
    SessionOperationContext,
    SessionOperationFenceLost,
    SessionOperationKind,
)
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.repository import PostgresSessionOperationRepository, SessionOperationConflictError
from elspeth.web.sessions import archive_quarantine as archive_quarantine_module
from elspeth.web.sessions import service as session_service_module
from elspeth.web.sessions.archive_quarantine import (
    ArchiveQuarantineIdentity,
    archive_quarantine_paths,
    list_archive_quarantine_manifests,
    purge_archive_quarantine,
    restore_archive_quarantine,
    stage_archive_quarantine,
)
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    chat_messages_table,
    composition_proposals_table,
    composition_states_table,
    guided_operations_table,
    proposal_events_table,
    session_operation_fences_table,
    sessions_table,
    web_instances_table,
)
from elspeth.web.sessions.protocol import (
    CompositionStateData,
    GuidedForkSettlementCommand,
    GuidedOperationClaimed,
    GuidedOperationFenceLostError,
    GuidedOperationTakenOver,
    SessionForkParentAuthority,
)
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import QuarantineCleanupError, SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

pytestmark = pytest.mark.testcontainer


@pytest.fixture()
def deployment(
    external_deployment_postgres_url: str,
    tmp_path: Path,
) -> Iterator[tuple[Engine, Engine, SessionServiceImpl, SessionServiceImpl, Path]]:
    first_engine = create_session_engine(external_deployment_postgres_url)
    second_engine = create_session_engine(external_deployment_postgres_url)
    initialize_session_schema(first_engine)
    shared = tmp_path / "shared-blobs"
    first = SessionServiceImpl(
        first_engine,
        shared,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.pg-fork-a"),
        owner_instance_id=f"fork-a-{uuid4()}",
        session_operation_lease_seconds=30,
    )
    second = SessionServiceImpl(
        second_engine,
        shared,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.pg-fork-b"),
        owner_instance_id=f"fork-b-{uuid4()}",
        session_operation_lease_seconds=30,
    )
    try:
        yield first_engine, second_engine, first, second, shared
    finally:
        first_engine.dispose()
        second_engine.dispose()


def _register_instance(engine: Engine, instance_id: str) -> None:
    with engine.begin() as conn:
        now = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        conn.execute(
            insert(web_instances_table).values(
                instance_id=instance_id,
                deployment_target="testcontainer",
                deployment_generation="fork-fencing",
                session_epoch=37,
                landscape_epoch=29,
                coordination_protocol=1,
                image_digest="sha256:fork-fencing",
                revision_label="fork-fencing",
                state="active",
                started_at=now,
                last_heartbeat_at=now,
                lease_expires_at=now + timedelta(minutes=5),
            )
        )


def _expire_archive_owner(
    engine: Engine,
    *,
    session_id: UUID,
    owner_instance_id: str,
) -> None:
    """Make one archive owner provably stale using PostgreSQL database time."""
    with engine.begin() as conn:
        now = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        conn.execute(
            update(session_operation_fences_table)
            .where(session_operation_fences_table.c.session_id == str(session_id))
            .values(lease_expires_at=now - timedelta(seconds=1))
        )
        conn.execute(
            update(web_instances_table)
            .where(web_instances_table.c.instance_id == owner_instance_id)
            .values(lease_expires_at=now - timedelta(seconds=1))
        )


def _session_rows_snapshot(engine: Engine, session_id: UUID) -> tuple[dict[str, Any], dict[str, Any], tuple[dict[str, Any], ...]]:
    """Capture the exact durable rows owned by one unrelated session."""
    with engine.connect() as conn:
        session_row = conn.execute(select(sessions_table).where(sessions_table.c.id == str(session_id))).mappings().one()
        fence_row = (
            conn.execute(select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(session_id)))
            .mappings()
            .one()
        )
        message_rows = conn.execute(
            select(chat_messages_table)
            .where(chat_messages_table.c.session_id == str(session_id))
            .order_by(chat_messages_table.c.sequence_no)
        ).mappings()
        return dict(session_row), dict(fence_row), tuple(dict(row) for row in message_rows)


@pytest.mark.asyncio
async def test_postgres_composer_proposal_stale_predecessor_writes_nothing_before_takeover(
    deployment,
    monkeypatch,
) -> None:
    first_engine, second_engine, first, second, _shared = deployment
    _register_instance(first_engine, first.session_operation_owner_instance_id)
    _register_instance(first_engine, second.session_operation_owner_instance_id)
    session_id = (await first.create_session(f"pg-proposal-{uuid4()}", "Proposal takeover", "local")).id
    predecessor = first.session_operation_authority.acquire(
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=first.session_operation_owner_instance_id,
        lease_seconds=first.session_operation_lease_seconds,
    )
    entered = threading.Event()
    release = threading.Event()
    original_validate = session_service_module.validate_proposal_blob_references

    def block_after_initial_authority(*args: Any, **kwargs: Any) -> None:
        entered.set()
        if not release.wait(timeout=10):
            raise AssertionError("proposal predecessor barrier timed out")
        original_validate(*args, **kwargs)

    monkeypatch.setattr(session_service_module, "validate_proposal_blob_references", block_after_initial_authority)
    predecessor_task = asyncio.create_task(
        first.create_composition_proposal(
            session_id=session_id,
            tool_call_id="call_pg_stale_predecessor",
            tool_name="set_pipeline",
            summary="This stale proposal must not exist.",
            rationale="The predecessor lease expires during staging.",
            affects=("graph",),
            arguments_json={"sources": {}, "nodes": [], "edges": [], "outputs": []},
            arguments_redacted_json={"sources": {}, "nodes": [], "edges": [], "outputs": []},
            base_state_id=None,
            actor="composer-web:user-alice",
            session_operation_context=predecessor,
        )
    )
    assert await asyncio.to_thread(entered.wait, 10)
    with second_engine.begin() as conn:
        now = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        conn.execute(
            update(session_operation_fences_table)
            .where(session_operation_fences_table.c.session_id == str(session_id))
            .values(lease_expires_at=now - timedelta(seconds=1))
        )
        conn.execute(
            update(web_instances_table)
            .where(web_instances_table.c.instance_id == first.session_operation_owner_instance_id)
            .values(lease_expires_at=now - timedelta(seconds=1))
        )

    successor_task = asyncio.create_task(
        asyncio.to_thread(
            second.session_operation_authority.acquire,
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=second.session_operation_owner_instance_id,
            lease_seconds=second.session_operation_lease_seconds,
        )
    )
    await asyncio.sleep(0.1)
    assert not successor_task.done()
    release.set()
    with pytest.raises(SessionOperationFenceLost):
        await predecessor_task
    successor = await asyncio.wait_for(successor_task, timeout=10)
    try:
        with second_engine.connect() as conn:
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
        winner = await second.create_composition_proposal(
            session_id=session_id,
            tool_call_id="call_pg_successor",
            tool_name="set_pipeline",
            summary="The successor owns this proposal.",
            rationale="The stale predecessor was fenced before publication.",
            affects=("graph",),
            arguments_json={"sources": {}, "nodes": [], "edges": [], "outputs": []},
            arguments_redacted_json={"sources": {}, "nodes": [], "edges": [], "outputs": []},
            base_state_id=None,
            actor="composer-web:user-alice",
            session_operation_context=successor,
        )
        with second_engine.connect() as conn:
            created_events = conn.execute(
                select(proposal_events_table).where(
                    proposal_events_table.c.session_id == str(session_id),
                    proposal_events_table.c.event_type == "proposal.created",
                )
            ).all()
            proposals = conn.execute(
                select(composition_proposals_table).where(composition_proposals_table.c.session_id == str(session_id))
            ).all()
        assert len(created_events) == 1
        assert len(proposals) == 1
        assert proposals[0].id == str(winner.id)
        assert created_events[0].proposal_id == proposals[0].id
        assert proposals[0].audit_event_id == created_events[0].id
    finally:
        second.session_operation_authority.release(successor)


@pytest.mark.asyncio
async def test_postgres_composer_proposal_reject_stale_predecessor_writes_nothing_and_successor_wins(
    deployment,
    monkeypatch,
) -> None:
    first_engine, second_engine, first, second, _shared = deployment
    _register_instance(first_engine, first.session_operation_owner_instance_id)
    _register_instance(first_engine, second.session_operation_owner_instance_id)
    session_id = (await first.create_session(f"pg-proposal-reject-{uuid4()}", "Proposal reject takeover", "local")).id
    compose_context = first.session_operation_authority.acquire(
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=first.session_operation_owner_instance_id,
        lease_seconds=first.session_operation_lease_seconds,
    )
    try:
        proposal = await first.create_composition_proposal(
            session_id=session_id,
            tool_call_id="call_pg_reject_takeover",
            tool_name="set_pipeline",
            summary="Reject under the successor authority.",
            rationale="The predecessor will lose its lease before terminal publication.",
            affects=("graph",),
            arguments_json={"sources": {}, "nodes": [], "edges": [], "outputs": []},
            arguments_redacted_json={"sources": {}, "nodes": [], "edges": [], "outputs": []},
            base_state_id=None,
            actor="composer-web:user-alice",
            session_operation_context=compose_context,
        )
    finally:
        first.session_operation_authority.release(compose_context)

    predecessor = first.session_operation_authority.acquire(
        session_id=session_id,
        operation_kind=SessionOperationKind.PROPOSAL,
        owner_instance_id=first.session_operation_owner_instance_id,
        lease_seconds=first.session_operation_lease_seconds,
    )
    entered = threading.Event()
    release = threading.Event()
    original_reject = session_service_module._SessionComposerMutations.reject_pending_proposal
    blocked = False

    def block_first_reject(self: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal blocked
        if not blocked:
            blocked = True
            entered.set()
            if not release.wait(timeout=10):
                raise AssertionError("proposal reject predecessor barrier timed out")
        original_reject(self, *args, **kwargs)

    monkeypatch.setattr(session_service_module._SessionComposerMutations, "reject_pending_proposal", block_first_reject)
    predecessor_task = asyncio.create_task(
        first.reject_composition_proposal(
            session_id=session_id,
            proposal_id=proposal.id,
            actor="user:alice",
            session_operation_context=predecessor,
        )
    )
    assert await asyncio.to_thread(entered.wait, 10)
    with second_engine.begin() as conn:
        now = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        conn.execute(
            update(session_operation_fences_table)
            .where(session_operation_fences_table.c.session_id == str(session_id))
            .values(lease_expires_at=now - timedelta(seconds=1))
        )
        conn.execute(
            update(web_instances_table)
            .where(web_instances_table.c.instance_id == first.session_operation_owner_instance_id)
            .values(lease_expires_at=now - timedelta(seconds=1))
        )

    successor_task = asyncio.create_task(
        asyncio.to_thread(
            second.session_operation_authority.acquire,
            session_id=session_id,
            operation_kind=SessionOperationKind.PROPOSAL,
            owner_instance_id=second.session_operation_owner_instance_id,
            lease_seconds=second.session_operation_lease_seconds,
        )
    )
    await asyncio.sleep(0.1)
    assert not successor_task.done()
    release.set()
    with pytest.raises(SessionOperationFenceLost):
        await predecessor_task
    successor = await asyncio.wait_for(successor_task, timeout=10)
    try:
        with second_engine.connect() as conn:
            stale_rejections = conn.execute(
                select(proposal_events_table).where(
                    proposal_events_table.c.proposal_id == str(proposal.id),
                    proposal_events_table.c.event_type == "proposal.rejected",
                )
            ).all()
            pending = conn.execute(select(composition_proposals_table).where(composition_proposals_table.c.id == str(proposal.id))).one()
        assert stale_rejections == []
        assert pending.status == "pending"
        assert pending.audit_event_id == str(proposal.audit_event_id)

        winner = await second.reject_composition_proposal(
            session_id=session_id,
            proposal_id=proposal.id,
            actor="user:alice",
            session_operation_context=successor,
        )
        with second_engine.connect() as conn:
            rejected_events = conn.execute(
                select(proposal_events_table).where(
                    proposal_events_table.c.proposal_id == str(proposal.id),
                    proposal_events_table.c.event_type == "proposal.rejected",
                )
            ).all()
            rejected_row = conn.execute(
                select(composition_proposals_table).where(composition_proposals_table.c.id == str(proposal.id))
            ).one()
        assert len(rejected_events) == 1
        assert rejected_row.status == "rejected"
        assert rejected_row.audit_event_id == rejected_events[0].id == str(winner.audit_event_id)
        assert rejected_row.updated_at == rejected_events[0].created_at
    finally:
        second.session_operation_authority.release(successor)


@pytest.mark.asyncio
async def test_postgres_composer_proposal_accept_stale_predecessor_writes_nothing_and_successor_wins(
    deployment,
    monkeypatch,
) -> None:
    first_engine, second_engine, first, second, _shared = deployment
    _register_instance(first_engine, first.session_operation_owner_instance_id)
    _register_instance(first_engine, second.session_operation_owner_instance_id)
    session_id = (await first.create_session(f"pg-proposal-accept-{uuid4()}", "Proposal accept takeover", "local")).id
    compose_context = first.session_operation_authority.acquire(
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=first.session_operation_owner_instance_id,
        lease_seconds=first.session_operation_lease_seconds,
    )
    try:
        proposal = await first.create_composition_proposal(
            session_id=session_id,
            tool_call_id="call_pg_accept_takeover",
            tool_name="set_pipeline",
            summary="Commit under the successor authority.",
            rationale="The stale predecessor must publish no partial cohort.",
            affects=("graph",),
            arguments_json={"sources": {}, "nodes": [], "edges": [], "outputs": []},
            arguments_redacted_json={"sources": {}, "nodes": [], "edges": [], "outputs": []},
            base_state_id=None,
            actor="composer-web:user-alice",
            session_operation_context=compose_context,
        )
    finally:
        first.session_operation_authority.release(compose_context)

    predecessor = first.session_operation_authority.acquire(
        session_id=session_id,
        operation_kind=SessionOperationKind.PROPOSAL,
        owner_instance_id=first.session_operation_owner_instance_id,
        lease_seconds=first.session_operation_lease_seconds,
    )
    entered = threading.Event()
    release = threading.Event()
    original_accept = session_service_module._SessionComposerMutations.accept_pending_ordinary_proposal
    blocked = False
    successor_reached_advisory_lock = threading.Event()

    def block_first_accept(self: Any, *args: Any, **kwargs: Any):
        nonlocal blocked
        if not blocked:
            blocked = True
            entered.set()
            if not release.wait(timeout=10):
                raise AssertionError("proposal accept predecessor barrier timed out")
        return original_accept(self, *args, **kwargs)

    def capture_successor_advisory_lock(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        if "pg_catalog.pg_advisory_xact_lock" in statement.lower():
            successor_reached_advisory_lock.set()

    monkeypatch.setattr(
        session_service_module._SessionComposerMutations,
        "accept_pending_ordinary_proposal",
        block_first_accept,
    )
    predecessor_task = asyncio.create_task(
        first.accept_composition_proposal(
            session_id=session_id,
            proposal_id=proposal.id,
            expected_current_state_id=None,
            state=CompositionStateData(is_valid=True),
            actor="user:alice",
            session_operation_context=predecessor,
        )
    )
    assert await asyncio.to_thread(entered.wait, 10)
    with second_engine.begin() as conn:
        now = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        conn.execute(
            update(session_operation_fences_table)
            .where(session_operation_fences_table.c.session_id == str(session_id))
            .values(lease_expires_at=now - timedelta(seconds=1))
        )
        conn.execute(
            update(web_instances_table)
            .where(web_instances_table.c.instance_id == first.session_operation_owner_instance_id)
            .values(lease_expires_at=now - timedelta(seconds=1))
        )

    event.listen(second_engine, "before_cursor_execute", capture_successor_advisory_lock)
    try:
        successor_task = asyncio.create_task(
            asyncio.to_thread(
                second.session_operation_authority.acquire,
                session_id=session_id,
                operation_kind=SessionOperationKind.PROPOSAL,
                owner_instance_id=second.session_operation_owner_instance_id,
                lease_seconds=second.session_operation_lease_seconds,
            )
        )
        assert await asyncio.to_thread(successor_reached_advisory_lock.wait, 10)
        assert not successor_task.done()
    finally:
        event.remove(second_engine, "before_cursor_execute", capture_successor_advisory_lock)
        release.set()
    with pytest.raises(SessionOperationFenceLost):
        await predecessor_task
    successor = await asyncio.wait_for(successor_task, timeout=10)
    try:
        with second_engine.connect() as conn:
            stale_state_count = conn.execute(
                select(func.count()).select_from(composition_states_table).where(composition_states_table.c.session_id == str(session_id))
            ).scalar_one()
            stale_acceptance_count = conn.execute(
                select(func.count())
                .select_from(proposal_events_table)
                .where(
                    proposal_events_table.c.proposal_id == str(proposal.id),
                    proposal_events_table.c.event_type == "proposal.accepted",
                )
            ).scalar_one()
            pending = conn.execute(select(composition_proposals_table).where(composition_proposals_table.c.id == str(proposal.id))).one()
        assert stale_state_count == 0
        assert stale_acceptance_count == 0
        assert pending.status == "pending"
        assert pending.committed_state_id is None
        assert pending.audit_event_id == str(proposal.audit_event_id)

        committed = await second.accept_composition_proposal(
            session_id=session_id,
            proposal_id=proposal.id,
            expected_current_state_id=None,
            state=CompositionStateData(is_valid=True),
            actor="user:alice",
            session_operation_context=successor,
        )
        with second_engine.connect() as conn:
            states = conn.execute(select(composition_states_table).where(composition_states_table.c.session_id == str(session_id))).all()
            accepted_events = conn.execute(
                select(proposal_events_table).where(
                    proposal_events_table.c.proposal_id == str(proposal.id),
                    proposal_events_table.c.event_type == "proposal.accepted",
                )
            ).all()
            committed_row = conn.execute(
                select(composition_proposals_table).where(composition_proposals_table.c.id == str(proposal.id))
            ).one()

        assert len(states) == 1
        assert len(accepted_events) == 1
        state_id = states[0].id
        accepted_event = accepted_events[0]
        assert committed.status == committed_row.status == "committed"
        assert committed_row.committed_state_id == state_id == str(committed.committed_state_id)
        assert accepted_event.payload == {"committed_state_id": state_id}
        assert committed_row.audit_event_id == accepted_event.id == str(committed.audit_event_id)
        assert committed_row.updated_at == accepted_event.created_at == states[0].created_at
    finally:
        second.session_operation_authority.release(successor)


@pytest.mark.asyncio
async def test_postgres_composer_preferences_stale_predecessor_writes_nothing_and_successor_wins(
    deployment,
    monkeypatch,
) -> None:
    first_engine, second_engine, first, second, _shared = deployment
    _register_instance(first_engine, first.session_operation_owner_instance_id)
    _register_instance(first_engine, second.session_operation_owner_instance_id)
    session_id = (await first.create_session(f"pg-preferences-{uuid4()}", "Preferences takeover", "local")).id
    with first_engine.connect() as conn:
        before = conn.execute(select(sessions_table).where(sessions_table.c.id == str(session_id))).one()

    predecessor = first.session_operation_authority.acquire(
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=first.session_operation_owner_instance_id,
        lease_seconds=first.session_operation_lease_seconds,
    )
    entered = threading.Event()
    release = threading.Event()
    original_record = session_service_module._SessionComposerMutations.record_preferences_changed
    blocked = False

    def block_first_event(self: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal blocked
        if not blocked:
            blocked = True
            entered.set()
            if not release.wait(timeout=10):
                raise AssertionError("preferences predecessor barrier timed out")
        original_record(self, *args, **kwargs)

    monkeypatch.setattr(session_service_module._SessionComposerMutations, "record_preferences_changed", block_first_event)
    predecessor_task = asyncio.create_task(
        first.update_composer_preferences(
            session_id,
            trust_mode="explicit_approve",
            density_default="medium",
            actor="user:alice",
            session_operation_context=predecessor,
        )
    )
    assert await asyncio.to_thread(entered.wait, 10)
    with second_engine.begin() as conn:
        now = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        conn.execute(
            update(session_operation_fences_table)
            .where(session_operation_fences_table.c.session_id == str(session_id))
            .values(lease_expires_at=now - timedelta(seconds=1))
        )
        conn.execute(
            update(web_instances_table)
            .where(web_instances_table.c.instance_id == first.session_operation_owner_instance_id)
            .values(lease_expires_at=now - timedelta(seconds=1))
        )

    successor_task = asyncio.create_task(
        asyncio.to_thread(
            second.session_operation_authority.acquire,
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=second.session_operation_owner_instance_id,
            lease_seconds=second.session_operation_lease_seconds,
        )
    )
    await asyncio.sleep(0.1)
    assert not successor_task.done()
    release.set()
    with pytest.raises(SessionOperationFenceLost):
        await predecessor_task
    successor = await asyncio.wait_for(successor_task, timeout=10)
    try:
        with second_engine.connect() as conn:
            after_predecessor = conn.execute(select(sessions_table).where(sessions_table.c.id == str(session_id))).one()
            assert after_predecessor.trust_mode == before.trust_mode
            assert after_predecessor.density_default == before.density_default
            assert after_predecessor.updated_at == before.updated_at
            assert (
                conn.execute(
                    select(func.count()).select_from(proposal_events_table).where(proposal_events_table.c.session_id == str(session_id))
                ).scalar_one()
                == 0
            )

        transition = await second.update_composer_preferences(
            session_id,
            trust_mode="explicit_approve",
            density_default="medium",
            actor="user:alice",
            session_operation_context=successor,
        )
        assert transition.prior.trust_mode == "auto_commit"
        assert transition.prior.density_default == "high"
        assert transition.current.trust_mode == "explicit_approve"
        assert transition.current.density_default == "medium"
        with second_engine.connect() as conn:
            winner_row = conn.execute(select(sessions_table).where(sessions_table.c.id == str(session_id))).one()
            events = conn.execute(
                select(proposal_events_table).where(
                    proposal_events_table.c.session_id == str(session_id),
                    proposal_events_table.c.event_type == "trust_mode.changed",
                )
            ).all()
        assert winner_row.trust_mode == "explicit_approve"
        assert winner_row.density_default == "medium"
        assert len(events) == 1
        assert events[0].proposal_id is None
        assert events[0].payload == {
            "trust_mode": "explicit_approve",
            "prior_trust_mode": "auto_commit",
            "density_default": "medium",
        }
        assert events[0].created_at == winner_row.updated_at
    finally:
        second.session_operation_authority.release(successor)


async def _claim(
    service: SessionServiceImpl,
    session_id: UUID,
    operation_id: str,
) -> SessionForkParentAuthority:
    context = service.session_operation_authority.acquire(
        session_id=session_id,
        operation_kind=SessionOperationKind.SESSION_FORK,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    )
    outcome = await service.reserve_guided_operation(
        session_id=session_id,
        operation_id=operation_id,
        kind="session_fork",
        request_hash="a" * 64,
        actor="composer_route",
        lease_seconds=30,
        session_operation_context=context,
    )
    assert type(outcome) in {GuidedOperationClaimed, GuidedOperationTakenOver}
    claimed = cast("GuidedOperationClaimed | GuidedOperationTakenOver", outcome)
    return SessionForkParentAuthority(
        parent_context=context,
        guided_fence=claimed.fence,
    )


@pytest.mark.asyncio
async def test_postgres_dual_fence_atomic_takeover_stale_refusal_and_fs_has_no_connection(
    deployment,
) -> None:
    first_engine, second_engine, first, second, shared = deployment
    _register_instance(first_engine, first.session_operation_owner_instance_id)
    _register_instance(first_engine, second.session_operation_owner_instance_id)
    blobs = BlobServiceImpl(second_engine, shared)
    parent = await first.create_session(f"pg-fork-{uuid4()}", "Parent", "local")
    create_lease = await SessionOperationLease.acquire(
        second.session_operation_authority,
        session_id=parent.id,
        operation_kind=SessionOperationKind.CREATE,
        owner_instance_id=second.session_operation_owner_instance_id,
        lease_seconds=second.session_operation_lease_seconds,
    )
    try:
        source = await blobs.create_blob(
            parent.id,
            "source.csv",
            b"a,b\n1,2\n",
            "text/csv",
            session_operation_context=create_lease.context,
        )
    finally:
        await create_lease.close()
    message = await first.add_message(
        parent.id,
        "user",
        "fork here",
        writer_principal="route_user_message",
    )
    operation_id = str(uuid4())
    first_parent = await _claim(first, parent.id, operation_id)
    fence_statements: list[tuple[str, str]] = []

    def capture_fence_sql(_conn, _cursor, statement, parameters, _context, _many) -> None:
        if "session_operation_fences" in statement:
            fence_statements.append((" ".join(statement.lower().split()), repr(parameters)))

    event.listen(first_engine, "before_cursor_execute", capture_fence_sql)
    try:
        staged = await first.fork_session(
            first_parent,
            fork_message_id=message.id,
            new_message_content="edited",
        )
    finally:
        event.remove(first_engine, "before_cursor_execute", capture_fence_sql)

    assert any(
        statement.startswith("insert into session_operation_fences") and "'create'" in parameters and "1" in parameters
        for statement, parameters in fence_statements
    )
    assert any(
        statement.startswith("update session_operation_fences") and "'session_fork'" in parameters and "2" in parameters
        for statement, parameters in fence_statements
    )

    with second_engine.connect() as conn:
        atomic = (
            conn.execute(
                select(
                    sessions_table.c.id.label("child_id"),
                    session_operation_fences_table.c.operation_kind,
                    session_operation_fences_table.c.operation_epoch,
                    guided_operations_table.c.operation_id.label("guided_operation_id"),
                )
                .join(
                    session_operation_fences_table,
                    session_operation_fences_table.c.session_id == sessions_table.c.id,
                )
                .join(
                    guided_operations_table,
                    guided_operations_table.c.result_session_id == sessions_table.c.id,
                )
                .where(sessions_table.c.id == str(staged.session.id))
            )
            .mappings()
            .one()
        )
    assert atomic["child_id"] == str(staged.session.id)
    assert atomic["operation_kind"] == "session_fork"
    assert atomic["operation_epoch"] == 2
    assert atomic["guided_operation_id"] == operation_id

    with first_engine.begin() as conn:
        now = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        conn.execute(
            update(session_operation_fences_table)
            .where(session_operation_fences_table.c.session_id.in_([str(parent.id), str(staged.session.id)]))
            .values(lease_expires_at=now - timedelta(seconds=1))
        )
        conn.execute(
            update(guided_operations_table)
            .where(
                guided_operations_table.c.session_id == str(parent.id),
                guided_operations_table.c.operation_id == operation_id,
            )
            .values(lease_expires_at=now - timedelta(seconds=1))
        )
        conn.execute(
            update(web_instances_table)
            .where(web_instances_table.c.instance_id == first.session_operation_owner_instance_id)
            .values(lease_expires_at=now - timedelta(seconds=1))
        )

    second_parent = await _claim(second, parent.id, operation_id)
    resumed = await second.fork_session(
        second_parent,
        fork_message_id=message.id,
        new_message_content="edited",
    )
    assert resumed.session.id == staged.session.id
    assert resumed.authority.parent.parent_context.fence.operation_epoch > staged.authority.parent.parent_context.fence.operation_epoch
    assert resumed.authority.child_context.fence.operation_epoch > staged.authority.child_context.fence.operation_epoch
    assert resumed.authority.parent.parent_context.fence.lease_token != staged.authority.parent.parent_context.fence.lease_token
    assert resumed.authority.child_context.fence.lease_token != staged.authority.child_context.fence.lease_token
    assert resumed.authority.parent.guided_fence.operation_id == operation_id
    assert resumed.authority.parent.guided_fence.attempt > staged.authority.parent.guided_fence.attempt
    with second_engine.connect() as conn:
        durable_binding = conn.execute(
            select(guided_operations_table.c.result_session_id).where(
                guided_operations_table.c.session_id == str(parent.id),
                guided_operations_table.c.operation_id == operation_id,
            )
        ).scalar_one()
    assert durable_binding == str(staged.session.id)

    async def checkpoint() -> None:
        return None

    with pytest.raises(BlobForkFenceLostError):
        await blobs.copy_blobs_for_fork(
            parent.id,
            staged.session.id,
            staged.blob_plan,
            staged.authority,
            checkpoint=checkpoint,
        )

    entered = threading.Event()
    release = threading.Event()
    original_read_bytes = Path.read_bytes

    def paused_read_bytes(path: Path) -> bytes:
        entered.set()
        assert release.wait(timeout=10)
        return original_read_bytes(path)

    with patch.object(Path, "read_bytes", paused_read_bytes):
        copy_task = asyncio.create_task(
            blobs.copy_blobs_for_fork(
                parent.id,
                resumed.session.id,
                resumed.blob_plan,
                resumed.authority,
                checkpoint=checkpoint,
            )
        )
        assert await asyncio.to_thread(entered.wait, 10)
        assert first_engine.pool.checkedout() == 0
        assert second_engine.pool.checkedout() == 0
        release.set()
        copied = await copy_task
    assert copied[source.id].session_id == resumed.session.id

    copied_bytes = Path(copied[source.id].storage_path).read_bytes()
    with second_engine.connect() as conn:
        before_session_count = conn.execute(
            select(func.count()).select_from(sessions_table).where(sessions_table.c.id.in_([str(parent.id), str(staged.session.id)]))
        ).scalar_one()
    stale_command = GuidedForkSettlementCommand(
        authority=staged.authority,
        expected_current_state_id=None,
        edited_message_id=staged.messages[-1].id,
        rewritten_state_id=None,
        rewritten_state=None,
        response_hash="b" * 64,
        actor="composer_route",
    )
    with pytest.raises(GuidedOperationFenceLostError):
        await first.settle_guided_fork_operation(stale_command)
    with pytest.raises(GuidedOperationFenceLostError):
        await first.fail_guided_fork_operation(
            staged.authority,
            failure_code="operation_failed",
            actor="composer_route",
        )
    with pytest.raises(BlobForkFenceLostError):
        await blobs.cleanup_blobs_for_fork(staged.authority)
    with pytest.raises(SessionOperationConflictError):
        await first.archive_session(parent.id)
    with pytest.raises(SessionOperationConflictError):
        await first.archive_session(staged.session.id)
    assert Path(copied[source.id].storage_path).read_bytes() == copied_bytes
    with second_engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count()).select_from(sessions_table).where(sessions_table.c.id.in_([str(parent.id), str(staged.session.id)]))
            ).scalar_one()
            == before_session_count
        )

    settled = await second.settle_guided_fork_operation(
        GuidedForkSettlementCommand(
            authority=resumed.authority,
            expected_current_state_id=None,
            edited_message_id=resumed.messages[-1].id,
            rewritten_state_id=None,
            rewritten_state=None,
            response_hash="c" * 64,
            actor="composer_route",
        )
    )
    assert settled.archived_at is None
    with pytest.raises(GuidedOperationFenceLostError):
        await second.settle_guided_fork_operation(
            GuidedForkSettlementCommand(
                authority=resumed.authority,
                expected_current_state_id=None,
                edited_message_id=resumed.messages[-1].id,
                rewritten_state_id=None,
                rewritten_state=None,
                response_hash="d" * 64,
                actor="competing-settler",
            )
        )
    with pytest.raises(GuidedOperationFenceLostError):
        await second.fail_guided_fork_operation(
            resumed.authority,
            failure_code="operation_failed",
            actor="competing-settler",
        )
    second.session_operation_authority.release(resumed.authority.child_context)
    second.session_operation_authority.release(resumed.authority.parent.parent_context)
    with second_engine.connect() as conn:
        child_released = conn.execute(
            select(session_operation_fences_table.c.released_at).where(
                session_operation_fences_table.c.session_id == str(resumed.session.id)
            )
        ).scalar_one()
        parent_released = conn.execute(
            select(session_operation_fences_table.c.released_at).where(session_operation_fences_table.c.session_id == str(parent.id))
        ).scalar_one()
    assert child_released <= parent_released

    await second.archive_session(resumed.session.id)
    await second.archive_session(parent.id)
    assert (await second.get_session(resumed.session.id)).archived_at is not None
    assert (await second.get_session(parent.id)).archived_at is not None


@pytest.mark.asyncio
async def test_postgres_target_rename_holds_no_connection_and_release_cannot_deadlock(
    deployment,
) -> None:
    first_engine, second_engine, first, second, shared = deployment
    _register_instance(first_engine, first.session_operation_owner_instance_id)
    _register_instance(first_engine, second.session_operation_owner_instance_id)
    blobs = BlobServiceImpl(second_engine, shared)
    parent = await first.create_session(f"pg-pair-{uuid4()}", "Parent", "local")
    create_lease = await SessionOperationLease.acquire(
        second.session_operation_authority,
        session_id=parent.id,
        operation_kind=SessionOperationKind.CREATE,
        owner_instance_id=second.session_operation_owner_instance_id,
        lease_seconds=second.session_operation_lease_seconds,
    )
    try:
        await blobs.create_blob(
            parent.id,
            "source.csv",
            b"x\n1\n",
            "text/csv",
            session_operation_context=create_lease.context,
        )
    finally:
        await create_lease.close()
    message = await first.add_message(
        parent.id,
        "user",
        "fork",
        writer_principal="route_user_message",
    )
    authority = await _claim(first, parent.id, str(uuid4()))
    staged = await first.fork_session(
        authority,
        fork_message_id=message.id,
        new_message_content="edited",
    )

    with pytest.raises(SessionOperationConflictError):
        await second.archive_session(parent.id)

    async def checkpoint() -> None:
        return None

    entered = threading.Event()
    resume_rename = threading.Event()
    original_replace = os.replace
    child_blob_dir = shared / "blobs" / str(staged.session.id)
    phase_sql: list[tuple[Engine, str, Any]] = []

    def capture_phase_sql(conn, _cursor, statement, parameters, _context, _many) -> None:
        phase_sql.append((conn.engine, " ".join(statement.lower().split()), parameters))

    def paused_target_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        source_path = Path(source)
        target_path = Path(target)
        if target_path.parent == child_blob_dir and source_path.name.endswith(".custody.tmp"):
            entered.set()
            assert resume_rename.wait(timeout=10)
        original_replace(source, target)

    copy_task: asyncio.Task[dict[UUID, Any]] | None = None
    release_task: asyncio.Task[None] | None = None
    event.listen(second_engine, "before_cursor_execute", capture_phase_sql)
    try:
        with patch("elspeth.web.blobs.service.os.replace", paused_target_replace):
            copy_task = asyncio.create_task(
                blobs.copy_blobs_for_fork(
                    parent.id,
                    staged.session.id,
                    staged.blob_plan,
                    staged.authority,
                    checkpoint=checkpoint,
                )
            )
            try:
                assert await asyncio.to_thread(entered.wait, 10)
                assert first_engine.pool.checkedout() == 0
                assert second_engine.pool.checkedout() == 0

                # This is a real production contender, not a manually held
                # lock. It must complete while filesystem persistence pauses.
                release_task = asyncio.create_task(
                    asyncio.to_thread(
                        first.session_operation_authority.release,
                        staged.authority.child_context,
                    )
                )
                await asyncio.wait_for(asyncio.shield(release_task), timeout=2)
            finally:
                resume_rename.set()
                if release_task is not None and not release_task.done():
                    await asyncio.wait_for(asyncio.shield(release_task), timeout=10)

            with pytest.raises(BlobForkFenceLostError):
                await asyncio.wait_for(copy_task, timeout=10)
    finally:
        event.remove(second_engine, "before_cursor_execute", capture_phase_sql)

    quota_lock_index = next(
        index
        for index, (_engine, statement, _parameters) in enumerate(phase_sql)
        if " from sessions " in f" {statement} " and statement.endswith(" for update")
    )
    pre_quota_advisory_locks = tuple(
        (engine, parameters) for engine, statement, parameters in phase_sql[:quota_lock_index] if "pg_advisory_xact_lock" in statement
    )
    assert len(pre_quota_advisory_locks) == 4
    observed_session_ids: list[str] = []
    for engine, parameters in pre_quota_advisory_locks:
        assert engine is second_engine
        assert type(parameters) is tuple
        assert parameters[0] == ELSPETH_SESSIONS_LOCK_CLASSID
        assert type(parameters[1]) is str
        observed_session_ids.append(parameters[1])
    expected_pair = tuple(sorted((str(parent.id), str(staged.session.id))))
    observed_pairs = tuple(tuple(observed_session_ids[offset : offset + 2]) for offset in range(0, len(observed_session_ids), 2))
    assert observed_pairs == (expected_pair, expected_pair)


@pytest.mark.asyncio
async def test_postgres_archive_first_paused_gap_admits_no_guided_row_or_child(
    deployment,
) -> None:
    first_engine, second_engine, first, second, shared = deployment
    _register_instance(first_engine, first.session_operation_owner_instance_id)
    _register_instance(first_engine, second.session_operation_owner_instance_id)
    parent = await first.create_session(f"pg-archive-{uuid4()}", "Parent", "local")
    message = await first.add_message(
        parent.id,
        "user",
        "fork",
        writer_principal="route_user_message",
    )
    blob_dir = shared / "blobs" / str(parent.id)
    blob_dir.mkdir(parents=True)
    (blob_dir / "held.bin").write_bytes(b"held")
    entered = threading.Event()
    release = threading.Event()
    original_rename_noreplace_at = archive_quarantine_module._rename_noreplace_at

    def paused_rename_noreplace_at(
        source_parent_fd: int,
        source_name: str,
        target_parent_fd: int,
        target_name: str,
    ) -> None:
        assert source_name == str(parent.id)
        assert target_name == "payload"
        entered.set()
        assert release.wait(timeout=10)
        original_rename_noreplace_at(
            source_parent_fd,
            source_name,
            target_parent_fd,
            target_name,
        )

    operation_id = str(uuid4())
    with patch.object(
        archive_quarantine_module,
        "_rename_noreplace_at",
        paused_rename_noreplace_at,
    ):
        archive_task = asyncio.create_task(first.archive_session(parent.id))
        assert await asyncio.to_thread(entered.wait, 10)
        with pytest.raises(SessionOperationConflictError):
            await _claim(second, parent.id, operation_id)
        with second_engine.connect() as conn:
            assert (
                conn.execute(
                    select(func.count()).select_from(guided_operations_table).where(guided_operations_table.c.session_id == str(parent.id))
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    select(func.count()).select_from(sessions_table).where(sessions_table.c.forked_from_session_id == str(parent.id))
                ).scalar_one()
                == 0
            )
        release.set()
        await archive_task

    with second_engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(sessions_table).where(sessions_table.c.id == str(parent.id))).scalar_one() == 0
    assert not blob_dir.exists()
    assert message.session_id == parent.id


@pytest.mark.asyncio
@pytest.mark.parametrize("external_phase", ["manifest_fsync", "payload_rename"])
async def test_postgres_archive_filesystem_phase_holds_no_database_connection(
    deployment,
    monkeypatch: pytest.MonkeyPatch,
    external_phase: str,
) -> None:
    first_engine, second_engine, first, second, shared = deployment
    _register_instance(first_engine, first.session_operation_owner_instance_id)
    _register_instance(first_engine, second.session_operation_owner_instance_id)
    session = await first.create_session(f"pg-archive-fs-{uuid4()}", "Filesystem phase", "local")
    blob_dir = shared / "blobs" / str(session.id)
    blob_dir.mkdir(parents=True)
    (blob_dir / "payload.csv").write_bytes(b"row\n")
    entered = threading.Event()
    release = threading.Event()
    blocked_once = False
    manifest_fsync_completed = False

    if external_phase == "manifest_fsync":
        original_fsync_file = archive_quarantine_module._fsync_file

        def blocked_manifest_fsync(descriptor: int) -> None:
            nonlocal blocked_once, manifest_fsync_completed
            descriptor_stat = os.fstat(descriptor)
            descriptor_target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            is_manifest_temp = (
                descriptor_target.name == "manifest.json.tmp"
                and descriptor_target.parent.parent.name == str(session.id)
                and descriptor_target.parent.parent.parent.name == "v1"
            )
            if is_manifest_temp:
                path_stat = descriptor_target.lstat()
                assert (descriptor_stat.st_dev, descriptor_stat.st_ino) == (path_stat.st_dev, path_stat.st_ino)
            if is_manifest_temp and not blocked_once:
                blocked_once = True
                entered.set()
                assert release.wait(timeout=10)
            original_fsync_file(descriptor)
            if is_manifest_temp:
                manifest_fsync_completed = True

        monkeypatch.setattr(archive_quarantine_module, "_fsync_file", blocked_manifest_fsync)
    else:
        original_rename_noreplace_at = archive_quarantine_module._rename_noreplace_at

        def blocked_rename_noreplace_at(
            source_parent_fd: int,
            source_name: str,
            target_parent_fd: int,
            target_name: str,
        ) -> None:
            nonlocal blocked_once
            if not blocked_once:
                blocked_once = True
                entered.set()
                assert release.wait(timeout=10)
            original_rename_noreplace_at(
                source_parent_fd,
                source_name,
                target_parent_fd,
                target_name,
            )

        monkeypatch.setattr(archive_quarantine_module, "_rename_noreplace_at", blocked_rename_noreplace_at)

    archive_task = asyncio.create_task(first.archive_session(session.id))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        assert first_engine.pool.checkedout() == 0
        assert second_engine.pool.checkedout() == 0
    finally:
        release.set()
    await asyncio.wait_for(archive_task, timeout=10)
    assert blocked_once
    assert external_phase != "manifest_fsync" or manifest_fsync_completed
    assert not blob_dir.exists()
    assert list_archive_quarantine_manifests(shared, session.id) == ()


@pytest.mark.asyncio
async def test_postgres_failed_archive_restores_before_contender_can_acquire(
    deployment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_engine, second_engine, first, second, shared = deployment
    _register_instance(first_engine, first.session_operation_owner_instance_id)
    _register_instance(first_engine, second.session_operation_owner_instance_id)
    session = await first.create_session(f"pg-archive-current-{uuid4()}", "Rollback ordering", "local")
    blob_dir = shared / "blobs" / str(session.id)
    blob_dir.mkdir(parents=True)
    blob = blob_dir / "payload.csv"
    blob.write_bytes(b"row\n")
    delete_failed = False
    authority = first.session_operation_authority
    original_release = authority.release
    release_entered = threading.Event()
    release_allowed = threading.Event()

    def fail_first_archive_delete(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        nonlocal delete_failed
        normalized = " ".join(statement.lower().split())
        if not delete_failed and normalized.startswith("delete from sessions"):
            delete_failed = True
            raise RuntimeError("injected archive delete failure")

    def blocked_release(context: SessionOperationContext) -> None:
        release_entered.set()
        assert release_allowed.wait(timeout=10)
        original_release(context)

    event.listen(first_engine, "before_cursor_execute", fail_first_archive_delete)
    monkeypatch.setattr(authority, "release", blocked_release)
    archive_task = asyncio.create_task(first.archive_session(session.id))
    try:
        assert await asyncio.to_thread(release_entered.wait, 10)
        assert blob.read_bytes() == b"row\n"
        assert list_archive_quarantine_manifests(shared, session.id) == ()
        with second_engine.connect() as conn:
            fence_before_contender = conn.execute(
                select(
                    session_operation_fences_table.c.owner_instance_id,
                    session_operation_fences_table.c.released_at,
                ).where(session_operation_fences_table.c.session_id == str(session.id))
            ).one()
        assert fence_before_contender.owner_instance_id == first.session_operation_owner_instance_id
        assert fence_before_contender.released_at is None
        with pytest.raises(SessionOperationConflictError):
            await second.archive_session(session.id)
        assert not archive_task.done()
    finally:
        release_allowed.set()
        event.remove(first_engine, "before_cursor_execute", fail_first_archive_delete)

    with pytest.raises(RuntimeError, match="injected archive delete failure"):
        await asyncio.wait_for(archive_task, timeout=10)
    assert delete_failed
    assert blob.read_bytes() == b"row\n"
    assert list_archive_quarantine_manifests(shared, session.id) == ()

    await second.archive_session(session.id)
    with second_engine.connect() as conn:
        assert (
            conn.execute(select(func.count()).select_from(sessions_table).where(sessions_table.c.id == str(session.id))).scalar_one() == 0
        )
    assert not blob_dir.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("archive_action_visibility", ["direct", "lost_acknowledgement"])
async def test_postgres_consumed_archive_purges_exactly_once(
    deployment,
    monkeypatch: pytest.MonkeyPatch,
    archive_action_visibility: str,
) -> None:
    first_engine, second_engine, first, second, shared = deployment
    _register_instance(first_engine, first.session_operation_owner_instance_id)
    _register_instance(first_engine, second.session_operation_owner_instance_id)
    session = await first.create_session(f"pg-archive-consumed-{uuid4()}", "Consumed cleanup", "local")
    blob_dir = shared / "blobs" / str(session.id)
    blob_dir.mkdir(parents=True)
    (blob_dir / "payload.csv").write_bytes(b"row\n")
    purge_identities: list[ArchiveQuarantineIdentity] = []
    reconciliation_outcomes: list[ArchiveDeleteReconciliation] = []
    original_purge = purge_archive_quarantine

    def recording_purge(data_dir: Path, identity: ArchiveQuarantineIdentity, canonical: Path) -> None:
        purge_identities.append(identity)
        original_purge(data_dir, identity, canonical)

    monkeypatch.setattr(session_service_module, "purge_archive_quarantine", recording_purge)
    if archive_action_visibility == "lost_acknowledgement":
        authority = first.session_operation_authority
        original_archive_delete = authority.archive_delete
        original_reconcile_archive_delete = authority.reconcile_archive_delete

        def committed_then_lost_acknowledgement(context) -> None:
            original_archive_delete(context)
            raise ConnectionError("injected lost acknowledgement after commit")

        def recording_reconcile_archive_delete(context: SessionOperationContext) -> ArchiveDeleteReconciliation:
            outcome = original_reconcile_archive_delete(context)
            assert type(outcome) is ArchiveDeleteReconciliation
            reconciliation_outcomes.append(outcome)
            return outcome

        monkeypatch.setattr(authority, "archive_delete", committed_then_lost_acknowledgement)
        monkeypatch.setattr(authority, "reconcile_archive_delete", recording_reconcile_archive_delete)

    await first.archive_session(session.id)

    assert len(purge_identities) == 1
    assert purge_identities[0].session_id == session.id
    if archive_action_visibility == "lost_acknowledgement":
        assert reconciliation_outcomes == [ArchiveDeleteReconciliation.CONSUMED]
    else:
        assert reconciliation_outcomes == []
    assert list_archive_quarantine_manifests(shared, session.id) == ()
    assert not blob_dir.exists()
    with second_engine.connect() as conn:
        assert (
            conn.execute(select(func.count()).select_from(sessions_table).where(sessions_table.c.id == str(session.id))).scalar_one() == 0
        )


@pytest.mark.asyncio
async def test_postgres_winner_reconciles_stale_manifest_and_stale_archiver_cannot_touch_winner(
    deployment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_engine, _second_engine, first, second, shared = deployment
    _register_instance(first_engine, first.session_operation_owner_instance_id)
    _register_instance(first_engine, second.session_operation_owner_instance_id)
    session = await first.create_session(f"pg-archive-takeover-{uuid4()}", "Archive takeover", "local")
    blob_dir = shared / "blobs" / str(session.id)
    blob_dir.mkdir(parents=True)
    (blob_dir / "payload.csv").write_bytes(b"winner bytes\n")
    first_stage_entered = threading.Event()
    first_stage_release = threading.Event()
    first_identity: ArchiveQuarantineIdentity | None = None
    restore_identities: list[ArchiveQuarantineIdentity] = []
    purge_identities: list[ArchiveQuarantineIdentity] = []
    original_stage = stage_archive_quarantine
    original_restore = restore_archive_quarantine
    original_purge = purge_archive_quarantine

    def block_first_stage(data_dir: Path, identity: ArchiveQuarantineIdentity, canonical: Path) -> None:
        nonlocal first_identity
        original_stage(data_dir, identity, canonical)
        if first_identity is None:
            first_identity = identity
            first_stage_entered.set()
            assert first_stage_release.wait(timeout=10)

    def recording_restore(data_dir: Path, identity: ArchiveQuarantineIdentity, canonical: Path) -> None:
        restore_identities.append(identity)
        original_restore(data_dir, identity, canonical)

    def recording_purge(data_dir: Path, identity: ArchiveQuarantineIdentity, canonical: Path) -> None:
        purge_identities.append(identity)
        original_purge(data_dir, identity, canonical)

    monkeypatch.setattr(session_service_module, "stage_archive_quarantine", block_first_stage)
    monkeypatch.setattr(session_service_module, "restore_archive_quarantine", recording_restore)
    monkeypatch.setattr(session_service_module, "purge_archive_quarantine", recording_purge)

    stale_task = asyncio.create_task(first.archive_session(session.id))
    try:
        assert await asyncio.to_thread(first_stage_entered.wait, 10)
        assert first_identity is not None
        stale_paths = archive_quarantine_paths(shared, first_identity)
        assert not blob_dir.exists()
        assert (stale_paths.payload / "payload.csv").read_bytes() == b"winner bytes\n"
        _expire_archive_owner(
            first_engine,
            session_id=session.id,
            owner_instance_id=first.session_operation_owner_instance_id,
        )

        await asyncio.wait_for(second.archive_session(session.id), timeout=10)
        assert restore_identities == [first_identity]
        assert len(purge_identities) == 1
        assert purge_identities[0] != first_identity
        assert purge_identities[0].session_id == session.id
        assert not blob_dir.exists()
        assert list_archive_quarantine_manifests(shared, session.id) == ()
    finally:
        first_stage_release.set()

    with pytest.raises(SessionOperationFenceLost) as exc_info:
        await asyncio.wait_for(stale_task, timeout=10)
    assert exc_info.value.reason is FenceLossReason.MISSING
    assert restore_identities == [first_identity]
    assert len(purge_identities) == 1
    assert not blob_dir.exists()
    assert list_archive_quarantine_manifests(shared, session.id) == ()


@pytest.mark.asyncio
async def test_postgres_postcommit_purge_failure_remains_discoverable(
    deployment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_engine, second_engine, first, second, shared = deployment
    _register_instance(first_engine, first.session_operation_owner_instance_id)
    _register_instance(first_engine, second.session_operation_owner_instance_id)
    session = await first.create_session(f"pg-archive-purge-{uuid4()}", "Discoverable cleanup", "local")
    blob_dir = shared / "blobs" / str(session.id)
    blob_dir.mkdir(parents=True)
    (blob_dir / "payload.csv").write_bytes(b"recover me\n")

    def fail_payload_rmtree(path: str | os.PathLike[str]) -> None:
        payload = Path(path)
        assert payload.name == "payload"
        assert payload.parent.parent.name == str(session.id)
        raise OSError("secret purge path detail")

    monkeypatch.setattr(shutil, "rmtree", fail_payload_rmtree)
    with pytest.raises(QuarantineCleanupError) as exc_info:
        await first.archive_session(session.id)

    rendered = "\n".join(
        (
            str(exc_info.value),
            *(exc_info.value.__notes__ if hasattr(exc_info.value, "__notes__") else ()),
            "".join(traceback.format_exception(exc_info.value)),
        )
    )
    assert "secret purge path detail" not in rendered
    assert str(shared) not in rendered
    assert exc_info.value.__cause__ is None
    assert not blob_dir.exists()
    manifests = list_archive_quarantine_manifests(shared, session.id)
    assert len(manifests) == 1
    obligation = archive_quarantine_paths(shared, manifests[0].identity)
    assert (obligation.payload / "payload.csv").read_bytes() == b"recover me\n"
    with second_engine.connect() as conn:
        assert (
            conn.execute(select(func.count()).select_from(sessions_table).where(sessions_table.c.id == str(session.id))).scalar_one() == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault_phase", ["prepare", "stage", "archive_delete", "purge"])
async def test_postgres_archive_faults_never_touch_unrelated_session_rows(
    deployment,
    monkeypatch: pytest.MonkeyPatch,
    fault_phase: str,
) -> None:
    first_engine, second_engine, first, second, shared = deployment
    _register_instance(first_engine, first.session_operation_owner_instance_id)
    _register_instance(first_engine, second.session_operation_owner_instance_id)
    target = await first.create_session(f"pg-archive-fault-{uuid4()}", "Fault target", "local")
    unrelated = await first.create_session(f"pg-archive-control-{uuid4()}", "Untouched control", "local")
    await first.add_message(
        unrelated.id,
        "user",
        "control message",
        writer_principal="route_user_message",
    )
    target_dir = shared / "blobs" / str(target.id)
    control_dir = shared / "blobs" / str(unrelated.id)
    target_dir.mkdir(parents=True)
    control_dir.mkdir(parents=True)
    (target_dir / "target.csv").write_bytes(b"target\n")
    control_blob = control_dir / "control.csv"
    control_blob.write_bytes(b"control\n")
    control_before = _session_rows_snapshot(second_engine, unrelated.id)
    delete_listener = None
    fault_injected = False

    if fault_phase == "prepare":
        original_fsync_file = archive_quarantine_module._fsync_file

        def fail_manifest_fsync(descriptor: int) -> None:
            nonlocal fault_injected
            descriptor_target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            if descriptor_target.name == "manifest.json.tmp" and descriptor_target.parent.parent.name == str(target.id):
                fault_injected = True
                raise OSError(errno.EIO, "injected manifest fsync fault")
            original_fsync_file(descriptor)

        monkeypatch.setattr(archive_quarantine_module, "_fsync_file", fail_manifest_fsync)
    elif fault_phase == "stage":
        original_rename_noreplace_at = archive_quarantine_module._rename_noreplace_at

        def fail_payload_rename(
            source_parent_fd: int,
            source_name: str,
            target_parent_fd: int,
            target_name: str,
        ) -> None:
            nonlocal fault_injected
            if source_name == str(target.id) and target_name == "payload":
                fault_injected = True
                raise OSError(errno.EIO, "injected payload rename fault")
            original_rename_noreplace_at(
                source_parent_fd,
                source_name,
                target_parent_fd,
                target_name,
            )

        monkeypatch.setattr(archive_quarantine_module, "_rename_noreplace_at", fail_payload_rename)
    elif fault_phase == "archive_delete":

        def fail_archive_delete(_conn, _cursor, statement, _parameters, _context, _many) -> None:
            nonlocal fault_injected
            normalized = " ".join(statement.lower().split())
            if not fault_injected and normalized.startswith("delete from sessions"):
                fault_injected = True
                raise RuntimeError("injected archive delete fault")

        delete_listener = fail_archive_delete
        event.listen(first_engine, "before_cursor_execute", delete_listener)
    else:
        original_rmtree = shutil.rmtree

        def fail_payload_rmtree(path: str | os.PathLike[str]) -> None:
            nonlocal fault_injected
            payload = Path(path)
            if payload.name == "payload" and payload.parent.parent.name == str(target.id):
                fault_injected = True
                raise OSError(errno.EIO, "injected payload purge fault")
            original_rmtree(path)

        monkeypatch.setattr(shutil, "rmtree", fail_payload_rmtree)

    expected_error = {
        "prepare": OSError,
        "stage": OSError,
        "archive_delete": RuntimeError,
        "purge": QuarantineCleanupError,
    }[fault_phase]
    try:
        with pytest.raises(expected_error):
            await first.archive_session(target.id)
    finally:
        if delete_listener is not None:
            event.remove(first_engine, "before_cursor_execute", delete_listener)

    assert fault_injected
    assert _session_rows_snapshot(second_engine, unrelated.id) == control_before
    assert control_blob.read_bytes() == b"control\n"
    if fault_phase == "purge":
        with second_engine.connect() as conn:
            assert (
                conn.execute(select(func.count()).select_from(sessions_table).where(sessions_table.c.id == str(target.id))).scalar_one()
                == 0
            )
        manifests = list_archive_quarantine_manifests(shared, target.id)
        assert len(manifests) == 1
        assert (archive_quarantine_paths(shared, manifests[0].identity).payload / "target.csv").read_bytes() == b"target\n"
    else:
        assert (target_dir / "target.csv").read_bytes() == b"target\n"
        assert list_archive_quarantine_manifests(shared, target.id) == ()


def test_postgres_reverse_logical_pair_requests_both_complete(
    deployment,
) -> None:
    first_engine, second_engine, first, _second, _shared = deployment
    repository_a = PostgresSessionOperationRepository(first_engine)
    repository_b = PostgresSessionOperationRepository(second_engine)
    first_session = first.session_operation_authority.create_session_with_initial_fence(
        user_id=f"pair-a-{uuid4()}",
        title="Pair A",
        auth_provider_type="local",
        owner_instance_id=first.session_operation_owner_instance_id,
        lease_seconds=30,
    )
    second_session = first.session_operation_authority.create_session_with_initial_fence(
        user_id=f"pair-b-{uuid4()}",
        title="Pair B",
        auth_provider_type="local",
        owner_instance_id=first.session_operation_owner_instance_id,
        lease_seconds=30,
    )
    canonical_order = tuple(sorted((str(first_session.id), str(second_session.id))))
    start = threading.Barrier(3)
    first_requests = threading.Barrier(2)
    requests_guard = threading.Lock()
    requests: dict[int, list[str]] = {
        id(first_engine): [],
        id(second_engine): [],
    }
    transaction_session_lock = coordination_repository.transaction_session_lock  # type: ignore[attr-defined]

    @contextmanager
    def observe_advisory_lock_request(
        conn: Connection,
        engine: Engine,
        session_id: str,
    ) -> Iterator[None]:
        with requests_guard:
            engine_requests = requests[id(engine)]
            request_index = len(engine_requests)
            engine_requests.append(session_id)
        if request_index == 0:
            first_requests.wait(timeout=10)
            with requests_guard:
                observed_first = (
                    requests[id(first_engine)][0],
                    requests[id(second_engine)][0],
                )
            assert observed_first == (canonical_order[0], canonical_order[0])
        with transaction_session_lock(conn, engine, session_id):
            yield

    def lock_pair(
        repository: PostgresSessionOperationRepository,
        left: str,
        right: str,
    ) -> tuple[str, str]:
        start.wait(timeout=10)
        with repository._locked_pair_transaction(left, right):
            pass
        return left, right

    with (
        patch.object(
            coordination_repository,
            "transaction_session_lock",
            observe_advisory_lock_request,
        ),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        forward = pool.submit(
            lock_pair,
            repository_a,
            str(first_session.id),
            str(second_session.id),
        )
        reverse = pool.submit(
            lock_pair,
            repository_b,
            str(second_session.id),
            str(first_session.id),
        )
        start.wait(timeout=10)
        assert forward.result(timeout=5) == (
            str(first_session.id),
            str(second_session.id),
        )
        assert reverse.result(timeout=5) == (
            str(second_session.id),
            str(first_session.id),
        )

    assert tuple(requests[id(first_engine)]) == canonical_order
    assert tuple(requests[id(second_engine)]) == canonical_order
