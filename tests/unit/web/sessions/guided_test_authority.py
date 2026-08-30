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
            if observed is not None and not getattr(observed, "expired", False):
                return observed
            observed_attempt = getattr(observed, "attempt", None)
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
