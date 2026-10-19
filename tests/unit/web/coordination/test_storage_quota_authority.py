"""R13 storage admission measures live blobs across an identity's sessions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event, insert, select, update
from sqlalchemy.exc import OperationalError
from tests.helpers.fenced_session import FencedSession

from elspeth.contracts.blobs import IdentityStorageQuotaExceededError, StorageAccountingUnavailableError
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection
from elspeth.web.coordination.quota_authority import QuotaExceeded, RepositoryQuotaAuthority, _storage_aggregate_integer
from elspeth.web.sessions.models import blobs_table, quota_policies_table, sessions_table

SET_AT = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.mark.parametrize("value", [0, 2**70, Decimal("0"), Decimal("2.0"), Decimal(2**70)])
def test_storage_aggregate_admits_only_exact_integral_database_values(value: int | Decimal) -> None:
    result = _storage_aggregate_integer(value)
    assert type(result) is int
    assert result == value


@pytest.mark.parametrize("value", [True, False, 1.0, -1, Decimal("-1"), Decimal("1.1"), Decimal("NaN"), Decimal("Infinity")])
def test_storage_aggregate_rejects_corrupt_or_lossy_values(value: object) -> None:
    with pytest.raises(AuditIntegrityError, match="non-negative exact integers"):
        _storage_aggregate_integer(value)


def _seed(fenced: FencedSession, *, cap: int, ceiling: int) -> None:
    conn = _resolve_mutation_connection(fenced.connection_token)
    for policy_id, identity_id, bound in (("identity", fenced.identity_id, cap), ("container", None, ceiling)):
        conn.execute(
            insert(quota_policies_table).values(
                policy_id=policy_id,
                identity_id=identity_id,
                tokens_per_day=1000,
                storage_bytes=bound,
                set_by_actor="operator",
                set_by_identity_id=None,
                set_at=SET_AT,
            )
        )
    other_session = str(uuid.uuid4())
    conn.execute(
        insert(sessions_table).values(
            id=other_session,
            user_id=fenced.identity_id,
            auth_provider_type="local",
            title="archived",
            created_at=SET_AT,
            updated_at=SET_AT,
            archived_at=SET_AT,
        )
    )
    for session_id, size, status in ((fenced.session_id, 30, "ready"), (other_session, 60, "pending")):
        blob_id = str(uuid.uuid4())
        conn.execute(
            insert(blobs_table).values(
                id=blob_id,
                session_id=session_id,
                filename="data.csv",
                mime_type="text/csv",
                size_bytes=size,
                content_hash="a" * 64 if status == "ready" else None,
                storage_path=f"/nonexistent/{blob_id}.csv",
                created_at=SET_AT,
                created_by="user",
                source_description=None,
                status=status,
            )
        )


@pytest.mark.parametrize(("cap", "ceiling", "refused"), [(100, 1000, True), (1000, 100, True), (101, 1000, False)])
def test_storage_admission_uses_identity_and_container_policy_rows(
    fenced_session: FencedSession, cap: int, ceiling: int, refused: bool
) -> None:
    _seed(fenced_session, cap=cap, ceiling=ceiling)
    recorded: list[QuotaExceeded] = []

    def admit() -> None:
        RepositoryQuotaAuthority.admit_storage_bytes(
            fenced_session.connection_token,
            session_id=fenced_session.session_id,
            additional_bytes=11,
            operation="blob_create",
            record=recorded.append,
        )

    if refused:
        with pytest.raises(IdentityStorageQuotaExceededError) as caught:
            admit()
        assert (caught.value.usage, caught.value.cap, caught.value.ceiling) == (90, cap, ceiling)
        assert recorded == [
            QuotaExceeded(
                identity_id=fenced_session.identity_id,
                provider="local",
                operation="blob_create",
                dimension="storage",
                cap=cap,
                ceiling=ceiling,
                usage=90,
                identity_policy_id="identity",
                container_policy_id="container",
                scope="identity" if cap <= ceiling else "container",
                limit=min(cap, ceiling),
            )
        ]
    else:
        admit()
        assert recorded == []


def test_zero_net_growth_does_not_measure_or_refuse(fenced_session: FencedSession) -> None:
    _seed(fenced_session, cap=80, ceiling=80)
    recorded: list[QuotaExceeded] = []
    admitted = RepositoryQuotaAuthority.admit_storage_bytes(
        fenced_session.connection_token,
        session_id=fenced_session.session_id,
        additional_bytes=0,
        operation="blob_replacement",
        record=recorded.append,
    )
    assert admitted.usage is None
    assert recorded == []


def test_container_storage_aggregates_other_identities(fenced_session: FencedSession) -> None:
    """An unrelated owner's retained bytes consume the shared ceiling."""
    from tests.fixtures.identities import ensure_test_identity

    _seed(fenced_session, cap=1000, ceiling=100)
    conn = _resolve_mutation_connection(fenced_session.connection_token)
    ensure_test_identity(conn, identity_id="other-storage-owner")
    other_session = str(uuid.uuid4())
    conn.execute(
        insert(sessions_table).values(
            id=other_session,
            user_id="other-storage-owner",
            auth_provider_type="local",
            title="other",
            created_at=SET_AT,
            updated_at=SET_AT,
        )
    )
    conn.execute(
        insert(blobs_table).values(
            id=str(uuid.uuid4()),
            session_id=other_session,
            filename="other.csv",
            mime_type="text/csv",
            size_bytes=9,
            content_hash="b" * 64,
            storage_path="/nonexistent/other.csv",
            created_at=SET_AT,
            created_by="user",
            status="ready",
        )
    )
    recorded: list[QuotaExceeded] = []
    RepositoryQuotaAuthority.admit_storage_bytes(
        fenced_session.connection_token,
        session_id=fenced_session.session_id,
        additional_bytes=1,
        operation="blob_create",
        record=recorded.append,
    )
    with pytest.raises(IdentityStorageQuotaExceededError) as caught:
        RepositoryQuotaAuthority.admit_storage_bytes(
            fenced_session.connection_token,
            session_id=fenced_session.session_id,
            additional_bytes=2,
            operation="blob_create",
            record=recorded.append,
        )
    assert caught.value.usage == 99
    assert recorded[0].usage == 99
    assert conn.execute(select(blobs_table.c.size_bytes).where(blobs_table.c.session_id == other_session)).scalar_one() == 9


