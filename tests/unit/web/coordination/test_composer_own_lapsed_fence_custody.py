"""An own lapsed settlement touches only this instance's exact, lapsed COMPOSE fence.

settle_own_lapsed settles a running job whose stored fence (operation, token,
epoch) is the session's current fence, unreleased and past its deadline, and
with the reaper's flag releases that fence in the same transaction. The fence
must be this instance's COMPOSE fence: an exact fence of another kind or owner
is corrupt stored state and raises AuditIntegrityError before any write. A
successor fence is never touched, and the release flag belongs to this arm only.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select, update
from tests.fixtures.identities import ensure_test_identity

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.contracts import SessionOperationContext
from elspeth.web.coordination.rate_limit_authority import ComposerQuotaAdmission, RepositoryRateLimitAuthority
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.sessions.composer_operation_errors import worker_lost_error
from elspeth.web.sessions.composer_operations import ComposerOperationFenceLost, composer_operation_request_hash
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import session_operation_fences_table as fences
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.schemas import SendMessageRequest

_OWNER = "test-owner"
_PAST = datetime(2000, 1, 1, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[tuple[Engine, SQLiteLocalSessionOperationAuthority, ComposerAsyncOperationAuthority, UUID]]:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'own-lapsed.db'}")
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    repository = SQLiteLocalSessionOperationAuthority(engine)
    session = repository.create_session_with_initial_fence(
        user_id="alice", title="Own lapsed", auth_provider_type="local", owner_instance_id=_OWNER, lease_seconds=30
    )
    authority = ComposerAsyncOperationAuthority(engine, owner_instance_id=_OWNER, claim_lease_seconds=30)
    try:
        yield engine, repository, authority, session.id
    finally:
        engine.dispose()


def _start_lapsed(
    store: tuple[Engine, SQLiteLocalSessionOperationAuthority, ComposerAsyncOperationAuthority, UUID],
) -> tuple[str, SessionOperationContext]:
    """A running job started under this instance's COMPOSE fence, which has then lapsed."""
    engine, repository, authority, sid = store
    request = SendMessageRequest(operation_id=str(uuid4()), content="Build")
    authority.admit(
        quota=ComposerQuotaAdmission(RepositoryRateLimitAuthority(engine, signing_key=b"operation-test-quota-key-32-bytes"), 10000),
        session_id=sid,
        operation_id=request.operation_id,
        kind="compose_message",
        request_hash=composer_operation_request_hash(session_id=sid, kind="compose_message", request=request),
        actor_user_id="alice",
        request_id="correlation",
        base_state_id=None,
        request_json=request.model_dump_json(),
        deadline_seconds=180.0,
        max_nonterminal=64,
        auth_provider_type="local",
    )
    (claim,) = authority.claim_next(limit=1)
    context = repository.start_composer_async_operation(claim, owner_instance_id=_OWNER, lease_seconds=30, auth_provider_type="local")
    _set_fence(engine, context, lease_expires_at=_PAST)
    return request.operation_id, context


def _set_fence(engine: Engine, context: SessionOperationContext, **values: Any) -> None:
    with engine.begin() as conn:
        updated = conn.execute(
            update(fences)
            .where(fences.c.session_id == context.fence.session_id, fences.c.operation_id == context.fence.operation_id)
            .values(**values)
        )
        assert updated.rowcount == 1


def _fence_row(engine: Engine, sid: UUID) -> dict[str, Any]:
    with engine.connect() as conn:
        return dict(conn.execute(select(fences).where(fences.c.session_id == str(sid))).one()._mapping)


def _settle(authority: ComposerAsyncOperationAuthority, sid: UUID, operation_id: str, *, release: Any = True) -> Any:
    return authority.settle_own_lapsed(
        session_id=sid,
        operation_id=operation_id,
        owner_instance_id=_OWNER,
        failure=worker_lost_error(request_id=None),
        release_lapsed_fence=release,
    )


def test_the_exact_lapsed_compose_fence_is_settled_and_released(store) -> None:
    engine, _repository, authority, sid = store
    operation_id, _context = _start_lapsed(store)

    settled = _settle(authority, sid, operation_id)

    assert (settled.status, settled.settled_by) == ("failed", "settle_own_lapsed")
    with engine.connect() as conn:
        assert conn.execute(select(fences.c.released_at).where(fences.c.session_id == str(sid))).scalar_one() is not None


@pytest.mark.parametrize("release", [True, False], ids=["reaper", "flagless"])
@pytest.mark.parametrize(
    "corruption",
    [{"operation_kind": SessionOperationKind.PROPOSAL.value}, {"owner_instance_id": "another-owner"}],
    ids=["kind", "owner"],
)
def test_an_exact_fence_of_another_kind_or_owner_is_an_integrity_failure_before_any_write(
    store, corruption: dict[str, str], release: bool
) -> None:
    engine, _repository, authority, sid = store
    operation_id, context = _start_lapsed(store)
    _set_fence(engine, context, **corruption)
    before = _fence_row(engine, sid)

    with pytest.raises(AuditIntegrityError):
        _settle(authority, sid, operation_id, release=release)

    record = authority.get(session_id=sid, operation_id=operation_id)
    assert record is not None and record.status == "running"
    assert _fence_row(engine, sid) == before


def test_a_successor_that_took_over_without_settling_is_never_touched(store) -> None:
    engine, repository, authority, sid = store
    operation_id, _context = _start_lapsed(store)
    # The expired fence is taken over by another instance, which has not settled the job yet.
    successor = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="successor-owner", lease_seconds=30
    )
    before = _fence_row(engine, sid)

    with pytest.raises(ComposerOperationFenceLost):
        _settle(authority, sid, operation_id)

    record = authority.get(session_id=sid, operation_id=operation_id)
    assert record is not None and record.status == "running"
    assert _fence_row(engine, sid) == before
    assert before["operation_id"] == successor.fence.operation_id and before["owner_instance_id"] == "successor-owner"


def test_the_release_flag_is_an_exact_bool(store) -> None:
    _engine, _repository, authority, sid = store
    operation_id, _context = _start_lapsed(store)

    with pytest.raises(TypeError):
        _settle(authority, sid, operation_id, release=1)

    record = authority.get(session_id=sid, operation_id=operation_id)
    assert record is not None and record.status == "running"


@pytest.mark.parametrize("settled_by", ["settle_lost", "settle_lost_inactive_session"])
def test_no_other_settlement_arm_may_release_a_fence(store, settled_by: str) -> None:
    engine, _repository, authority, sid = store
    operation_id, _context = _start_lapsed(store)
    before = _fence_row(engine, sid)

    with pytest.raises(AuditIntegrityError):
        authority._settle_recovery(
            session_id=sid,
            operation_id=operation_id,
            failure=worker_lost_error(request_id=None),
            settled_by=settled_by,
            recovery_context=None,
            authoritative_failure=False,
            release_lapsed_fence=True,
        )

    record = authority.get(session_id=sid, operation_id=operation_id)
    assert record is not None and record.status == "running"
    assert _fence_row(engine, sid) == before
