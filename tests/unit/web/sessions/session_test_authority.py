"""Test-only session service harness with real session-operation authority.

Ordinary writers receive a short-lived operation context when a test omits one.
Receipt helpers retain a matching context across claim and settlement.
"""

from __future__ import annotations

import contextlib
import weakref
from typing import Any, cast
from uuid import UUID

from elspeth.web.coordination.contracts import SessionOperationContext, SessionOperationFenceLost, SessionOperationKind
from elspeth.web.sessions.protocol import OperationReceiptClaimed, OperationReceiptFence, OperationReceiptKind, OperationReceiptTakenOver
from elspeth.web.sessions.service import SessionServiceImpl


class FencedSessionServiceHarness(SessionServiceImpl):
    _contexts_by_engine: weakref.WeakKeyDictionary[Any, dict[UUID, SessionOperationContext]] = weakref.WeakKeyDictionary()

    async def _receipt_context(self, session_id: UUID, kind: OperationReceiptKind) -> SessionOperationContext:
        if kind == "session_fork":
            operation_kind = SessionOperationKind.SESSION_FORK
        elif kind == "state_revert":
            operation_kind = SessionOperationKind.COMPOSE
        else:
            raise ValueError("Unsupported receipt kind")
        contexts = self._contexts_by_engine.setdefault(self._engine, {})
        existing = contexts.get(session_id)
        if existing is not None:
            try:
                await self._run_sync(self.session_operation_authority.compare_and_swap, existing)
            except SessionOperationFenceLost:
                contexts.pop(session_id, None)
            else:
                if existing.operation_kind is operation_kind:
                    return existing
                raise AssertionError("Test receipt context has another active operation kind")
        context = cast(
            SessionOperationContext,
            await self._run_sync(
                lambda: self.session_operation_authority.acquire(
                    session_id=session_id,
                    operation_kind=operation_kind,
                    owner_instance_id=self.session_operation_owner_instance_id,
                    lease_seconds=self.session_operation_lease_seconds,
                )
            ),
        )
        contexts[session_id] = context
        return context

    def _context_for_receipt(self, fence: OperationReceiptFence, context: SessionOperationContext | None) -> SessionOperationContext:
        if context is not None:
            return context
        cached = self._contexts_by_engine.setdefault(self._engine, {}).get(fence.session_id)
        if cached is None:
            raise AssertionError("Receipt helper requires a claimed harness context or an explicit session operation context")
        return cached

    async def _release_receipt_context(self, session_id: UUID, context: SessionOperationContext) -> None:
        contexts = self._contexts_by_engine.setdefault(self._engine, {})
        if contexts.get(session_id) != context:
            return
        contexts.pop(session_id, None)
        with contextlib.suppress(SessionOperationFenceLost):
            await self._run_sync(self.session_operation_authority.release, context)

    async def reserve_operation_receipt(self, *, session_operation_context=None, **kwargs):
        context = session_operation_context or await self._receipt_context(kwargs["session_id"], kwargs["kind"])
        try:
            outcome = await super().reserve_operation_receipt(session_operation_context=context, **kwargs)
        except BaseException:
            if session_operation_context is None:
                await self._release_receipt_context(kwargs["session_id"], context)
            raise
        if session_operation_context is None and type(outcome) not in (OperationReceiptClaimed, OperationReceiptTakenOver):
            await self._release_receipt_context(kwargs["session_id"], context)
        return outcome

    async def renew_operation_receipt(self, fence, *, session_operation_context=None, **kwargs):
        context = self._context_for_receipt(fence, session_operation_context)
        return await super().renew_operation_receipt(fence, session_operation_context=context, **kwargs)

    async def bind_operation_receipt(self, fence, *, session_operation_context=None, **kwargs):
        context = self._context_for_receipt(fence, session_operation_context)
        return await super().bind_operation_receipt(fence, session_operation_context=context, **kwargs)

    async def complete_operation_receipt(self, fence, *, session_operation_context=None, **kwargs):
        context = self._context_for_receipt(fence, session_operation_context)
        outcome = await super().complete_operation_receipt(fence, session_operation_context=context, **kwargs)
        if session_operation_context is None:
            await self._release_receipt_context(fence.session_id, context)
        return outcome

    async def fail_operation_receipt(self, fence, *, session_operation_context=None, **kwargs):
        context = self._context_for_receipt(fence, session_operation_context)
        outcome = await super().fail_operation_receipt(fence, session_operation_context=context, **kwargs)
        if session_operation_context is None:
            await self._release_receipt_context(fence.session_id, context)
        return outcome

    # ------------------------------------------------------------------
    # Per-call authority for ordinary session writers. Tests
    # call these without a context; the harness holds an exact, short-lived
    # lease of the required kind for the duration of the one call and
    # releases it, reusing a live receipt context when one already
    # covers the session so the two never conflict.
    # ------------------------------------------------------------------
    @contextlib.asynccontextmanager
    async def _call_context(self, session_id: UUID, kind: SessionOperationKind):
        if isinstance(session_id, str):
            session_id = UUID(session_id)
        cached = self._contexts_by_engine.setdefault(self._engine, {}).get(session_id)
        if cached is not None:
            try:
                await self._run_sync(self.session_operation_authority.compare_and_swap, cached)
            except SessionOperationFenceLost:
                self._contexts_by_engine[self._engine].pop(session_id, None)
            else:
                if cached.operation_kind is kind:
                    yield cached
                    return

        def _acquire() -> SessionOperationContext:
            return self.session_operation_authority.acquire(
                session_id=session_id,
                operation_kind=kind,
                owner_instance_id=self.session_operation_owner_instance_id,
                lease_seconds=self.session_operation_lease_seconds,
            )

        try:
            context = cast(SessionOperationContext, await self._run_sync(_acquire))
        except SessionOperationFenceLost:
            # Some tests insert sessions_table rows directly, which never
            # mints the retained epoch-1 fence the production lifecycle
            # creates. Seed exactly that fence and retry once; any other
            # cause propagates.
            from tests.helpers.session_fences import ensure_session_fence

            seeded = await self._run_sync(
                ensure_session_fence,
                self._engine,
                session_id,
                owner_instance_id=self.session_operation_owner_instance_id,
            )
            if not seeded:
                raise
            context = cast(SessionOperationContext, await self._run_sync(_acquire))
        try:
            yield context
        finally:
            with contextlib.suppress(SessionOperationFenceLost):
                await self._run_sync(self.session_operation_authority.release, context)

    async def save_composition_state(self, session_id, state, *, session_operation_context=None, **kwargs):
        if session_operation_context is not None:
            return await super().save_composition_state(session_id, state, session_operation_context=session_operation_context, **kwargs)
        async with self._call_context(session_id, SessionOperationKind.COMPOSE) as context:
            return await super().save_composition_state(session_id, state, session_operation_context=context, **kwargs)

    async def update_session_title(self, session_id, title, *, session_operation_context=None, **kwargs):
        if session_operation_context is not None:
            return await super().update_session_title(session_id, title, session_operation_context=session_operation_context, **kwargs)
        async with self._call_context(session_id, SessionOperationKind.COMPOSE) as context:
            return await super().update_session_title(session_id, title, session_operation_context=context, **kwargs)

    async def add_message_with_transcript(self, session_id, *args, session_operation_context=None, **kwargs):
        if session_operation_context is not None:
            return await super().add_message_with_transcript(
                session_id, *args, session_operation_context=session_operation_context, **kwargs
            )
        async with self._call_context(session_id, SessionOperationKind.COMPOSE) as context:
            return await super().add_message_with_transcript(session_id, *args, session_operation_context=context, **kwargs)

    def _kw_writer(name: str, kind: SessionOperationKind):  # type: ignore[misc]
        async def _wrapped(self, *args, session_operation_context=None, **kwargs):
            parent = getattr(super(), name)
            if session_operation_context is not None:
                return await parent(*args, session_operation_context=session_operation_context, **kwargs)
            session_id = args[0] if args else kwargs["session_id"]
            async with self._call_context(session_id, kind) as context:
                return await parent(*args, session_operation_context=context, **kwargs)

        _wrapped.__name__ = name
        return _wrapped

    # Message/title/resolve writers require an exact operation; tests that
    # call them bare get a COMPOSE context here.
    add_message = _kw_writer("add_message", SessionOperationKind.COMPOSE)
    add_messages_atomic = _kw_writer("add_messages_atomic", SessionOperationKind.COMPOSE)
    resolve_interpretation_event = _kw_writer("resolve_interpretation_event", SessionOperationKind.COMPOSE)
    create_pending_interpretation_event = _kw_writer("create_pending_interpretation_event", SessionOperationKind.COMPOSE)
    commit_transition_response = _kw_writer("commit_transition_response", SessionOperationKind.COMPOSE)
    create_composition_proposal = _kw_writer("create_composition_proposal", SessionOperationKind.COMPOSE)
    create_pipeline_composition_proposal = _kw_writer("create_pipeline_composition_proposal", SessionOperationKind.COMPOSE)
    record_auto_interpreted_no_surfaces_event = _kw_writer("record_auto_interpreted_no_surfaces_event", SessionOperationKind.COMPOSE)
    reject_composition_proposal = _kw_writer("reject_composition_proposal", SessionOperationKind.PROPOSAL)
    reject_pipeline_composition_proposal = _kw_writer("reject_pipeline_composition_proposal", SessionOperationKind.PROPOSAL)
    accept_composition_proposal = _kw_writer("accept_composition_proposal", SessionOperationKind.PROPOSAL)
    settle_pipeline_composition_proposal = _kw_writer("settle_pipeline_composition_proposal", SessionOperationKind.PROPOSAL)
    create_run = _kw_writer("create_run", SessionOperationKind.EXECUTE)

    def _run_writer(name: str):  # type: ignore[misc]
        async def _wrapped(self, *args, session_operation_context=None, **kwargs):
            parent = getattr(super(), name)
            if session_operation_context is not None:
                return await parent(*args, session_operation_context=session_operation_context, **kwargs)
            run_id = args[0] if args else kwargs["run_id"]
            run = await self.get_run(run_id)
            async with self._call_context(run.session_id, SessionOperationKind.EXECUTE) as context:
                return await parent(*args, session_operation_context=context, **kwargs)

        _wrapped.__name__ = name
        return _wrapped

    update_run_status = _run_writer("update_run_status")
    append_run_event = _run_writer("append_run_event")
    record_blob_inline_resolutions = _run_writer("record_blob_inline_resolutions")
    del _run_writer
    del _kw_writer
