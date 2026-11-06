"""Actual P4 product controls and historical outcome ownership on PostgreSQL."""

import asyncio
from datetime import timedelta

import psycopg2.extensions
import pytest
from sqlalchemy import select, update
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target, operation_store
from tests.testcontainer.web.test_session_operation_fence_postgres import _register_instance
from tests.unit.web.coordination.test_continuation_p4_recovery_start import new_owners
from tests.unit.web.sessions.test_continuation_checkpoint_outcome import (
    test_actual_p4_binds_current_checkpoint_and_refuses_a_coherent_prior_recurrence,
)
from tests.unit.web.sessions.test_continuation_live_phases import (
    _advance,
    _phase_case,
    test_real_stop_reads_and_publishes_only_the_actual_completed_prefix,
)
from tests.unit.web.sessions.test_continuation_p4_product import (
    test_actual_admitted_source_and_once_transfer_keep_original_ownership,
    test_actual_committed_p4_readback_survives_terminal_progress_without_reminting_write_authority,
    test_actual_completed_application_requires_closed_original_dependencies,
    test_actual_p4_one_physical_outcome_keeps_atomic_and_uncertain_boundaries,
    test_actual_unavailable_unwind_keeps_original_failure_and_one_physical_owner,
)
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.coordination.repository import PostgresSessionOperationRepository
from elspeth.web.required_sql_outcomes import RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.composer_operations import ComposerOperationFenceLost, ComposerOperationRunning
from elspeth.web.sessions.continuation import require_continuation_mutation
from elspeth.web.sessions.continuation_p4 import read_p4_transaction_outcome_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts

pytestmark = pytest.mark.testcontainer

__all__ = [
    "operation_postgres_target",
    "operation_store",
    "test_actual_admitted_source_and_once_transfer_keep_original_ownership",
    "test_actual_committed_p4_readback_survives_terminal_progress_without_reminting_write_authority",
    "test_actual_completed_application_requires_closed_original_dependencies",
    "test_actual_p4_binds_current_checkpoint_and_refuses_a_coherent_prior_recurrence",
    "test_actual_p4_one_physical_outcome_keeps_atomic_and_uncertain_boundaries",
    "test_actual_p4_readback_binds_history_after_a_different_peer_generation",
    "test_actual_unavailable_unwind_keeps_original_failure_and_one_physical_owner",
    "test_real_stop_reads_and_publishes_only_the_actual_completed_prefix",
]


