"""Seed the retained session-operation fence for a directly inserted session row.

Production sessions are created through
``create_session_with_initial_fence`` which writes the ``sessions`` row and a
released epoch-1 ``create`` fence in one transaction. Tests that insert
``sessions_table`` rows by hand to shape a scenario leave that fence absent, so
every fenced writer reports ``SessionOperationFenceLost: missing``. This helper
writes the same released epoch-1 fence the lifecycle method writes; it does not
open a lease and does not bypass any production check.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Connection, insert

from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.sessions.models import session_operation_fences_table
from tests.unit.web.sessions.guided_test_authority import DualFencedSessionServiceHarness


def seed_session_operation_fence(
    conn: Connection,
    session_id: UUID | str,
    *,
    owner_instance_id: str,
    created_at: datetime | None = None,
) -> None:
    """Insert the released epoch-1 ``create`` fence for ``session_id``."""
    if type(owner_instance_id) is not str or not owner_instance_id:
        raise ValueError("owner_instance_id must be a nonblank string")
    stamped = created_at if created_at is not None else datetime.now(UTC)
    conn.execute(
        insert(session_operation_fences_table).values(
            session_id=str(session_id),
            operation_id=uuid4().hex,
            lease_token=uuid4().hex,
            operation_kind=SessionOperationKind.CREATE.value,
            owner_instance_id=owner_instance_id,
            operation_epoch=1,
            lease_expires_at=stamped,
            released_at=stamped,
        )
    )


def ensure_session_fence(engine, session_id: UUID | str, *, owner_instance_id: str) -> bool:
    """Seed the released epoch-1 fence iff the session row exists without one.

    Returns True when a fence was seeded (the caller may retry its acquire).
    Returns False when there is nothing this helper can honestly repair: the
    session row itself is absent, or a fence row already exists (the original
    failure then had a different cause and must propagate).
    """
    from sqlalchemy import select

    from elspeth.web.sessions.models import sessions_table

    sid = str(session_id)
    with engine.begin() as conn:
        session_row = conn.execute(select(sessions_table.c.id, sessions_table.c.created_at).where(sessions_table.c.id == sid)).one_or_none()
        if session_row is None:
            return False
        fence_row = conn.execute(
            select(session_operation_fences_table.c.session_id).where(session_operation_fences_table.c.session_id == sid)
        ).one_or_none()
        if fence_row is not None:
            return False
        seed_session_operation_fence(conn, sid, owner_instance_id=owner_instance_id)
        return True


@__import__("contextlib").asynccontextmanager
async def acquire_compose_context(sessions_service, session_id):
    """Acquire a real COMPOSE session-operation context for a direct-call test.

    Mints the context through the production authority (no bypass) and
    releases it afterwards; requires the session row and its retained fence
    to exist (use the fixtures or ``ensure_session_fence`` first).
    """
    from contextlib import suppress
    from uuid import UUID as _UUID

    from elspeth.web.coordination.contracts import SessionOperationFenceLost

    sid = session_id if not isinstance(session_id, str) else _UUID(session_id)
    context = await sessions_service._run_sync(
        lambda: sessions_service.session_operation_authority.acquire(
            session_id=sid,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=sessions_service.session_operation_owner_instance_id,
            lease_seconds=sessions_service.session_operation_lease_seconds,
        )
    )
    try:
        yield context
    finally:
        with suppress(SessionOperationFenceLost):
            await sessions_service._run_sync(sessions_service.session_operation_authority.release, context)


def _make_context(session_id, operation_kind: SessionOperationKind):
    """Build one exact synthetic context of ``operation_kind`` bound to ``session_id``.

    Nominal validators check exact type, kind, and session binding — all
    satisfied honestly here; anything that proves liveness against the
    database will (correctly) reject it.
    """
    from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence

    return SessionOperationContext(
        fence=SessionOperationFence(
            session_id=str(session_id),
            operation_id=str(uuid4()),
            lease_token=uuid4().hex,
            operation_epoch=2,
        ),
        operation_kind=operation_kind,
    )


def make_compose_context(session_id):
    """Build an exact synthetic COMPOSE context for pure-unit direct calls.

    For tests that monkeypatch the code the context would otherwise reach
    (no live authority to acquire from).
    """
    return _make_context(session_id, SessionOperationKind.COMPOSE)


def make_execute_context(session_id):
    """Build an exact synthetic EXECUTE context for pure-unit direct calls.

    For the context-taking execution seams (``update_run_status``,
    ``append_run_event``, the blob finalizers) when no lease lifetime is under
    test. Methods that take a *lease* (``execute``, ``_run_pipeline``) need
    :func:`execute_lease` or :func:`adopt_execute_lease` instead — their exact
    ``SessionOperationLease`` check cannot be satisfied by a bare context.
    """
    return _make_context(session_id, SessionOperationKind.EXECUTE)


def make_blob_read_context(session_id):
    """Build an exact synthetic BLOB_READ context for pure-unit direct calls.

    For ``validate`` / ``validate_state`` / ``compute_snapshot`` and the other
    read-side seams that take ``session_operation_context`` and are reached in
    production under a BLOB_READ lease acquired by the route.
    """
    return _make_context(session_id, SessionOperationKind.BLOB_READ)


class RecordingSessionOperationAuthority:
    """Honest in-memory authority surface for fakes in pure-unit tests.

    Mints exact ``SessionOperationContext`` objects and records every call so
    a test can assert what authority was requested. No production bypass: code
    under test still receives and threads exact contexts; anything verifying
    liveness against a real database does not belong on this fake.

    ``renew`` returns the identical context, so a real
    ``SessionOperationLease`` built over this authority stays alive across
    renewals exactly as it would over the SQLite authority. The two error knobs
    model authority loss as a resource: set ``compare_and_swap_error`` to make
    the next ``guard_external_effect`` fail, ``renew_error`` to make the next
    renewal record a loss. Both raise the exact exception assigned.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.active: list[object] = []
        self.compare_and_swap_error: BaseException | None = None
        self.renew_error: BaseException | None = None

    def acquire(self, *, session_id, operation_kind, owner_instance_id, lease_seconds):
        del owner_instance_id, lease_seconds
        context = _make_context(session_id, operation_kind)
        self.calls.append(("acquire", context))
        self.active.append(context)
        return context

    def renew(self, context, *, lease_seconds):
        del lease_seconds
        self.calls.append(("renew", context))
        if self.renew_error is not None:
            raise self.renew_error
        return context

    def compare_and_swap(self, context):
        self.calls.append(("compare_and_swap", context))
        if self.compare_and_swap_error is not None:
            raise self.compare_and_swap_error
        return context

    def release(self, context):
        self.calls.append(("release", context))
        if context in self.active:
            self.active.remove(context)

    def validate_fork_child_lease(self, *args, **kwargs):
        self.calls.append(("validate_fork_child_lease", (args, kwargs)))


