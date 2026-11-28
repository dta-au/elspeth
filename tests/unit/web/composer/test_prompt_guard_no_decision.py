"""A prompt guard check requires a decision from its calls."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from types import TracebackType
from typing import Any
from uuid import uuid4

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import PromptGuardCall, PromptGuardCallResult, PromptGuardDecision, PromptGuardRequest
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer.prompt_guard import PromptGuardMount, PromptGuardRefused, inspect_prompt_guard_request
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)
from tests.helpers.prompt_guard_clients import SingleCallPromptGuardClient
from tests.unit.web.composer.test_prompt_guard_reuse import _check, _event, _last_check
from tests.unit.web.sessions.test_token_usage_adapters import _quota_service


@pytest.fixture
def guard_sessions(tmp_path: Any) -> Iterator[tuple[Any, Any]]:
    engine, authority, service = _quota_service(tmp_path)
    contexts = []

    def open_owner() -> ProviderInvocationOwner:
        session = authority.create_session_with_initial_fence(
            user_id="alice", title="Guard check", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
        )
        context = authority.acquire(
            session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
        )
        contexts.append(context)
        coordinator = RequiredWorkCoordinator(
            RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
        )
        return ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))

    yield engine, open_owner
    for context in contexts:
        authority.release(context)
    engine.dispose()


class _CallClient(SingleCallPromptGuardClient):
    adapter = "fixture"
    policy_fingerprint = "a" * 64
    timeout_seconds = 0.05

    def __init__(self) -> None:
        self.calls: list[PromptGuardCall] = []
        self.action = "raise"

    async def inspect_call(self, call: PromptGuardCall) -> PromptGuardCallResult:
        self.calls.append(call)
        if self.action == "raise":
            raise RuntimeError("call ended")
        if self.action == "cancel":
            raise asyncio.CancelledError()
        if self.action == "integrity":
            raise AuditIntegrityError("Call result failed integrity")
        if self.action == "wait":
            await asyncio.sleep(0.2)
        return PromptGuardCallResult(PromptGuardDecision("allow", "clean"))


class _GeneratorContextClient(_CallClient):
    @asynccontextmanager
    async def request_context(self) -> AsyncIterator[None]:
        with suppress(Exception, asyncio.CancelledError):
            yield


class _SuppressingContext(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> bool:
        return exc_type is not None and issubclass(exc_type, Exception)


class _ClassContextClient(_CallClient):
    def request_context(self) -> AbstractAsyncContextManager[None]:
        return _SuppressingContext()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client_type",
    [
        pytest.param(_GeneratorContextClient, id="generator-context"),
        pytest.param(_ClassContextClient, id="class-context"),
    ],
)
async def test_check_fails_closed_when_a_call_exception_produces_no_decision(client_type: type[_CallClient]) -> None:
    client = client_type()
    decision = await inspect_prompt_guard_request(client, _request())
    assert decision == PromptGuardDecision("unavailable", "service_error")
    assert len(client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client_type",
    [
        pytest.param(_GeneratorContextClient, id="generator-context"),
        pytest.param(_ClassContextClient, id="class-context"),
    ],
)
async def test_integrity_error_is_raised_when_the_request_context_ends_exceptions(client_type: type[_CallClient]) -> None:
    client = client_type()
    client.action = "integrity"
    with pytest.raises(AuditIntegrityError, match=r"^Call result failed integrity$"):
        await inspect_prompt_guard_request(client, _request())
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_check_fails_closed_when_call_cancellation_produces_no_decision() -> None:
    client = _GeneratorContextClient()
    client.action = "cancel"
    decision = await inspect_prompt_guard_request(client, _request())
    assert decision == PromptGuardDecision("unavailable", "service_error")
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_call_exception_propagates_when_the_context_leaves_it_open() -> None:
    client = _CallClient()
    with pytest.raises(RuntimeError, match="call ended"):
        await inspect_prompt_guard_request(client, _request())
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_deadline_without_a_call_decision_is_audited_and_admits_no_units(guard_sessions) -> None:
    engine, open_owner = guard_sessions
    owner = open_owner()
    client = _GeneratorContextClient()
    client.action = "wait"
    mount = PromptGuardMount(client)
    units = [_event(0)]
    custodies = []
    with pytest.raises(PromptGuardRefused) as caught:
        await _check(mount, owner, texts=units, custody_out=custodies)
    assert caught.value.disposition == "unavailable"
    assert caught.value.http_status == 503
    check = _last_check(engine, custodies[0])
    assert (check["disposition"], check["reason"]) == ("unavailable", "service_error")
    assert check["audit_id"] == caught.value.audit_id
    first_calls = len(client.calls)
    client.action = "allow"
    await _check(mount, owner, texts=units)
    assert len(client.calls) - first_calls == 2
    assert [call.payload for call in client.calls[first_calls:]] == [call.payload for call in client.calls[:first_calls]]
    assert _last_check(engine, custodies[0])["disposition"] == "allow"


def _request() -> PromptGuardRequest:
    return PromptGuardRequest("composer", "Prompt", ("Document",), "a" * 64)