def test_revoked_identity_policy_suspends_storage_growth(fenced_session: FencedSession) -> None:
    _seed(fenced_session, cap=80, ceiling=80)
    conn = _resolve_mutation_connection(fenced_session.connection_token)
    conn.execute(
        update(quota_policies_table).where(quota_policies_table.c.identity_id == fenced_session.identity_id).values(revoked_at=SET_AT)
    )
    recorded: list[QuotaExceeded] = []
    with pytest.raises(StorageAccountingUnavailableError):
        RepositoryQuotaAuthority.admit_storage_bytes(
            fenced_session.connection_token,
            session_id=fenced_session.session_id,
            additional_bytes=11,
            operation="blob_create",
            record=recorded.append,
        )
    assert recorded == []


def test_never_issued_identity_policy_can_use_container_only_ceiling(fenced_session: FencedSession) -> None:
    conn = _resolve_mutation_connection(fenced_session.connection_token)
    conn.execute(
        insert(quota_policies_table).values(
            policy_id="container",
            identity_id=None,
            tokens_per_day=1000,
            storage_bytes=10,
            set_by_actor="operator",
            set_by_identity_id=None,
            set_at=SET_AT,
        )
    )
    recorded: list[QuotaExceeded] = []
    admitted = RepositoryQuotaAuthority.admit_storage_bytes(
        fenced_session.connection_token,
        session_id=fenced_session.session_id,
        additional_bytes=10,
        operation="blob_create",
        record=recorded.append,
    )
    assert (admitted.usage, admitted.cap, admitted.ceiling) == (0, None, 10)
    assert recorded == []


