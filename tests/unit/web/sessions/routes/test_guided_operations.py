"""Route adapter tests for retry-safe composer operations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.protocol import (
    GUIDED_OPERATION_FAILURE_CODE_VALUES,
    GuidedCompositionStateResult,
    GuidedOperationActive,
    GuidedOperationClaimed,
    GuidedOperationCompleted,
    GuidedOperationConflictError,
    GuidedOperationFailed,
    GuidedOperationFence,
)
from elspeth.web.sessions.routes.guided_operations import (
    _SAFE_FAILURES,
    GuidedOperationExpired,
    GuidedOperationLease,
    guided_response_hash,
    raise_guided_operation_failure,
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

    async def reserve_guided_operation(self, **_kwargs):
        self.reserve_calls += 1
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


@pytest.mark.asyncio
async def test_new_operation_returns_live_fence() -> None:
    session_id = uuid4()
    fence = GuidedOperationFence(session_id=session_id, operation_id=_request().operation_id, lease_token="secret", attempt=1)
    service = _Service([GuidedOperationClaimed(fence=fence, lease_expires_at=datetime.now(UTC) + timedelta(minutes=1))])

    result = await reserve_or_replay_guided_operation(
        service=service,
        session_id=session_id,
        kind="guided_reenter",
        request=_request(),
        replay=lambda _locator: _never(),
    )

    assert result == GuidedOperationLease(fence=fence)


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
    assert service.get_calls == 1
    assert service.reserve_calls == 1


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
    assert service.reserve_calls == 1
    assert service.get_calls == 1
    assert sleeps == [pytest.approx(0.05)]


@pytest.mark.asyncio
async def test_reserve_result_cannot_authorise_takeover_without_get_confirmation(monkeypatch) -> None:
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

    monkeypatch.setattr("elspeth.web.sessions.routes.guided_operations.asyncio.sleep", no_sleep)
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


async def _response(value: str) -> _Response:
    return _Response(value=value)


async def _never() -> _Response:
    raise AssertionError("replay callback must not run")


class TestClosedFailureEnvelope:
    """``_SAFE_FAILURES`` is the HTTP face of the closed failure vocabulary.

    ``raise_guided_operation_failure`` refuses an unmapped code with an
    ``AuditIntegrityError`` — a raw 500 — so a code added to
    ``GuidedOperationFailureCode`` without an entry here turns a well-classified
    failure back into the generic crash it was classified out of.
    """

    def test_every_closed_failure_code_has_an_http_envelope(self) -> None:
        assert set(_SAFE_FAILURES) == GUIDED_OPERATION_FAILURE_CODE_VALUES

    def test_policy_blocked_answers_422_without_provider_blame_or_a_retry_offer(self) -> None:
        """The permanent half of the split must not read as a transient fault.

        The observed failure (guided S3, 2026-07-31) reached the user as
        "The provider returned an invalid response. Retry with a new operation
        id." for a refusal that had ZERO provider calls and could never succeed
        on retry. The replacement copy must blame neither the provider nor the
        client's operation id, and must not invite a retry.
        """
        with pytest.raises(HTTPException) as caught:
            raise_guided_operation_failure(GuidedOperationFailed(failure_code="policy_blocked"))

        assert caught.value.status_code == 422
        detail = caught.value.detail
        assert isinstance(detail, dict)
        assert detail["error_type"] == "guided_operation_terminal_failure"
        assert detail["failure_code"] == "policy_blocked"
        copy = str(detail["detail"])
        lowered = copy.lower()
        assert "provider" not in lowered
        assert "operation id" not in lowered
        assert "deployment policy" in lowered
        # Names the permanence explicitly rather than offering a retry.
        assert "retrying will fail the same way" in lowered

    def test_permanent_and_transient_codes_are_partitioned_by_status_class(self) -> None:
        """A 5xx says "our side broke, try again"; a policy refusal is neither."""
        transient = ("provider_unavailable", "provider_timeout", "invalid_provider_response")
        for code in transient:
            status, copy = _SAFE_FAILURES[code]
            assert status >= 500, code
            assert "retry" in copy.lower(), code
        status, _copy = _SAFE_FAILURES["policy_blocked"]
        assert 400 <= status < 500

    def test_invalid_provider_response_copy_drops_the_operation_id_jargon(self) -> None:
        """The client mints a fresh operation id on every re-click by itself.

        Naming the id taught the reader an internal protocol detail they cannot
        act on; "Retry the request." is the whole actionable instruction.
        """
        _status, copy = _SAFE_FAILURES["invalid_provider_response"]

        assert copy.endswith("Retry the request.")
        assert "operation id" not in copy.lower()
