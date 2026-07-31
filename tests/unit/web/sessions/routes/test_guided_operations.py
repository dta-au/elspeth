"""Route adapter tests for retry-safe composer operations."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.contracts import (
    SessionOperationContext,
    SessionOperationFence,
    SessionOperationKind,
)
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions.protocol import (
    GuidedCompositionStateResult,
    GuidedOperationActive,
    GuidedOperationClaimed,
    GuidedOperationCompleted,
    GuidedOperationConflictError,
    GuidedOperationFailed,
    GuidedOperationFence,
)
from elspeth.web.sessions.routes import guided_operations as guided_operations_module
from elspeth.web.sessions.routes.guided_operations import (
    GuidedOperationExpired,
    GuidedOperationLease,
    guided_response_hash,
    reserve_or_replay_guided_operation,
)
from elspeth.web.sessions.schemas import ReenterGuidedRequest


class _Response(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    value: str


class _Service:
    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.reserve_calls = 0
        self.get_calls = 0
        self.reserve_kwargs = []
        self.session_operation_authority = object()
        self.session_operation_owner_instance_id = "route-test"
        self.session_operation_lease_seconds = 30

    async def reserve_guided_operation(self, **kwargs):
        self.reserve_calls += 1
        self.reserve_kwargs.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def get_guided_operation(self, **_kwargs):
        self.get_calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _request() -> ReenterGuidedRequest:
    return ReenterGuidedRequest(operation_id="00000000-0000-4000-8000-000000000001")


class _Lease:
    def __init__(self, context: SessionOperationContext) -> None:
        self.context = context
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _context(session_id, *, kind: SessionOperationKind = SessionOperationKind.COMPOSE) -> SessionOperationContext:
    return SessionOperationContext(
        fence=SessionOperationFence(
            session_id=str(session_id),
            operation_id="session-operation",
            lease_token="session-secret",
            operation_epoch=1,
        ),
        operation_kind=kind,
    )


@pytest.mark.asyncio
async def test_new_operation_acquires_compose_before_reserve_and_returns_composite_lease(monkeypatch) -> None:
    session_id = uuid4()
    fence = GuidedOperationFence(session_id=session_id, operation_id=_request().operation_id, lease_token="secret", attempt=1)
    service = _Service([None, GuidedOperationClaimed(fence=fence, lease_expires_at=datetime.now(UTC) + timedelta(minutes=1))])
    session_lease = _Lease(_context(session_id))
    acquired: list[dict[str, object]] = []

    async def acquire(_cls, authority, **kwargs):
        acquired.append({"authority": authority, **kwargs})
        return session_lease

    monkeypatch.setattr(SessionOperationLease, "acquire", classmethod(acquire))

    result = await reserve_or_replay_guided_operation(
        service=service,
        session_id=session_id,
        kind="guided_reenter",
        request=_request(),
        replay=lambda _locator: _never(),
    )

    assert result == GuidedOperationLease(fence=fence, session_lease=session_lease)
    assert acquired == [
        {
            "authority": service.session_operation_authority,
            "session_id": session_id,
            "operation_kind": SessionOperationKind.COMPOSE,
            "owner_instance_id": "route-test",
            "lease_seconds": 30,
        }
    ]
    assert service.reserve_kwargs[0]["session_operation_context"] is session_lease.context


@pytest.mark.asyncio
async def test_terminal_replay_never_acquires_session_authority(monkeypatch) -> None:
    session_id = uuid4()
    locator = GuidedCompositionStateResult(state_id=uuid4())
    response = _Response(value="replayed")
    service = _Service([GuidedOperationCompleted(result=locator, response_hash=guided_response_hash(response))])

    async def forbidden_acquire(*_args, **_kwargs):
        raise AssertionError("join/replay must not acquire session authority")

    monkeypatch.setattr(SessionOperationLease, "acquire", forbidden_acquire)

    result = await reserve_or_replay_guided_operation(
        service=service,
        session_id=session_id,
        kind="guided_reenter",
        request=_request(),
        replay=lambda _locator: _response("replayed"),
    )

    assert result == response
    assert service.get_calls == 1
    assert service.reserve_calls == 0


@pytest.mark.asyncio
async def test_acquire_reserve_race_releases_session_authority_before_join(monkeypatch) -> None:
    session_id = uuid4()
    locator = GuidedCompositionStateResult(state_id=uuid4())
    response = _Response(value="race winner")
    session_lease = _Lease(_context(session_id))

    class RaceService(_Service):
        async def get_guided_operation(self, **kwargs):
            if self.get_calls == 1:
                assert session_lease.closed
            return await super().get_guided_operation(**kwargs)

    service = RaceService(
        [
            None,
            GuidedOperationActive(attempt=1, lease_expires_at=datetime.now(UTC) + timedelta(seconds=30)),
            GuidedOperationCompleted(result=locator, response_hash=guided_response_hash(response)),
        ]
    )

    async def acquire(_cls, _authority, **_kwargs):
        return session_lease

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(SessionOperationLease, "acquire", classmethod(acquire))
    monkeypatch.setattr("elspeth.web.sessions.routes.guided_operations.asyncio.sleep", no_sleep)

    result = await reserve_or_replay_guided_operation(
        service=service,
        session_id=session_id,
        kind="guided_reenter",
        request=_request(),
        replay=lambda _locator: _response("race winner"),
    )

    assert result == response
    assert session_lease.closed


@pytest.mark.asyncio
async def test_cancellation_after_reserve_started_fails_guided_before_releasing_session(monkeypatch) -> None:
    session_id = uuid4()
    fence = GuidedOperationFence(session_id=session_id, operation_id=_request().operation_id, lease_token="secret", attempt=1)
    claimed = GuidedOperationClaimed(fence=fence, lease_expires_at=datetime.now(UTC) + timedelta(minutes=1))
    session_lease = _Lease(_context(session_id))
    reserve_started = asyncio.Event()
    finish_reserve = asyncio.Event()
    events: list[str] = []

    class CancellationService(_Service):
        async def reserve_guided_operation(self, **kwargs):
            self.reserve_kwargs.append(kwargs)
            reserve_started.set()
            try:
                await finish_reserve.wait()
            except asyncio.CancelledError:
                await finish_reserve.wait()
            return claimed

        async def fail_guided_operation(self, actual_fence, **kwargs):
            assert actual_fence == fence
            assert kwargs["session_operation_context"] is session_lease.context
            assert not session_lease.closed
            events.append("failed")
            return GuidedOperationFailed(failure_code="request_cancelled")

    service = CancellationService([None])

    async def acquire(_cls, _authority, **_kwargs):
        return session_lease

    monkeypatch.setattr(SessionOperationLease, "acquire", classmethod(acquire))
    task = asyncio.create_task(
        reserve_or_replay_guided_operation(
            service=service,
            session_id=session_id,
            kind="guided_reenter",
            request=_request(),
            replay=lambda _locator: _never(),
        )
    )
    await reserve_started.wait()
    task.cancel()
    finish_reserve.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert events == ["failed"]
    assert session_lease.closed


@pytest.mark.asyncio
async def test_operation_id_reuse_conflict_is_static_409() -> None:
    session_id = uuid4()
    service = _Service([GuidedOperationConflictError(session_id=session_id, operation_id=_request().operation_id)])

    with pytest.raises(HTTPException) as caught:
        await reserve_or_replay_guided_operation(
            service=service,
            session_id=session_id,
            kind="guided_reenter",
            request=_request(),
            replay=lambda _locator: _never(),
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == "Operation id is already bound to a different request."


@pytest.mark.asyncio
async def test_completed_operation_reconstructs_exact_strict_response() -> None:
    session_id = uuid4()
    locator = GuidedCompositionStateResult(state_id=uuid4())
    response = _Response(value="same")
    service = _Service([GuidedOperationCompleted(result=locator, response_hash=guided_response_hash(response))])

    async def replay(actual):
        assert actual == locator
        return response

    result = await reserve_or_replay_guided_operation(
        service=service,
        session_id=session_id,
        kind="guided_reenter",
        request=_request(),
        replay=replay,
    )

    assert result == response


@pytest.mark.asyncio
async def test_completed_operation_rejects_response_domain_hash_mismatch() -> None:
    session_id = uuid4()
    locator = GuidedCompositionStateResult(state_id=uuid4())
    service = _Service([GuidedOperationCompleted(result=locator, response_hash="0" * 64)])

    with pytest.raises(AuditIntegrityError, match="response hash"):
        await reserve_or_replay_guided_operation(
            service=service,
            session_id=session_id,
            kind="guided_reenter",
            request=_request(),
            replay=lambda _locator: _response("changed"),
        )


@pytest.mark.asyncio
async def test_active_operation_polls_to_terminal_replay(monkeypatch) -> None:
    session_id = uuid4()
    locator = GuidedCompositionStateResult(state_id=uuid4())
    response = _Response(value="joined")
    service = _Service(
        [
            GuidedOperationActive(attempt=1, lease_expires_at=datetime.now(UTC) + timedelta(seconds=1)),
            GuidedOperationCompleted(result=locator, response_hash=guided_response_hash(response)),
        ]
    )

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("elspeth.web.sessions.routes.guided_operations.asyncio.sleep", no_sleep)
    result = await reserve_or_replay_guided_operation(
        service=service,
        session_id=session_id,
        kind="guided_reenter",
        request=_request(),
        replay=lambda _locator: _response("joined"),
    )

    assert result == response
    assert service.get_calls == 2
    assert service.reserve_calls == 0


@pytest.mark.asyncio
async def test_host_clock_ahead_does_not_trigger_reserve_spin(monkeypatch) -> None:
    """Only the DB-computed expired flag authorises takeover."""

    session_id = uuid4()
    locator = GuidedCompositionStateResult(state_id=uuid4())
    response = _Response(value="joined despite skew")
    service = _Service(
        [
            GuidedOperationActive(
                attempt=1,
                # Deliberately behind the host clock while the authoritative
                # DB classification remains unexpired.
                lease_expires_at=datetime.now(UTC) - timedelta(minutes=10),
                expired=False,
            ),
            GuidedOperationCompleted(result=locator, response_hash=guided_response_hash(response)),
        ]
    )
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("elspeth.web.sessions.routes.guided_operations.asyncio.sleep", record_sleep)
    result = await reserve_or_replay_guided_operation(
        service=service,
        session_id=session_id,
        kind="guided_reenter",
        request=_request(),
        replay=lambda _locator: _response("joined despite skew"),
    )

    assert result == response
    assert service.reserve_calls == 0
    assert service.get_calls == 2
    assert sleeps == [pytest.approx(0.05)]


@pytest.mark.asyncio
async def test_db_expired_get_acquires_session_authority_before_takeover(monkeypatch) -> None:
    session_id = uuid4()
    locator = GuidedCompositionStateResult(state_id=uuid4())
    response = _Response(value="joined")
    service = _Service(
        [
            GuidedOperationActive(
                attempt=1,
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
                expired=True,
            ),
            GuidedOperationCompleted(result=locator, response_hash=guided_response_hash(response)),
        ]
    )

    async def no_sleep(_seconds: float) -> None:
        return None

    session_lease = _Lease(_context(session_id))

    async def acquire(_cls, _authority, **_kwargs):
        return session_lease

    monkeypatch.setattr("elspeth.web.sessions.routes.guided_operations.asyncio.sleep", no_sleep)
    monkeypatch.setattr(SessionOperationLease, "acquire", classmethod(acquire))
    result = await reserve_or_replay_guided_operation(
        service=service,
        session_id=session_id,
        kind="guided_reenter",
        request=_request(),
        replay=lambda _locator: _response("joined"),
    )

    assert result == response
    assert service.reserve_calls == 1
    assert service.get_calls == 1
    assert session_lease.closed


@pytest.mark.asyncio
async def test_replay_only_lookup_returns_none_for_expired_operation_without_takeover() -> None:
    session_id = uuid4()
    service = _Service(
        [
            GuidedOperationActive(
                attempt=1,
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
                expired=True,
            )
        ]
    )

    result = await reserve_or_replay_guided_operation(
        service=service,
        session_id=session_id,
        kind="guided_reenter",
        request=_request(),
        replay=lambda _locator: _never(),
        reserve_if_absent=False,
        takeover_expired=False,
    )

    assert result == GuidedOperationExpired(attempt=1)
    assert service.get_calls == 1
    assert service.reserve_calls == 0


@pytest.mark.asyncio
async def test_non_taking_over_mode_rejects_reserve_if_absent() -> None:
    service = _Service([])

    with pytest.raises(AuditIntegrityError, match="must not reserve"):
        await reserve_or_replay_guided_operation(
            service=service,
            session_id=uuid4(),
            kind="guided_reenter",
            request=_request(),
            replay=lambda _locator: _never(),
            takeover_expired=False,
        )

    assert service.get_calls == 0
    assert service.reserve_calls == 0


@pytest.mark.asyncio
async def test_failed_operation_maps_only_closed_safe_failure() -> None:
    service = _Service([GuidedOperationFailed(failure_code="provider_timeout")])

    with pytest.raises(HTTPException) as caught:
        await reserve_or_replay_guided_operation(
            service=service,
            session_id=uuid4(),
            kind="guided_reenter",
            request=_request(),
            replay=lambda _locator: _never(),
        )

    assert caught.value.status_code == 504
    assert caught.value.detail == {
        "error_type": "guided_operation_terminal_failure",
        "failure_code": "provider_timeout",
        "detail": "The operation timed out. Retry with a new operation id.",
    }


@pytest.mark.asyncio
async def test_terminal_stale_settlement_conflict_maps_to_safe_http_409() -> None:
    service = _Service([GuidedOperationFailed(failure_code="stale_conflict")])

    with pytest.raises(HTTPException) as caught:
        await reserve_or_replay_guided_operation(
            service=service,
            session_id=uuid4(),
            kind="guided_reenter",
            request=_request(),
            replay=lambda _locator: _never(),
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == {
        "error_type": "guided_operation_terminal_failure",
        "failure_code": "stale_conflict",
        "detail": "The guided state changed before settlement. Reload the authoritative state.",
    }


@pytest.mark.asyncio
async def test_guided_lease_guard_repeated_cancellation_fails_guided_before_exact_once_close() -> None:
    guard_factory = getattr(guided_operations_module, "guided_operation_lease_guard", None)
    assert guard_factory is not None
    session_id = uuid4()
    fence = GuidedOperationFence(session_id=session_id, operation_id="guard-cancel", lease_token="guided-secret", attempt=1)
    context = _context(session_id)
    failure_entered = asyncio.Event()
    release_failure = asyncio.Event()
    events: list[str] = []

    class OrderedLease(_Lease):
        async def close(self) -> None:
            assert not self.closed
            events.append("close")
            await super().close()

    class GuardService:
        async def fail_guided_operation(self, actual_fence, **kwargs):
            assert actual_fence == fence
            assert kwargs["session_operation_context"] == context
            events.append("fail-start")
            failure_entered.set()
            await release_failure.wait()
            events.append("fail-end")
            return GuidedOperationFailed(failure_code="request_cancelled")

    lease = OrderedLease(context)
    reserved = GuidedOperationLease(fence=fence, session_lease=lease)
    entered = asyncio.Event()

    async def run() -> None:
        async with guard_factory(
            service=GuardService(),
            lease=reserved,
        ):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    await entered.wait()
    task.cancel("first guided cancellation")
    await failure_entered.wait()
    task.cancel("repeated guided cancellation")
    await asyncio.sleep(0)
    assert not task.done()
    release_failure.set()
    with pytest.raises(asyncio.CancelledError, match="first guided cancellation") as caught:
        await task

    assert caught.value.args == ("first guided cancellation",)
    assert events == ["fail-start", "fail-end", "close"]
    assert lease.closed


@pytest.mark.asyncio
async def test_guided_lease_guard_preserves_primary_with_sanitized_cleanup_notes() -> None:
    guard_factory = getattr(guided_operations_module, "guided_operation_lease_guard", None)
    assert guard_factory is not None
    session_id = uuid4()
    fence = GuidedOperationFence(session_id=session_id, operation_id="guard-error", lease_token="guided-secret", attempt=1)
    context = _context(session_id)

    class FailingLease(_Lease):
        async def close(self) -> None:
            raise ValueError("SESSION-CLEANUP-SECRET")

    class GuardService:
        async def fail_guided_operation(self, *_args, **_kwargs):
            raise RuntimeError("GUIDED-CLEANUP-SECRET")

    primary = KeyError("primary failure")
    with pytest.raises(KeyError) as caught:
        async with guard_factory(
            service=GuardService(),
            lease=GuidedOperationLease(fence=fence, session_lease=FailingLease(context)),
        ):
            raise primary

    assert caught.value is primary
    rendered_notes = repr(caught.value.__notes__)
    assert "RuntimeError" in rendered_notes
    assert "ValueError" in rendered_notes
    assert "GUIDED-CLEANUP-SECRET" not in rendered_notes
    assert "SESSION-CLEANUP-SECRET" not in rendered_notes


@pytest.mark.asyncio
async def test_guided_lease_guard_rejects_normal_exit_while_guided_fence_is_still_live() -> None:
    guard_factory = getattr(guided_operations_module, "guided_operation_lease_guard", None)
    assert guard_factory is not None
    session_id = uuid4()
    fence = GuidedOperationFence(session_id=session_id, operation_id="guard-live-return", lease_token="secret", attempt=1)
    context = _context(session_id)
    lease = _Lease(context)
    calls: list[str] = []

    class GuardService:
        async def fail_guided_operation(self, actual_fence, **kwargs):
            assert actual_fence == fence
            assert kwargs["failure_code"] == "operation_failed"
            calls.append("failed")
            return GuidedOperationFailed(failure_code="operation_failed")

    with pytest.raises(AuditIntegrityError, match="returned before its guided operation became terminal"):
        async with guard_factory(
            service=GuardService(),
            lease=GuidedOperationLease(fence=fence, session_lease=lease),
        ):
            pass

    assert calls == ["failed"]
    assert lease.closed


async def _response(value: str) -> _Response:
    return _Response(value=value)


async def _never() -> _Response:
    raise AssertionError("replay callback must not run")