def test_empty_identity_storage_is_measured_zero(fenced_session: FencedSession) -> None:
    conn = _resolve_mutation_connection(fenced_session.connection_token)
    conn.execute(
        insert(quota_policies_table).values(
            policy_id="identity",
            identity_id=fenced_session.identity_id,
            tokens_per_day=1000,
            storage_bytes=10,
            set_by_actor="operator",
            set_by_identity_id=None,
            set_at=SET_AT,
        )
    )
    recorded: list[QuotaExceeded] = []
    admitted = RepositoryQuotaAuthority.admit_storage_bytes(
        fenced_session.connection_token,
        session_id=fenced_session.session_id,
        additional_bytes=10,
        operation="blob_create",
        record=recorded.append,
    )
    assert (admitted.usage, admitted.cap, admitted.ceiling) == (0, 10, None)
    assert recorded == []


@pytest.mark.parametrize("identity_state", ["absent", "active", "revoked"])
def test_token_only_container_does_not_invent_storage_bound(fenced_session: FencedSession, identity_state: str) -> None:
    conn = _resolve_mutation_connection(fenced_session.connection_token)
    conn.execute(
        insert(quota_policies_table).values(
            policy_id="token-only", identity_id=None, tokens_per_day=1000, storage_bytes=None, set_by_actor="config", set_at=SET_AT
        )
    )
    if identity_state != "absent":
        conn.execute(
            insert(quota_policies_table).values(
                policy_id="identity",
                identity_id=fenced_session.identity_id,
                tokens_per_day=1000,
                storage_bytes=10,
                set_by_actor="system",
                set_at=SET_AT,
                revoked_at=SET_AT if identity_state == "revoked" else None,
            )
        )
    recorded: list[QuotaExceeded] = []
    if identity_state == "revoked":
        with pytest.raises(StorageAccountingUnavailableError):
            RepositoryQuotaAuthority.admit_storage_bytes(
                fenced_session.connection_token,
                session_id=fenced_session.session_id,
                additional_bytes=11,
                operation="blob_create",
                record=recorded.append,
            )
    elif identity_state == "active":
        with pytest.raises(IdentityStorageQuotaExceededError) as caught:
            RepositoryQuotaAuthority.admit_storage_bytes(
                fenced_session.connection_token,
                session_id=fenced_session.session_id,
                additional_bytes=11,
                operation="blob_create",
                record=recorded.append,
            )
        assert (caught.value.cap, caught.value.ceiling) == (10, None)
    else:
        admitted = RepositoryQuotaAuthority.admit_storage_bytes(
            fenced_session.connection_token,
            session_id=fenced_session.session_id,
            additional_bytes=11,
            operation="blob_create",
            record=recorded.append,
        )
        assert (admitted.cap, admitted.ceiling, admitted.usage) == (None, None, None)


def test_accounting_sql_failure_refuses_without_claiming_measured_usage(fenced_session: FencedSession) -> None:
    recorded: list[QuotaExceeded] = []

    def fail_policy_read(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        if "quota_policies" in statement:
            raise OperationalError(statement, {}, RuntimeError("quota read unavailable"))

    event.listen(fenced_session.engine, "before_cursor_execute", fail_policy_read)
    try:
        with pytest.raises(StorageAccountingUnavailableError):
            RepositoryQuotaAuthority.admit_storage_bytes(
                fenced_session.connection_token,
                session_id=fenced_session.session_id,
                additional_bytes=1,
                operation="blob_create",
                record=recorded.append,
            )
    finally:
        event.remove(fenced_session.engine, "before_cursor_execute", fail_policy_read)
    assert recorded == []