_TEST_EXECUTE_OWNER_INSTANCE_ID = "test-execute-owner"


@__import__("contextlib").asynccontextmanager
async def execute_lease(
    session_id,
    *,
    authority: RecordingSessionOperationAuthority | None = None,
    lease_seconds: int = 30,
    renew_interval_seconds: float | None = None,
    owner_instance_id: str = _TEST_EXECUTE_OWNER_INSTANCE_ID,
):
    """Hold a real EXECUTE ``SessionOperationLease`` for one async test body.

    Acquires through ``SessionOperationLease.acquire`` — the same call the
    execution route makes — over ``authority`` (a fresh recording authority
    when omitted; pass your own to assert on its ``calls``). The lease is a
    genuine lifecycle object: its renewal task runs, ``guard_external_effect``
    reproves through the authority, and ``close`` releases on exit. Nothing
    the production guard checks is bypassed; a raising ``compare_and_swap``
    surfaces from the guard exactly as it does under the SQLite authority.
    """
    from uuid import UUID as _UUID

    from elspeth.web.coordination.lifecycle import SessionOperationLease

    if authority is None:
        authority = RecordingSessionOperationAuthority()
    sid = session_id if isinstance(session_id, _UUID) else _UUID(str(session_id))
    lease = await SessionOperationLease.acquire(
        authority,
        session_id=sid,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id=owner_instance_id,
        lease_seconds=lease_seconds,
        renew_interval_seconds=renew_interval_seconds,
    )
    async with lease:
        yield lease


