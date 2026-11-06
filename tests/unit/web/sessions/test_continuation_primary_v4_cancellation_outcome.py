"""Exact required-work custody for V4 PRIMARY zero-use SQL disposition."""

from uuid import uuid4

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkCoordinator,
    RequiredWorkSource,
    RequiredWorkTicket,
)


def _coordinator() -> RequiredWorkCoordinator:
    context = SessionOperationContext(SessionOperationFence(str(uuid4()), str(uuid4()), "private-token", 1), SessionOperationKind.COMPOSE)
    return RequiredWorkCoordinator(RequiredWorkAuthority(RequiredAuthorityKind.DURABLE_COMPOSE, context, str(uuid4()), 1))


@pytest.mark.asyncio
async def test_v4_no_sdk_validator_requires_registered_source_and_actual_returned_handoff() -> None:
    coordinator = _coordinator()
    ticket = coordinator.reserve(RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL)
    value = ["original cancelled attempt"]
    outcome = await run_required_sql_finish_once(ticket, lambda: value)
    assert type(outcome) is RequiredSQLReturned and outcome.value is value
    coordinator.validate_undispatched_cancellation_sql_outcome(ticket=ticket, actual_outcome=outcome)

    with pytest.raises(AuditIntegrityError, match="actual finish-once carrier"):
        coordinator.validate_undispatched_cancellation_sql_outcome(ticket=ticket, actual_outcome=RequiredSQLReturned(value, ()))
    with pytest.raises(AuditIntegrityError, match="exact registered source authority"):
        coordinator.validate_undispatched_cancellation_sql_outcome(
            ticket=RequiredWorkTicket(ticket.key, coordinator=coordinator), actual_outcome=outcome
        )
    other = coordinator.reserve(RequiredWorkSource.CONTINUATION_READ_SQL)
    other_outcome = await run_required_sql_finish_once(other, lambda: value)
    with pytest.raises(AuditIntegrityError, match="exact registered source authority"):
        coordinator.validate_undispatched_cancellation_sql_outcome(ticket=other, actual_outcome=other_outcome)


@pytest.mark.asyncio
async def test_v4_no_sdk_validator_preserves_original_raised_handoff() -> None:
    coordinator = _coordinator()
    ticket = coordinator.reserve(RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL)
    error = RuntimeError("original zero-use SQL failure")

    def fail() -> None:
        raise error

    outcome = await run_required_sql_finish_once(ticket, fail)
    assert type(outcome) is RequiredSQLRaised and outcome.error is error
    coordinator.validate_undispatched_cancellation_sql_outcome(ticket=ticket, actual_outcome=outcome)
    with pytest.raises(AuditIntegrityError, match="actual finish-once carrier"):
        coordinator.validate_undispatched_cancellation_sql_outcome(ticket=ticket, actual_outcome=RequiredSQLRaised(error, ()))
