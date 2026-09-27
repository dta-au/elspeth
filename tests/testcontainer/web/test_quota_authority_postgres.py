"""QuotaAuthority on PostgreSQL: the timestamptz day window, the NULL-aware aggregate and R14 (Task I1).

SQLite stores ``recorded_at`` as text and PostgreSQL as ``timestamptz``, so the
UTC-midnight boundary and the unknown-row count (a ``SUM`` over a ``CASE``
expression) are proven on the production dialect, not inferred from the unit run.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
import structlog
from sqlalchemy import insert, select, text
from sqlalchemy.engine import make_url
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.fenced_session import CONTAINER_TOKENS_PER_DAY, IDENTITY_TOKENS_PER_DAY, FencedSession, seed_token_policies

from elspeth.contracts.blobs import IdentityStorageQuotaExceededError
from elspeth.contracts.chargeable_admission import (
    AdmissionRefusalReason,
    ChargeableAdmissionPolicy,
    ChargeableAdmissionRefused,
    QuotaDisposition,
)
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.coordination import chargeable_admission_authority
from elspeth.web.coordination.chargeable_admission_authority import RepositoryChargeableAdmissionAuthority
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.mutation_connection_registry import (
    _register_mutation_connection,
    _resolve_mutation_connection,
    _unregister_mutation_connection,
)
from elspeth.web.coordination.quota_authority import (
    QuotaExceeded,
    RepositoryQuotaAuthority,
    TokenUsageEntry,
    admit_storage_bytes_on_connection,
    begin_provider_attempt_on_connection,
    cancel_undispatched_provider_attempt_on_connection,
)
from elspeth.web.coordination.repository import PostgresSessionOperationRepository
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY
from elspeth.web.sessions import service as session_service_module
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    blobs_table,
    chat_messages_table,
    quota_policies_table,
    quota_provider_attempts_table,
    sessions_table,
    token_usage_ledger_table,
)
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

pytestmark = pytest.mark.testcontainer

DAY = datetime(2026, 9, 13, tzinfo=UTC)


@pytest.fixture
def pg_fenced(external_deployment_postgres_url: str) -> Iterator[FencedSession]:
    """A real PostgreSQL session transaction over an isolated database."""
    database = f"quota_{uuid.uuid4().hex}"
    control = create_session_engine(external_deployment_postgres_url, isolation_level="AUTOCOMMIT")
    with control.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
    engine = create_session_engine(make_url(external_deployment_postgres_url).set(database=database).render_as_string(hide_password=False))
    try:
        initialize_session_schema(engine)
        with engine.begin() as conn:
            ensure_test_identity(conn, identity_id="alice")
        session = PostgresSessionOperationRepository(engine).create_session_with_initial_fence(
            user_id="alice", title="fenced", auth_provider_type="local", owner_instance_id="owner", lease_seconds=120
        )
        with engine.connect() as connection, connection.begin() as transaction:
            token = _register_mutation_connection(connection)
            try:
                yield FencedSession(engine=engine, connection_token=token, identity_id="alice", session_id=str(session.id))
            finally:
                _unregister_mutation_connection(token)
                transaction.rollback()
    finally:
        engine.dispose()
        with control.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
        control.dispose()


def _record(fenced: FencedSession, prompt: int | None, completion: int | None, *, at: datetime) -> None:
    RepositoryQuotaAuthority.record_token_usage(
        fenced.connection_token,
        session_id=fenced.session_id,
        source="composer",
        run_id=None,
        entries=(
            TokenUsageEntry(
                model="m", prompt_tokens=prompt, completion_tokens=completion, cached_prompt_tokens=None, reasoning_tokens=None
            ),
        ),
        recorded_at=at,
    )


@pytest.mark.asyncio
async def test_run_sync_quota_callbacks_hold_advisory_lock_until_receipt(pg_fenced: FencedSession, monkeypatch: pytest.MonkeyPatch) -> None:
    with pg_fenced.engine.begin() as conn:
        seed_token_policies(conn, identity_id=pg_fenced.identity_id)
    policy = ChargeableAdmissionPolicy(
        identity_token_quota_configured=True,
        secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash,
    )
    service = SessionServiceImpl(
        pg_fenced.engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.run_quota_postgres"),
        chargeable_admission_policy=policy,
    )
    session_id = uuid.UUID(pg_fenced.session_id)
    async with await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    ) as compose_lease:
        state = await service.save_composition_state(
            session_id,
            CompositionStateData(is_valid=True),
            provenance="session_seed",
            session_operation_context=compose_lease.context,
        )
    async with await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session_id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    ) as execute_lease:
        run = await service.create_run(session_id, state.id, session_operation_context=execute_lease.context)
        entered = Event()
        release = Event()
        original = session_service_module.begin_provider_attempt_on_connection

        def first(*args, **kwargs):
            attempt = original(*args, **kwargs)
            if not entered.is_set():
                entered.set()
                if not release.wait(10):
                    raise AssertionError("first transaction was not released")
            return attempt

        monkeypatch.setattr(session_service_module, "begin_provider_attempt_on_connection", first)
        first_call = asyncio.create_task(
            run_sync_in_worker(service.begin_run_provider_attempt_sync, session_operation_context=execute_lease.context, run_id=run.id)
        )
        try:
            assert await run_sync_in_worker(entered.wait, 5)
            # The first writer holds its transaction open. The peer must wait
            # for the PostgreSQL session/identity lock, then get its own ID.
            second = SessionServiceImpl(
                pg_fenced.engine,
                telemetry=build_sessions_telemetry(),
                log=structlog.get_logger("test.run_quota_postgres_peer"),
                chargeable_admission_policy=policy,
            )
            second_call = asyncio.create_task(
                run_sync_in_worker(second.begin_run_provider_attempt_sync, session_operation_context=execute_lease.context, run_id=run.id)
            )
            await asyncio.sleep(0.2)
            assert not first_call.done() and not second_call.done()
        finally:
            release.set()
        first_attempt, second_attempt = await asyncio.gather(first_call, second_call)
        assert first_attempt.attempt_id != second_attempt.attempt_id
        first_entry = TokenUsageEntry(
            model="test/model",
            prompt_tokens=3,
            completion_tokens=2,
            cached_prompt_tokens=0,
            reasoning_tokens=0,
            call_id="pg-run-call-1",
            recorded_at=datetime.now(UTC),
        )
        second_entry = TokenUsageEntry(
            model="test/model",
            prompt_tokens=4,
            completion_tokens=1,
            cached_prompt_tokens=0,
            reasoning_tokens=0,
            call_id="pg-run-call-2",
            recorded_at=datetime.now(UTC),
        )
        await run_sync_in_worker(
            service.settle_run_provider_attempt_sync,
            session_operation_context=execute_lease.context,
            attempt_id=first_attempt.attempt_id,
            entry=first_entry,
        )
        await run_sync_in_worker(
            second.settle_run_provider_attempt_sync,
            session_operation_context=execute_lease.context,
            attempt_id=second_attempt.attempt_id,
            entry=second_entry,
        )
        with pg_fenced.engine.connect() as conn:
            attempts = conn.execute(
                select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.run_id == str(run.id))
            ).all()
            ledger = conn.execute(select(token_usage_ledger_table).where(token_usage_ledger_table.c.run_id == str(run.id))).all()
        assert len(attempts) == len(ledger) == 2
        assert {row.ledger_entry_id for row in attempts} == {row.entry_id for row in ledger}


def test_daily_total_window_and_unknown_measure_on_postgres(pg_fenced: FencedSession) -> None:
    _record(pg_fenced, 1000, 1000, at=DAY - timedelta(microseconds=1))
    _record(pg_fenced, 100, 50, at=DAY)
    _record(pg_fenced, 10, 5, at=DAY + timedelta(hours=23, minutes=59, seconds=59, microseconds=999999))
    _record(pg_fenced, 1000, 1000, at=DAY + timedelta(days=1))
    assert (
        RepositoryQuotaAuthority.daily_token_total(pg_fenced.connection_token, identity_id=pg_fenced.identity_id, day_start_utc=DAY) == 165
    )
    _record(pg_fenced, None, 5, at=DAY + timedelta(hours=3))
    assert (
        RepositoryQuotaAuthority.daily_token_total(pg_fenced.connection_token, identity_id=pg_fenced.identity_id, day_start_utc=DAY) is None
    )


def test_r14_refuses_and_derives_from_the_usage_on_postgres(pg_fenced: FencedSession, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_token_policies(_resolve_mutation_connection(pg_fenced.connection_token), identity_id=pg_fenced.identity_id)
    monkeypatch.setattr(chargeable_admission_authority, "database_now", lambda _conn: DAY + timedelta(hours=12))
    policy = ChargeableAdmissionPolicy(secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash)
    _record(pg_fenced, 999, 0, at=DAY + timedelta(hours=1))
    within = RepositoryChargeableAdmissionAuthority.assess(pg_fenced.connection_token, session_id=pg_fenced.session_id, policy=policy)
    assert within.allowed
    assert within.evidence.quota_disposition is QuotaDisposition.WITHIN_CAP
    _record(pg_fenced, 1, 0, at=DAY + timedelta(hours=2))
    refused = RepositoryChargeableAdmissionAuthority.assess(pg_fenced.connection_token, session_id=pg_fenced.session_id, policy=policy)
    assert refused.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED
    assert (refused.evidence.cap, refused.evidence.ceiling, refused.evidence.usage) == (
        IDENTITY_TOKENS_PER_DAY,
        CONTAINER_TOKENS_PER_DAY,
        1000,
    )


def test_daily_total_widens_before_adding_token_columns(pg_fenced: FencedSession) -> None:
    _record(pg_fenced, 2_000_000_000, 2_000_000_000, at=DAY)
    assert (
        RepositoryQuotaAuthority.daily_token_total(pg_fenced.connection_token, identity_id=pg_fenced.identity_id, day_start_utc=DAY)
        == 4_000_000_000
    )


def test_undispatched_cancellation_is_atomic_and_idempotent_on_postgres(pg_fenced: FencedSession) -> None:
    from elspeth.contracts.errors import AuditIntegrityError

    conn = _resolve_mutation_connection(pg_fenced.connection_token)
    context = SessionOperationContext(
        fence=SessionOperationFence(session_id=pg_fenced.session_id, operation_id="operation", lease_token="lease", operation_epoch=1),
        operation_kind=SessionOperationKind.COMPOSE,
    )
    policy = ChargeableAdmissionPolicy(secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash)
    attempt = begin_provider_attempt_on_connection(conn, session_operation_context=context, source="composer", policy=policy)

    def append_event(event_id: str, content: str, created_at: datetime) -> None:
        conn.execute(
            insert(chat_messages_table).values(
                id=event_id,
                session_id=pg_fenced.session_id,
                role="audit",
                content=content,
                raw_content=None,
                tool_calls=None,
                sequence_no=1,
                writer_principal="compose_loop",
                composition_state_id=None,
                tool_call_id=None,
                parent_assistant_id=None,
                created_at=created_at,
            )
        )

    for _ in range(2):
        cancel_undispatched_provider_attempt_on_connection(
            conn,
            session_operation_context=context,
            attempt_id=attempt.attempt_id,
            requested_model="test/model",
            append_audit_event=append_event,
        )
    events = conn.execute(select(chat_messages_table)).all()
    ledger = conn.execute(select(token_usage_ledger_table)).all()
    stored_attempt = conn.execute(select(quota_provider_attempts_table)).one()
    assert len(events) == len(ledger) == 1
    assert events[0].tool_calls is None
    assert (ledger[0].prompt_tokens, ledger[0].completion_tokens, ledger[0].cached_prompt_tokens, ledger[0].reasoning_tokens) == (
        0,
        0,
        0,
        0,
    )
    assert events[0].created_at == ledger[0].recorded_at
    assert stored_attempt.settled_at is not None
    assert stored_attempt.ledger_entry_id == ledger[0].entry_id
    with pytest.raises(AuditIntegrityError):
        cancel_undispatched_provider_attempt_on_connection(
            conn,
            session_operation_context=context,
            attempt_id=attempt.attempt_id,
            requested_model="different/model",
            append_audit_event=append_event,
        )


def test_pending_attempt_admission_serializes_on_identity_lock(pg_fenced: FencedSession) -> None:
    conn = _resolve_mutation_connection(pg_fenced.connection_token)
    seed_token_policies(conn, identity_id=pg_fenced.identity_id)
    conn.commit()
    policy = ChargeableAdmissionPolicy(secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash)
    first_written = Event()
    release_first = Event()
    second_started = Event()
    second_pid: list[int] = []

    def context(epoch: int) -> SessionOperationContext:
        return SessionOperationContext(
            fence=SessionOperationFence(
                session_id=pg_fenced.session_id, operation_id="operation", lease_token="lease", operation_epoch=epoch
            ),
            operation_kind=SessionOperationKind.COMPOSE,
        )

    def first() -> None:
        with pg_fenced.engine.begin() as owned:
            begin_provider_attempt_on_connection(owned, session_operation_context=context(1), source="composer", policy=policy)
            first_written.set()
            assert release_first.wait(10)

    def second() -> AdmissionRefusalReason | None:
        with pg_fenced.engine.begin() as owned:
            second_pid.append(owned.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            second_started.set()
            try:
                begin_provider_attempt_on_connection(owned, session_operation_context=context(2), source="composer", policy=policy)
            except ChargeableAdmissionRefused as exc:
                return exc.decision.refusal_reason
        return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        first_result = workers.submit(first)
        try:
            assert first_written.wait(5)
            second_result = workers.submit(second)
            assert second_started.wait(5)
            deadline = time.monotonic() + 5
            blocked = False
            while time.monotonic() < deadline:
                with pg_fenced.engine.connect() as observer:
                    blocked = observer.execute(
                        text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"), {"pid": second_pid[0]}
                    ).scalar_one()
                if blocked:
                    break
                time.sleep(0.01)
            assert blocked, "second admission must wait on the first identity lock"
        finally:
            release_first.set()
        first_result.result(timeout=5)
        assert second_result.result(timeout=5) is AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE


def test_storage_admission_joins_archived_session_blobs_on_postgres(pg_fenced: FencedSession) -> None:
    conn = _resolve_mutation_connection(pg_fenced.connection_token)
    conn.execute(
        insert(quota_policies_table).values(
            policy_id="storage-alice",
            identity_id="alice",
            tokens_per_day=1000,
            storage_bytes=100,
            set_by_actor="operator",
            set_by_identity_id=None,
            set_at=DAY,
        )
    )
    archived_id = str(uuid.uuid4())
    blob_id = str(uuid.uuid4())
    conn.execute(
        insert(sessions_table).values(
            id=archived_id,
            user_id="alice",
            auth_provider_type="local",
            title="archived",
            created_at=DAY,
            updated_at=DAY,
            archived_at=DAY,
        )
    )
    conn.execute(
        insert(blobs_table).values(
            id=blob_id,
            session_id=archived_id,
            filename="held.csv",
            mime_type="text/csv",
            size_bytes=90,
            content_hash=None,
            storage_path=f"/nonexistent/{blob_id}.csv",
            created_at=DAY,
            created_by="user",
            source_description=None,
            status="pending",
        )
    )
    recorded: list[QuotaExceeded] = []
    with pytest.raises(IdentityStorageQuotaExceededError):
        RepositoryQuotaAuthority.admit_storage_bytes(
            pg_fenced.connection_token,
            session_id=pg_fenced.session_id,
            additional_bytes=11,
            operation="blob_create",
            record=recorded.append,
        )
    assert [(row.dimension, row.usage, row.cap) for row in recorded] == [("storage", 90, 100)]


def test_storage_admission_does_not_wait_on_container_policy_row_lock(pg_fenced: FencedSession) -> None:
    with pg_fenced.engine.begin() as setup:
        setup.execute(
            insert(quota_policies_table).values(
                policy_id="storage-container",
                identity_id=None,
                tokens_per_day=1000,
                storage_bytes=100,
                set_by_actor="operator",
                set_by_identity_id=None,
                set_at=DAY,
            )
        )

    def admit() -> int | None:
        with pg_fenced.engine.begin() as other:
            return admit_storage_bytes_on_connection(
                other,
                session_id=pg_fenced.session_id,
                additional_bytes=1,
                operation="blob_create",
                record=lambda _outcome: pytest.fail("unexpected storage refusal"),
            ).usage

    with ThreadPoolExecutor(max_workers=1) as workers, pg_fenced.engine.begin() as locked:
        locked.execute(
            select(quota_policies_table.c.policy_id).where(quota_policies_table.c.policy_id == "storage-container").with_for_update()
        )
        future = workers.submit(admit)
        assert future.result(timeout=5) == 0
