"""Test adapter: give legacy compose() / _run_one_turn_for_test callers an exact COMPOSE lease.

Composer entry points require a session-operation context since the
multi-replica recovery. Tests written before that drive the loop for a named
session with no context; this adapter acquires a real, short-lived COMPOSE
lease on the composer's sessions service for the duration of the call (the
same authority the routes use) — no production bypass, no optional-context
arm. Register it as an autouse fixture in the conftest of any legacy suite.
"""

from __future__ import annotations


def install_fenced_compose_adapter(monkeypatch) -> None:
    """Test adapter: compose() calls that name a session but carry no
    session-operation context acquire an exact, short-lived COMPOSE lease on
    the composer's sessions service for the duration of the call (mirrors
    DualFencedSessionServiceHarness for the session writers)."""
    from elspeth.contracts.session_operation import SessionOperationKind
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.coordination.contracts import SessionOperationFenceLost
    from elspeth.web.coordination.lifecycle import SessionOperationLease

    async def _with_lease(self, session_id, kwargs, call):
        sessions = getattr(self, "_sessions_service", None)
        authority = getattr(sessions, "session_operation_authority", None)
        if session_id is None or kwargs.get("session_operation_context") is not None or authority is None:
            return await call(**kwargs)
        try:
            lease = await SessionOperationLease.acquire(
                authority,
                session_id=session_id if not isinstance(session_id, str) else __import__("uuid").UUID(session_id),
                operation_kind=SessionOperationKind.COMPOSE,
                owner_instance_id=sessions.session_operation_owner_instance_id,
                lease_seconds=sessions.session_operation_lease_seconds,
            )
        except SessionOperationFenceLost:
            from tests.helpers.session_fences import ensure_session_fence

            session_uuid = session_id if not isinstance(session_id, str) else __import__("uuid").UUID(session_id)
            engine = getattr(sessions, "_engine", None)
            seeded = engine is not None and ensure_session_fence(
                engine,
                session_uuid,
                owner_instance_id=sessions.session_operation_owner_instance_id,
            )
            if not seeded:
                # No durable session row to fence (fixture-minted ids): run
                # the call exactly as the legacy test wrote it.
                return await call(**kwargs)
            lease = await SessionOperationLease.acquire(
                authority,
                session_id=session_uuid,
                operation_kind=SessionOperationKind.COMPOSE,
                owner_instance_id=sessions.session_operation_owner_instance_id,
                lease_seconds=sessions.session_operation_lease_seconds,
            )
        try:
            forwarded = {name: value for name, value in kwargs.items() if name != "session_operation_context"}
            return await call(session_operation_context=lease.context, **forwarded)
        finally:
            await lease.close()

    real_compose = ComposerServiceImpl.compose
    real_turn = ComposerServiceImpl._run_one_turn_for_test

    async def compose(
        self,
        message,
        messages,
        state,
        session_id=None,
        current_state_id=None,
        user_id=None,
        progress=None,
        guided_terminal=None,
        user_message_id=None,
        session_operation_context=None,
    ):
        kwargs = {
            "message": message,
            "messages": messages,
            "state": state,
            "session_id": session_id,
            "current_state_id": current_state_id,
            "user_id": user_id,
            "progress": progress,
            "guided_terminal": guided_terminal,
            "user_message_id": user_message_id,
            "session_operation_context": session_operation_context,
        }

        async def call(**kw):
            return await real_compose(self, **kw)

        return await _with_lease(self, session_id, kwargs, call)

    async def run_one_turn(self, **kwargs):
        async def call(**kw):
            return await real_turn(self, **kw)

        return await _with_lease(self, kwargs.get("session_id"), kwargs, call)

    monkeypatch.setattr(ComposerServiceImpl, "compose", compose)
    monkeypatch.setattr(ComposerServiceImpl, "_run_one_turn_for_test", run_one_turn)