def adopt_execute_lease(
    loop,
    session_id,
    authority: RecordingSessionOperationAuthority | None = None,
    *,
    lease_seconds: int = 30,
    renew_interval_seconds: float | None = None,
):
    """Adopt a real EXECUTE lease onto ``loop`` for a synchronous direct call.

    For tests that drive a sync internal (``_run_pipeline``,
    ``_finalize_output_blobs``, ``_on_pipeline_done``) from the test thread
    and own an event loop for the service's ``_call_async`` bridge. The lease
    is built by ``SessionOperationLease.adopt`` — its compare-and-swap runs
    through ``authority`` and its renewal task is bound to ``loop`` — so the
    sync guard calls the code under test makes reprove through the authority
    exactly as in production. Close it with :func:`close_adopted_lease` on the
    same loop; the loop must be open and not running when either is called.
    """
    from elspeth.web.coordination.lifecycle import SessionOperationLease

    if authority is None:
        authority = RecordingSessionOperationAuthority()
    return loop.run_until_complete(
        SessionOperationLease.adopt(
            authority,
            make_execute_context(session_id),
            lease_seconds=lease_seconds,
            renew_interval_seconds=renew_interval_seconds,
        )
    )


def close_adopted_lease(loop, lease) -> None:
    """Close a lease from :func:`adopt_execute_lease` on the loop it was adopted onto."""
    loop.run_until_complete(lease.close())


def seed_live_compose_context(engine, session_id, *, owner_instance_id: str = "test-live-owner", lease_seconds: int = 300):
    """Upsert one LIVE COMPOSE fence row for ``session_id`` and return its exact context.

    For sync direct calls on fenced writers whose session ids are not UUIDs
    (legacy fixture ids), where the production acquire path cannot be used.
    The DTO matches the durable row exactly, so writer-side database
    verification passes honestly; nothing in production is bypassed.
    """
    from datetime import timedelta

    from sqlalchemy import delete

    from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind

    sid = str(session_id)
    operation_id = str(uuid4())
    lease_token = uuid4().hex
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(delete(session_operation_fences_table).where(session_operation_fences_table.c.session_id == sid))
        conn.execute(
            insert(session_operation_fences_table).values(
                session_id=sid,
                operation_id=operation_id,
                lease_token=lease_token,
                operation_kind=SessionOperationKind.COMPOSE.value,
                owner_instance_id=owner_instance_id,
                operation_epoch=2,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                released_at=None,
            )
        )
    return SessionOperationContext(
        fence=SessionOperationFence(
            session_id=sid,
            operation_id=operation_id,
            lease_token=lease_token,
            operation_epoch=2,
        ),
        operation_kind=SessionOperationKind.COMPOSE,
    )


class FencedComposeTurnHarness(DualFencedSessionServiceHarness):
    """Supply the sync ``persist_compose_turn`` primitive its COMPOSE authority.

    The harness's other adapters are async and cannot cover this primitive:
    it refuses to run inside an event loop. A call that names no context
    receives the one LIVE COMPOSE context seeded for its session -- minted
    through ``seed_live_compose_context`` on first use and reused for that
    session's later turns -- so the writer's database authority check runs
    for real against a durable fence row. No optional-context arm reaches
    production and no signature is relaxed; an explicit context is forwarded
    untouched.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._live_compose_contexts: dict[str, SessionOperationContext] = {}

    def persist_compose_turn(self, **kwargs):
        if "session_operation_context" not in kwargs:
            sid = str(kwargs["session_id"])
            context = self._live_compose_contexts.get(sid)
            if context is None:
                context = seed_live_compose_context(self._engine, sid, owner_instance_id=self.session_operation_owner_instance_id)
                self._live_compose_contexts[sid] = context
            kwargs["session_operation_context"] = context
        return super().persist_compose_turn(**kwargs)
