"""QuotaAuthority on PostgreSQL: the timestamptz day window, the NULL-aware aggregate and R14 (Task I1).

SQLite stores ``recorded_at`` as text and PostgreSQL as ``timestamptz``, so the
UTC-midnight boundary and the unknown-row count (a ``SUM`` over a ``CASE``
expression) are proven on the production dialect, not inferred from the unit run.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.fenced_session import CONTAINER_TOKENS_PER_DAY, IDENTITY_TOKENS_PER_DAY, FencedSession, seed_token_policies

from elspeth.contracts.chargeable_admission import (
    AdmissionRefusalReason,
    ChargeableAdmissionPolicy,
    ChargeableAdmissionRefused,
    QuotaDisposition,
)
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.coordination import chargeable_admission_authority
from elspeth.web.coordination.chargeable_admission_authority import RepositoryChargeableAdmissionAuthority
from elspeth.web.coordination.mutation_connection_registry import (
    _register_mutation_connection,
    _resolve_mutation_connection,
    _unregister_mutation_connection,
)
from elspeth.web.coordination.quota_authority import RepositoryQuotaAuthority, TokenUsageEntry, begin_provider_attempt_on_connection
from elspeth.web.coordination.repository import PostgresSessionOperationRepository
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema

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
