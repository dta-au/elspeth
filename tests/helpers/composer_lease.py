"""Test adapter: give legacy compose() / _run_one_turn_for_test callers an exact COMPOSE lease.

Composer entry points require a session-operation context since the
multi-replica recovery. Tests written before that drive the loop for a named
session with no context; this adapter acquires a real, short-lived COMPOSE
lease on the composer's sessions service for the duration of the call (the
same authority the routes use) — no production bypass, no optional-context
arm. Register it as an autouse fixture in the conftest of any legacy suite.
"""

from __future__ import annotations

from unittest.mock import patch


def install_fenced_compose_adapter(monkeypatch) -> None:
    """Test adapter: compose() calls that name a session but carry no
    session-operation context acquire an exact, short-lived COMPOSE lease on
    the composer's sessions service for the duration of the call (mirrors
    FencedSessionServiceHarness for the session writers)."""
    from uuid import UUID, uuid4

    from elspeth.contracts.session_operation import SessionOperationKind
    from elspeth.web.composer.provider_quota import ProviderInvocationOwner
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.coordination.contracts import SessionOperationFenceLost
    from elspeth.web.coordination.lifecycle import SessionOperationLease
    from elspeth.web.required_work import (
        RequiredAuthorityKind,
        RequiredWorkAuthority,
        RequiredWorkBinding,
        RequiredWorkCoordinator,
        RequiredWorkRole,
    )

    async def _with_provider_owner(self, kwargs, call, lease=None):
        sessions = self._sessions_service
        context = kwargs.get("session_operation_context")
        if sessions is None or context is None or kwargs.get("required_work") is not None or kwargs.get("provider_owner") is not None:
            return await call(**kwargs)
        coordinator = RequiredWorkCoordinator(
            RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
        )
        if lease is not None:
            lease.bind_required_work(coordinator)
        binding = RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN)
        forwarded = {**kwargs, "required_work": binding, "provider_owner": ProviderInvocationOwner(service=sessions, required_work=binding)}
        body = None
        try:
            return await call(**forwarded)
        except BaseException as failure:
            body = failure
            raise
        finally:
            try:
                if lease is None:
                    coordinator.assert_completed()
                else:
                    # The lifecycle owns and joins a possibly active renewal;
                    # every body producer/physical write must already be done.
                    coordinator.settlement_failure_receipts()
            except BaseException as settlement:
                if body is not None and settlement is not body:
                    raise BaseExceptionGroup("Composer test body and required settlement outcomes", [body, settlement]) from None
                raise

    async def _with_lease(self, session_id, kwargs, call, *, bind_provider_owner=False):
        # ``self`` is always a ComposerServiceImpl (the adapter is installed on
        # that class) and its sessions service is a SessionServiceImpl, so the
        # authority and engine are read directly: a double lacking them fails
        # the calling test loudly instead of silently running unfenced.
        sessions = self._sessions_service
        if session_id is None or kwargs.get("session_operation_context") is not None or sessions is None:
            return await _with_provider_owner(self, kwargs, call) if bind_provider_owner else await call(**kwargs)
        authority = sessions.session_operation_authority
        session_uuid = session_id if not isinstance(session_id, str) else UUID(session_id)
        try:
            lease = await SessionOperationLease.acquire(
                authority,
                session_id=session_uuid,
                operation_kind=SessionOperationKind.COMPOSE,
                owner_instance_id=sessions.session_operation_owner_instance_id,
                lease_seconds=sessions.session_operation_lease_seconds,
            )
        except SessionOperationFenceLost:
            from tests.helpers.session_fences import ensure_session_fence

            seeded = ensure_session_fence(
                sessions._engine,
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
        body = None
        try:
            forwarded = {name: value for name, value in kwargs.items() if name != "session_operation_context"}
            forwarded["session_operation_context"] = lease.context
            return await _with_provider_owner(self, forwarded, call, lease) if bind_provider_owner else await call(**forwarded)
        except BaseException as failure:
            body = failure
            raise
        finally:
            try:
                await lease.close()
            except BaseException as closing:
                if body is not None and closing is not body:
                    raise BaseExceptionGroup("Composer test body and lease close outcomes", [body, closing]) from None
                raise

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
        user_message_id=None,
        session_operation_context=None,
        completion_gates=None,
        budget_seconds=None,
        *,
        required_work=None,
        provider_owner=None,
        recovery_context=None,
        retained_operation_context=None,
        advisor_history=None,
        mutation_ceiling=None,
    ):
        kwargs = {
            "message": message,
            "messages": messages,
            "state": state,
            "session_id": session_id,
            "current_state_id": current_state_id,
            "user_id": user_id,
            "progress": progress,
            "user_message_id": user_message_id,
            "session_operation_context": session_operation_context,
            "completion_gates": completion_gates,
            "budget_seconds": budget_seconds,
            "required_work": required_work,
            "provider_owner": provider_owner,
            "recovery_context": recovery_context,
            "retained_operation_context": retained_operation_context,
            "advisor_history": advisor_history,
            "mutation_ceiling": mutation_ceiling,
        }

        async def call(**kw):
            return await real_compose(self, **kw)

        return await _with_lease(self, session_id, kwargs, call, bind_provider_owner=True)

    async def run_one_turn(self, **kwargs):
        scripted_llm = kwargs.pop("llm", None)
        scripted_sdk = kwargs.pop("sdk", None)
        if scripted_llm is not None and scripted_sdk is not None:
            raise ValueError("Choose either the legacy llm script or the physical sdk script")

        async def call(**kw):
            if scripted_sdk is not None:
                # Raw SDK scripts retain the actual gateway admission,
                # physical custody, audit and settlement owners.
                from elspeth.web.composer.prompt_guard import prompt_guard_user_scope

                binding = kw.pop("required_work", None)
                provider = kw.pop("provider_owner", None)
                actor = kw.pop("user_id", None)
                if type(binding) is not RequiredWorkBinding or type(provider) is not ProviderInvocationOwner:
                    raise ValueError("Physical SDK scripts require actual required-work and provider owners")
                if provider.required_work is not binding or provider.service is not self._sessions_service:
                    raise ValueError("Physical SDK script owners must retain their exact binding and session service")
                actual_loop = self._compose_loop

                async def owned_loop(message, *args, **loop_kwargs):
                    assert loop_kwargs["session_id"] == kw["session_id"]
                    assert loop_kwargs["session_operation_context"] is kw["session_operation_context"]
                    binding.validate_context(loop_kwargs["session_operation_context"])
                    assert "required_work" not in loop_kwargs and "provider_owner" not in loop_kwargs and "user_id" not in loop_kwargs
                    with prompt_guard_user_scope(message):
                        return await actual_loop(
                            message, *args, **loop_kwargs, required_work=binding, provider_owner=provider, user_id=actor
                        )

                with patch("litellm.acompletion", new=scripted_sdk), patch.object(self, "_compose_loop", new=owned_loop):
                    return await real_turn(self, **kw)
            if scripted_llm is None:
                return await real_turn(self, **kw)
            with patch.object(self._provider_gateway, "_call_llm", new=scripted_llm):
                return await real_turn(self, **kw)

        return await _with_lease(self, kwargs.get("session_id"), kwargs, call, bind_provider_owner=scripted_sdk is not None)

    monkeypatch.setattr(ComposerServiceImpl, "compose", compose)
    monkeypatch.setattr(ComposerServiceImpl, "_run_one_turn_for_test", run_one_turn)