@pytest.fixture(autouse=True)
def _actual_postgres_backend(operation_store, record_property):
    engine, repository, _authority, _sessions, _sid = operation_store
    assert engine.dialect.name == "postgresql" and engine.dialect.driver == "psycopg2"
    assert type(repository) is PostgresSessionOperationRepository
    with engine.connect() as conn:
        assert isinstance(conn.connection.driver_connection, psycopg2.extensions.connection)
    record_property("p4_actual_backend", "postgresql+psycopg2")
    record_property("p4_actual_repository", "PostgresSessionOperationRepository")


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ("none", "predecessor", "event", "creator"))
async def test_actual_p4_readback_binds_history_after_a_different_peer_generation(operation_store, tmp_path, monkeypatch, changed):
    engine, _repository, authority, _sessions, sid = operation_store
    second = None
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, first, primary, sdk):
        # Complete both actual tools and publish normally. A durable Stop would
        # prohibit this recovery, so the separate original Stop oracle remains
        # the source for that disposition rather than clearing a Stop marker.
        await _advance(owner)
        await _advance(owner)
        await _advance(owner)
        phase = owner.p4
        assert phase is not None and phase.application is not None and phase.committed_outcome is not None
        assert phase.writer_observation is not None and phase.writer_observation.committed
        observation = phase.writer_observation
        composite, physical = phase.committed_outcome, phase.physical
        assert type(physical) is RequiredSQLReturned and physical.value is composite and composite.application is phase.application
        assert primary is owner.primary and len(sdk) == 1
        sql = tuple(ticket for ticket in owner.work.coordinator.tickets if ticket.key.source is RequiredWorkSource.COMPOSE_CHECKPOINT_SQL)
        projections = tuple(
            ticket for ticket in owner.work.coordinator.tickets if ticket.key.source is RequiredWorkSource.COMPOSE_CHECKPOINT_PROJECTION
        )
        assert len(sql) == len(projections) == 1 and sql[0].complete and projections[0].complete
        original_future = sql[0]._future
        assert original_future is not None and original_future.done() and sql[0]._finish_once_handoff is physical
        original_head = authority.load_continuation_snapshot(first)
        assert original_head == composite.application.snapshot and phase.application_sql is None

        def read_outcome():
            with locked_session_transaction(engine, str(sid)) as conn:
                return read_p4_transaction_outcome_on_connection(conn, observation, env_ref_names=phase.inventory.names)

        assert await asyncio.to_thread(read_outcome) is composite.audit_outcome
        await owner.lease.close()
        _engine, peer_repository, _same_instance_authority, _peer_sessions, _sid = new_owners(operation_store)
        peer_id = "p4-history-peer"
        _register_instance(engine, instance_id=peer_id, lease_delta=timedelta(minutes=10))
        peer_authority = ComposerAsyncOperationAuthority(
            engine,
            owner_instance_id=peer_id,
            claim_lease_seconds=authority._claim_lease_seconds,
            continuation_policy=authority._continuation_policy,
        )
        try:
            assert (
                peer_repository.recover_composer_segment(
                    session_id=sid,
                    operation_id=first.claim.operation_id,
                    expected_generation=first.claim.ownership_generation,
                    actor_user_id="alice",
                    auth_provider_type="local",
                )
                is True
            )
            (claim,) = peer_authority.claim_segment(limit=1)
            context = peer_repository.start_composer_segment(claim, owner_instance_id=peer_id, lease_seconds=30, auth_provider_type="local")
            second = ComposerOperationRunning(claim, context)
            assert claim.segment_sequence == first.claim.segment_sequence
            assert claim.ownership_generation == first.claim.ownership_generation + 1
            assert claim.attempt == first.claim.attempt + 1 and claim.claim_token != first.claim.claim_token
            assert context.fence.operation_id != first.session_operation_context.fence.operation_id
            assert context.fence.operation_epoch > first.session_operation_context.fence.operation_epoch
            assert context.fence.lease_token != first.session_operation_context.fence.lease_token
            with engine.connect() as conn:
                peer_instance = conn.execute(
                    select(attempts.c.owner_instance_id).where(
                        attempts.c.session_id == str(sid),
                        attempts.c.operation_id == claim.operation_id,
                        attempts.c.sequence == claim.segment_sequence,
                        attempts.c.generation == claim.ownership_generation,
                    )
                ).scalar_one()
            assert peer_instance == peer_id and peer_instance != authority._owner_instance_id
            peer_head = peer_authority.load_continuation_snapshot(second)
            before = stored_rows(engine)
            assert await asyncio.to_thread(read_outcome) is composite.audit_outcome
            assert observation.application_candidate is composite.application and observation.candidate is composite.audit_outcome
            assert observation.readback is not None and observation.original_error is None
            with pytest.raises((ComposerOperationFenceLost, SessionOperationFenceLost)):
                peer_authority.load_continuation_snapshot(first)

            with locked_session_transaction(engine, str(sid)) as conn:
                assert require_continuation_mutation(conn, second.session_operation_context) is True
                with pytest.raises((ComposerOperationFenceLost, SessionOperationFenceLost)):
                    require_continuation_mutation(conn, first.session_operation_context)
                # This proof is historical DATA from the actual old invocation;
                # it cannot revive the old mutation fence just rejected above.
                assert (
                    read_p4_transaction_outcome_on_connection(conn, observation, env_ref_names=phase.inventory.names)
                    is composite.audit_outcome
                )
                if changed != "none":
                    savepoint = conn.begin_nested()
                    try:
                        record = composite.application.bundle.entry.applications[-1]
                        where = (checkpoints.c.session_id == str(sid), checkpoints.c.operation_id == first.claim.operation_id)
                        if changed == "predecessor":
                            prepared_key = conn.execute(
                                select(checkpoints.c.snapshot_chunk_key).where(
                                    *where,
                                    checkpoints.c.checkpoint_id == observation.commit.expected_checkpoint_id,
                                )
                            ).scalar_one()
                            pending_key = conn.execute(
                                select(checkpoints.c.snapshot_chunk_key).where(
                                    *where,
                                    checkpoints.c.checkpoint_id == record.checkpoint_id,
                                )
                            ).scalar_one()
                            assert prepared_key != pending_key
                            result = conn.execute(
                                update(checkpoints)
                                .where(
                                    *where,
                                    checkpoints.c.checkpoint_id == record.checkpoint_id,
                                )
                                .values(snapshot_chunk_key=prepared_key)
                            )
                            assert result.rowcount == 1
                            assert (
                                conn.execute(
                                    select(checkpoints.c.snapshot_chunk_key).where(
                                        *where,
                                        checkpoints.c.checkpoint_id == record.checkpoint_id,
                                    )
                                ).scalar_one()
                                == prepared_key
                            )
                            refusal = "pending result's original source relation"
                        elif changed == "event":
                            event_where = (events.c.session_id == str(sid), events.c.operation_id == first.claim.operation_id)
                            other_key = conn.execute(
                                select(events.c.payload_chunk_key).where(
                                    *event_where,
                                    events.c.sequence == observation.commit.expected_cursor,
                                )
                            ).scalar_one()
                            applied_cursor = composite.application.snapshot.cursor
                            original_key = conn.execute(
                                select(events.c.payload_chunk_key).where(
                                    *event_where,
                                    events.c.sequence == applied_cursor,
                                )
                            ).scalar_one()
                            assert other_key != original_key
                            result = conn.execute(
                                update(events).where(*event_where, events.c.sequence == applied_cursor).values(payload_chunk_key=other_key)
                            )
                            assert result.rowcount == 1
                            assert (
                                conn.execute(
                                    select(events.c.payload_chunk_key).where(*event_where, events.c.sequence == applied_cursor)
                                ).scalar_one()
                                == other_key
                            )
                            refusal = "actual application event/predecessor"
                        else:
                            assert changed == "creator"
                            creator_where = (
                                attempts.c.session_id == str(sid),
                                attempts.c.operation_id == first.claim.operation_id,
                                attempts.c.sequence == first.claim.segment_sequence,
                                attempts.c.generation == first.claim.ownership_generation,
                            )
                            result = conn.execute(update(attempts).where(*creator_where).values(claim_attempt=first.claim.attempt + 1))
                            assert result.rowcount == 1
                            assert (
                                conn.execute(select(attempts.c.claim_attempt).where(*creator_where)).scalar_one() == first.claim.attempt + 1
                            )
                            refusal = "actual creator/principal/source"
                        # All active constraints/FKs accepted the controlled
                        # source change. Only this real reader call is expected
                        # to refuse; a setup SQL error cannot satisfy the proof.
                        with pytest.raises(AuditIntegrityError, match=refusal):
                            read_p4_transaction_outcome_on_connection(conn, observation, env_ref_names=phase.inventory.names)
                    finally:
                        savepoint.rollback()
                    assert (
                        read_p4_transaction_outcome_on_connection(conn, observation, env_ref_names=phase.inventory.names)
                        is composite.audit_outcome
                    )
            assert stored_rows(engine) == before and peer_authority.load_continuation_snapshot(second) == peer_head
            assert sql[0]._future is original_future and sql[0]._finish_once_handoff is physical and physical.value is composite
            assert phase.application is composite.application and phase.application_sql is None and len(sdk) == 1
        finally:
            if second is not None:
                peer_repository.release(second.session_operation_context)
