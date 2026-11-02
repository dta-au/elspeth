"""Dormant V4 hold SQL contract: original source, capacity and once-only spend."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from sqlalchemy import insert, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.sessions.continuation_retained_capacity import (
    ContinuationRetainedCapacityExceeded,
    ProspectiveContinuationChunk,
    ProviderTerminalHoldSource,
    admit_retained_payload_chunks,
    bind_provider_terminal_hold_attempt,
    consume_provider_terminal_hold_after_complete_result,
    reserve_provider_terminal_hold,
)
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from tests.helpers.continuation_policy import continuation_test_settings

pytest_plugins = ("tests.unit.web.sessions.test_continuation_schema",)

_CHECKPOINT = "00000000-0000-4000-8000-000000000003"
_SESSION_OPERATION = "00000000-0000-4000-8000-000000000004"
_PHYSICAL_ATTEMPT = "00000000-0000-4000-8000-000000000005"
_LEDGER_ENTRY = "00000000-0000-4000-8000-000000000006"
_LEASE = "original-lease-token"
_EFFECT = "provider:0:0"
_ORIGINAL_SNAPSHOT = b"{}"


def _seed_original(conn, sid: str, operation_id: str, *, allowance: int) -> ProviderTerminalHoldSource:
    now = datetime.now(UTC)
    admitted = continuation_policy_v4_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=allowance))
    assert admitted is not None
    conn.execute(
        insert(jobs).values(
            session_id=sid,
            operation_id=operation_id,
            kind="compose_message",
            status="running",
            phase="executing",
            current_segment_sequence=1,
            ownership_generation=1,
            checkpoint_id=_CHECKPOINT,
            journal_cursor=0,
            ingress_phase="pending",
            reserved_user_message_id="original",
            request_hash="a" * 64,
            continuation_policy_version=admitted.schema_version,
            continuation_policy_sha256=admitted.policy_hash,
            continuation_retained_payload_limit_bytes=allowance,
            actor_user_id="alice",
            request_json="{}",
            claim_owner_instance_id="test-owner",
            attempt=1,
            session_operation_id=_SESSION_OPERATION,
            session_operation_lease_token=_LEASE,
            session_operation_epoch=1,
            created_at=now,
            updated_at=now,
            started_at=now,
        )
    )
    conn.execute(
        insert(chunks).values(
            session_id=sid,
            operation_id=operation_id,
            chunk_key="source-snapshot",
            codec="snapshot.v2",
            payload=_ORIGINAL_SNAPSHOT,
            sha256=sha256(_ORIGINAL_SNAPSHOT).hexdigest(),
            byte_length=len(_ORIGINAL_SNAPSHOT),
        )
    )
    conn.execute(
        insert(checkpoints).values(
            session_id=sid,
            operation_id=operation_id,
            checkpoint_id=_CHECKPOINT,
            kind="transition",
            cursor=0,
            snapshot_chunk_key="source-snapshot",
            frame_stack_version=1,
            created_at=now,
        )
    )
    conn.execute(
        insert(segments).values(
            session_id=sid,
            operation_id=operation_id,
            sequence=1,
            predecessor_sequence=None,
            state="running",
            generation=1,
            claim_token="original-claim",
            claim_owner_instance_id="test-owner",
            claim_expires_at=now + timedelta(minutes=1),
            session_operation_id=_SESSION_OPERATION,
            session_operation_lease_token=_LEASE,
            session_operation_epoch=1,
            enqueued_at=now,
            next_eligible_at=now,
            started_at=now,
            cooperative_at=now,
            deadline_at=now + timedelta(minutes=1),
            checkpoint_id=_CHECKPOINT,
            next_action_json="{}",
            policy_sha256=admitted.policy_hash,
        )
    )
    conn.execute(
        insert(attempts).values(
            session_id=sid,
            operation_id=operation_id,
            sequence=1,
            generation=1,
            claim_attempt=1,
            session_operation_id=_SESSION_OPERATION,
            session_operation_epoch=1,
            session_operation_lease_token=_LEASE,
            owner_instance_id="test-owner",
            started_at=now,
            claim_expires_at=now + timedelta(minutes=1),
        )
    )
    conn.execute(
        insert(effects).values(
            session_id=sid,
            operation_id=operation_id,
            effect_key=_EFFECT,
            input_sha256="b" * 64,
            owner="provider",
            disposition="prepared",
            creator_sequence=1,
            creator_generation=1,
        )
    )
    return ProviderTerminalHoldSource(
        session_id=sid,
        operation_id=operation_id,
        effect_key=_EFFECT,
        policy_sha256=admitted.policy_hash,
        allowance_bytes=allowance,
        source_checkpoint_id=_CHECKPOINT,
        source_cursor=0,
        source_snapshot_sha256=sha256(_ORIGINAL_SNAPSHOT).hexdigest(),
        source_input_sha256="b" * 64,
        creator_sequence=1,
        creator_generation=1,
        claim_attempt=1,
        session_operation_id=_SESSION_OPERATION,
        session_operation_epoch=1,
        lease_sha256=sha256(_LEASE.encode()).hexdigest(),
        reserved_bytes=8,
        fallback_contract_sha256=sha256(b"composer.paid-overflow.v1").hexdigest(),
    )


@pytest.fixture
def hold_store(continuation_unadmitted_parent):
    engine, sid, operation_id = continuation_unadmitted_parent
    with engine.begin() as conn:
        source = _seed_original(conn, sid, operation_id, allowance=12)
    return engine, source


def _reserve(conn, source: ProviderTerminalHoldSource) -> None:
    reserve_provider_terminal_hold(
        conn,
        source=source,
        prospective=(ProspectiveContinuationChunk("dispatch", b"12", "event.v2"),),
        created_at=datetime.now(UTC),
    )


def _bind_original_attempt(conn, source: ProviderTerminalHoldSource) -> None:
    conn.execute(
        insert(physical).values(
            attempt_id=_PHYSICAL_ATTEMPT,
            identity_id="alice",
            session_id=source.session_id,
            source="composer",
            started_at=datetime.now(UTC),
            operation_id=source.session_operation_id,
            operation_epoch=source.session_operation_epoch,
            lease_token=_LEASE,
        )
    )
    conn.execute(
        update(effects)
        .where(effects.c.session_id == source.session_id, effects.c.operation_id == source.operation_id, effects.c.effect_key == _EFFECT)
        .values(
            disposition="dispatched",
            provider_attempt_id=_PHYSICAL_ATTEMPT,
            physical_sequence=1,
            physical_generation=1,
            requested_model="test-model",
        )
    )
    bind_provider_terminal_hold_attempt(conn, source=source, provider_attempt_id=_PHYSICAL_ATTEMPT)


def test_exact_reservation_counts_historical_bytes_and_active_hold(hold_store):
    engine, source = hold_store
    with engine.begin() as conn:
        _reserve(conn, source)
        row = conn.execute(select(holds).where(holds.c.session_id == source.session_id)).one()
        assert row.state == "active" and row.provider_attempt_id is None and row.reserved_bytes == 8
        admit_retained_payload_chunks(
            conn,
            session_id=source.session_id,
            operation_id=source.operation_id,
            prospective=(ProspectiveContinuationChunk("fits", b"12", "event.v2"),),
            retained_payload_limit_bytes=source.allowance_bytes,
        )
        with pytest.raises(ContinuationRetainedCapacityExceeded):
            admit_retained_payload_chunks(
                conn,
                session_id=source.session_id,
                operation_id=source.operation_id,
                prospective=(ProspectiveContinuationChunk("one-short", b"123", "event.v2"),),
                retained_payload_limit_bytes=source.allowance_bytes,
            )


def test_one_byte_short_reservation_leaves_no_hold_or_dispatched_effect(continuation_unadmitted_parent):
    engine, sid, operation_id = continuation_unadmitted_parent
    with engine.begin() as conn:
        source = _seed_original(conn, sid, operation_id, allowance=11)
    with engine.begin() as conn:
        with pytest.raises(ContinuationRetainedCapacityExceeded):
            _reserve(conn, source)
        assert conn.execute(select(holds).where(holds.c.session_id == sid)).all() == []
        assert conn.execute(select(effects.c.disposition).where(effects.c.session_id == sid)).scalar_one() == "prepared"


def test_foreign_source_and_second_hold_cannot_take_original_reservation(hold_store):
    engine, source = hold_store
    with engine.begin() as conn:
        with pytest.raises(AuditIntegrityError, match="original provider effect"):
            _reserve(conn, replace(source, source_input_sha256="c" * 64))
        _reserve(conn, source)
        with pytest.raises(AuditIntegrityError, match="already exists"):
            _reserve(conn, source)
        assert conn.execute(select(holds.c.effect_key).where(holds.c.session_id == source.session_id)).all() == [(_EFFECT,)]


def test_bound_attempt_is_unique_and_only_original_credit_can_be_used(hold_store):
    engine, source = hold_store
    with engine.begin() as conn:
        _reserve(conn, source)
        _bind_original_attempt(conn, source)
        with pytest.raises(AuditIntegrityError, match="already bound"):
            bind_provider_terminal_hold_attempt(conn, source=source, provider_attempt_id=_PHYSICAL_ATTEMPT)
        with pytest.raises(ContinuationRetainedCapacityExceeded):
            admit_retained_payload_chunks(
                conn,
                session_id=source.session_id,
                operation_id=source.operation_id,
                prospective=(ProspectiveContinuationChunk("large", b"0123456789", "event.v2"),),
                retained_payload_limit_bytes=source.allowance_bytes,
            )
        admit_retained_payload_chunks(
            conn,
            session_id=source.session_id,
            operation_id=source.operation_id,
            prospective=(ProspectiveContinuationChunk("large", b"0123456789", "event.v2"),),
            retained_payload_limit_bytes=source.allowance_bytes,
            consuming_hold=source,
        )
        with pytest.raises(AuditIntegrityError, match="original source"):
            admit_retained_payload_chunks(
                conn,
                session_id=source.session_id,
                operation_id=source.operation_id,
                prospective=(),
                retained_payload_limit_bytes=source.allowance_bytes,
                consuming_hold=replace(source, source_input_sha256="c" * 64),
            )


def test_hold_consumes_once_only_after_complete_settlement(hold_store):
    engine, source = hold_store
    with engine.begin() as conn:
        _reserve(conn, source)
        _bind_original_attempt(conn, source)
        now = datetime.now(UTC)
        with pytest.raises(AuditIntegrityError, match="before complete paid settlement"):
            consume_provider_terminal_hold_after_complete_result(
                conn, source=source, provider_attempt_id=_PHYSICAL_ATTEMPT, consumed_at=now
            )
        conn.execute(
            insert(chunks).values(
                session_id=source.session_id,
                operation_id=source.operation_id,
                chunk_key="complete-response",
                codec="composer.response.v1",
                payload=b"{}",
                sha256=sha256(b"{}").hexdigest(),
                byte_length=2,
            )
        )
        conn.execute(
            insert(ledger).values(
                entry_id=_LEDGER_ENTRY,
                identity_id="alice",
                source="composer",
                session_id=source.session_id,
                model="test-model",
                prompt_tokens=1,
                completion_tokens=1,
                recorded_at=now,
            )
        )
        conn.execute(
            update(physical).where(physical.c.attempt_id == _PHYSICAL_ATTEMPT).values(settled_at=now, ledger_entry_id=_LEDGER_ENTRY)
        )
        conn.execute(
            update(effects)
            .where(
                effects.c.session_id == source.session_id, effects.c.operation_id == source.operation_id, effects.c.effect_key == _EFFECT
            )
            .values(disposition="committed", receipt_key="audit:complete", result_chunk_key="complete-response", response_finished_at=now)
        )
        consume_provider_terminal_hold_after_complete_result(conn, source=source, provider_attempt_id=_PHYSICAL_ATTEMPT, consumed_at=now)
        with pytest.raises(AuditIntegrityError, match="missing or foreign paid attempt"):
            consume_provider_terminal_hold_after_complete_result(
                conn, source=source, provider_attempt_id=_PHYSICAL_ATTEMPT, consumed_at=now
            )
        assert conn.execute(select(holds.c.state).where(holds.c.session_id == source.session_id)).scalar_one() == "consumed"
        admit_retained_payload_chunks(
            conn,
            session_id=source.session_id,
            operation_id=source.operation_id,
            prospective=(ProspectiveContinuationChunk("after", b"01234567", "event.v2"),),
            retained_payload_limit_bytes=source.allowance_bytes,
        )
