"""Test-only adapter for legacy guided-row tests after dual fencing became mandatory.

New dual-fence tests pass contexts explicitly.  Older tests in this package
exercise the guided row/state semantics and use this harness to keep a real,
current session-operation context paired with every production service call.
"""

from __future__ import annotations

import contextlib
import weakref
from typing import Any, cast
from uuid import UUID

from elspeth.web.coordination.contracts import SessionOperationContext, SessionOperationFenceLost, SessionOperationKind
from elspeth.web.sessions.protocol import GuidedOperationActive
from elspeth.web.sessions.service import SessionServiceImpl

_UNSET = object()


class DualFencedSessionServiceHarness(SessionServiceImpl):
    _contexts_by_engine: weakref.WeakKeyDictionary[Any, dict[UUID, SessionOperationContext]] = weakref.WeakKeyDictionary()

    async def _guided_test_context(self, session_id: UUID, kind: str) -> SessionOperationContext:
        contexts = self._contexts_by_engine.setdefault(self._engine, {})
        existing = contexts.get(session_id)
        if existing is not None:
            try:
                await self._run_sync(self.session_operation_authority.compare_and_swap, existing)
            except SessionOperationFenceLost:
                contexts.pop(session_id, None)
            else:
                return existing
        operation_kind = SessionOperationKind.SESSION_FORK if kind == "session_fork" else SessionOperationKind.COMPOSE
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

    def _remember_guided_test_context(self, session_id: UUID, context: SessionOperationContext) -> None:
        self._contexts_by_engine.setdefault(self._engine, {})[session_id] = context

    async def reserve_guided_operation(self, *, session_operation_context=None, **kwargs):
        context = session_operation_context or await self._guided_test_context(kwargs["session_id"], kwargs["kind"])
        self._remember_guided_test_context(kwargs["session_id"], context)
        return await super().reserve_guided_operation(session_operation_context=context, **kwargs)

    async def reconcile_guided_start_operation(
        self,
        *,
        session_operation_context=None,
        observed_attempt=_UNSET,
        lease_seconds=300,
        **kwargs,
    ):
        if observed_attempt is _UNSET:
            observed = await super().get_guided_start_reconciliation(
                session_id=kwargs["session_id"],
                operation_id=kwargs["operation_id"],
            )
            if observed is None:
                observed_attempt = None
            elif isinstance(observed, GuidedOperationActive) and observed.expired:
                observed_attempt = observed.attempt
            else:
                # An unexpired active attempt, or a terminal Completed/Failed
                # descriptor (neither carries ``expired``/``attempt``): replay it.
                return observed
        context = session_operation_context or await self._guided_test_context(kwargs["session_id"], "guided_start")
        return await super().reconcile_guided_start_operation(
            session_operation_context=context,
            observed_attempt=observed_attempt,
            lease_seconds=lease_seconds,
            **kwargs,
        )

    async def _context_for_fence(self, fence, context):
        if context is not None:
            return context
        existing = self._contexts_by_engine.setdefault(self._engine, {}).get(fence.session_id)
        return existing or await self._guided_test_context(fence.session_id, "guided")

    async def renew_guided_operation(self, fence, *, session_operation_context=None, **kwargs):
        context = await self._context_for_fence(fence, session_operation_context)
        return await super().renew_guided_operation(fence, session_operation_context=context, **kwargs)

    async def bind_guided_operation(self, fence, *, session_operation_context=None, **kwargs):
        context = await self._context_for_fence(fence, session_operation_context)
        return await super().bind_guided_operation(fence, session_operation_context=context, **kwargs)

    async def complete_guided_operation(self, fence, *, session_operation_context=None, **kwargs):
        context = await self._context_for_fence(fence, session_operation_context)
        return await super().complete_guided_operation(fence, session_operation_context=context, **kwargs)

    async def fail_guided_operation(self, fence, *, session_operation_context=None, **kwargs):
        context = await self._context_for_fence(fence, session_operation_context)
        return await super().fail_guided_operation(fence, session_operation_context=context, **kwargs)

    async def fail_guided_operation_with_audit(self, command, *, session_operation_context=None):
        context = await self._context_for_fence(command.fence, session_operation_context)
        return await super().fail_guided_operation_with_audit(command, session_operation_context=context)

    async def revert_state_for_guided_operation(self, fence, *, session_operation_context=None, **kwargs):
        context = await self._context_for_fence(fence, session_operation_context)
        return await super().revert_state_for_guided_operation(fence, session_operation_context=context, **kwargs)

    async def save_state_for_guided_operation(self, fence, *, session_operation_context=None, **kwargs):
        context = await self._context_for_fence(fence, session_operation_context)
        return await super().save_state_for_guided_operation(fence, session_operation_context=context, **kwargs)

    async def _command_context(self, command, context):
        return context or await self._guided_test_context(command.fence.session_id, "guided")

    async def settle_guided_state_operation(self, command, *, session_operation_context=None, **kwargs):
        context = await self._command_context(command, session_operation_context)
        return await super().settle_guided_state_operation(command, session_operation_context=context, **kwargs)

    # ------------------------------------------------------------------
    # Per-call authority for the ordinary session writers. Legacy tests
    # call these without a context; the harness holds an exact, short-lived
    # lease of the required kind for the duration of the one call and
    # releases it, reusing a live guided-test context when one already
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
            # Legacy tests insert sessions_table rows directly, which never
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

    # P4-D6 family A2b: the message/title/resolve writers require an exact
    # operation; legacy tests that call them bare get a COMPOSE context here.
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

    async def stage_guided_pipeline_proposal(self, command, *, session_operation_context=None, **kwargs):
        context = await self._command_context(command, session_operation_context)
        return await super().stage_guided_pipeline_proposal(command, session_operation_context=context, **kwargs)

    async def stage_guided_full_pipeline_proposal(self, command, *, session_operation_context=None):
        context = await self._command_context(command, session_operation_context)
        return await super().stage_guided_full_pipeline_proposal(command, session_operation_context=context)

    async def accept_guided_pipeline_proposal(self, command, *, session_operation_context=None, **kwargs):
        context = await self._command_context(command, session_operation_context)
        return await super().accept_guided_pipeline_proposal(command, session_operation_context=context, **kwargs)

    async def admit_guided_pipeline_confirmation(self, command, *, session_operation_context=None):
        context = await self._command_context(command, session_operation_context)
        return await super().admit_guided_pipeline_confirmation(command, session_operation_context=context)

    async def record_guided_pipeline_dispatch(self, command, *, session_operation_context=None):
        context = await self._command_context(command, session_operation_context)
        return await super().record_guided_pipeline_dispatch(command, session_operation_context=context)

    async def back_edit_guided_pipeline_proposal(self, command, *, session_operation_context=None, **kwargs):
        context = await self._command_context(command, session_operation_context)
        return await super().back_edit_guided_pipeline_proposal(command, session_operation_context=context, **kwargs)

    async def reject_guided_pipeline_proposal(self, command, *, session_operation_context=None):
        context = await self._command_context(command, session_operation_context)
        return await super().reject_guided_pipeline_proposal(command, session_operation_context=context)

    async def seed_or_complete_guided_start_operation(self, fence, *, session_operation_context=None, **kwargs):
        owns_context = session_operation_context is None
        context = await self._context_for_fence(fence, session_operation_context)
        outcome = await super().seed_or_complete_guided_start_operation(
            fence,
            session_operation_context=context,
            **kwargs,
        )
        if owns_context:
            # Concurrent legacy tests can share one cached harness context;
            # the first terminal seed owns its exact release.
            with contextlib.suppress(SessionOperationFenceLost):
                await self._run_sync(self.session_operation_authority.release, context)
            contexts = self._contexts_by_engine.setdefault(self._engine, {})
            if contexts.get(fence.session_id) == context:
                contexts.pop(fence.session_id, None)
        return outcome

    async def complete_existing_state_guided_operation(self, fence, *, session_operation_context=None, **kwargs):
        context = await self._context_for_fence(fence, session_operation_context)
        return await super().complete_existing_state_guided_operation(fence, session_operation_context=context, **kwargs)
