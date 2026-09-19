"""R13 storage admission measures live blobs across an identity's sessions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, insert, update
from sqlalchemy.exc import OperationalError
from tests.helpers.fenced_session import FencedSession

from elspeth.contracts.blobs import IdentityStorageQuotaExceededError, StorageAccountingUnavailableError
from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection
from elspeth.web.coordination.quota_authority import QuotaExceeded, RepositoryQuotaAuthority
from elspeth.web.sessions.models import blobs_table, quota_policies_table, sessions_table

SET_AT = datetime(2026, 9, 1, tzinfo=UTC)


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
