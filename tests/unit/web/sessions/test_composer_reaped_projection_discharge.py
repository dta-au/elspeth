"""The reaper discharges a retained owner's failure projection only from its own exact settlement.

A turn whose failed-terminal write was refused before it wrote keeps its
TERMINAL_FAILURE_PROJECTION open. When the reaper's settle_own_lapsed settles
that job, the worker completes the projection from the typed settled record:
same job, attempt, fence operation and epoch, failed, and settled by
settle_own_lapsed. Any other exact mismatch is an integrity failure and the
coordinator stays retained; a job another settler finished is left alone.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from uuid import uuid4

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.contracts import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkCoordinator,
    RequiredWorkSource,
)
from elspeth.web.sessions.composer_async_worker import ComposerAsyncWorker
from elspeth.web.sessions.composer_operation_errors import worker_lost_error
from elspeth.web.sessions.composer_operations import COMPOSER_OPERATION_RESULT_SCHEMA_SUCCESS, ComposerOperationRecord
from elspeth.web.sessions.operation_codec import strict_response_hash
from elspeth.web.sessions.schemas import ChatMessageResponse, MessageWithStateResponse
from tests.unit.web.sessions.test_composer_async_worker import _admit, _file_app, _worker


async def _setup(tmp_path) -> tuple[ComposerAsyncWorker, ComposerOperationRecord, RequiredWorkCoordinator]:
    app, service, _engine, _composer = _file_app(tmp_path)
    authority = app.state.composer_async_operation_authority
    worker = _worker(app, authority)
    admitted = await _admit(app, service, authority, operation_id=str(uuid4()))
    failed = authority.settle_unstarted(
        None, session_id=admitted.session_id, operation_id=admitted.operation_id, failure=worker_lost_error(request_id=None)
    )
    context = SessionOperationContext(
        SessionOperationFence(
            session_id=str(admitted.session_id), operation_id=str(uuid4()), lease_token="reaped-turn-lease", operation_epoch=2
        ),
        SessionOperationKind.COMPOSE,
    )
    # The shape settle_own_lapsed returns for a started, lapsed turn.
    settled = replace(
        failed,
        started_at=failed.created_at,
        attempt=1,
        claim_owner_instance_id=worker._owner_instance_id,
        session_operation_id=context.fence.operation_id,
        session_operation_epoch=context.fence.operation_epoch,
        settled_by="settle_own_lapsed",
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.DURABLE_COMPOSE, context, settled.operation_id, settled.attempt)
    )
    ComposerAsyncWorker._reserve_failed_terminal_projection(coordinator)
    worker._required_coordinators[(settled.session_id, settled.operation_id)] = coordinator
    return worker, settled, coordinator


def _completed(settled: ComposerOperationRecord) -> ComposerOperationRecord:
    """The same settlement as a valid completed record: only the status disagrees with a reaped failure."""
    response = MessageWithStateResponse(
        message=ChatMessageResponse(
            id=str(uuid4()),
            session_id=str(settled.session_id),
            role="assistant",
            content="done",
            segments=[],
            created_at=settled.created_at,
            sequence_no=1,
        ),
        state=None,
        proposals=[],
    )
    result_json = response.model_dump_json()
    return replace(
        settled,
        status="completed",
        failure_code=None,
        user_message_id=uuid4(),
        result_schema=COMPOSER_OPERATION_RESULT_SCHEMA_SUCCESS,
        result_json=result_json,
        result_sha256=strict_response_hash(response),
    )


@pytest.mark.asyncio
async def test_the_exact_own_lapsed_settlement_completes_the_projection_and_drops_the_coordinator(tmp_path) -> None:
    worker, settled, coordinator = await _setup(tmp_path)
    key = (settled.session_id, settled.operation_id)
    assert coordinator.all_completed is False

    worker._discharge_reaped_failure_projection(key, settled)

    assert coordinator.all_completed is True
    assert key not in worker._required_coordinators


@pytest.mark.asyncio
async def test_a_job_another_settler_finished_leaves_the_coordinator_retained(tmp_path) -> None:
    worker, settled, coordinator = await _setup(tmp_path)
    key = (settled.session_id, settled.operation_id)

    worker._discharge_reaped_failure_projection(key, replace(settled, settled_by="settle_lost_inactive_session"))

    assert coordinator.all_completed is False
    assert worker._required_coordinators[key] is coordinator


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch",
    ["epoch", "fence_operation", "attempt", "other_job", "completed"],
)
async def test_an_exact_authority_mismatch_is_an_integrity_failure_and_keeps_custody(tmp_path, mismatch: str) -> None:
    worker, settled, coordinator = await _setup(tmp_path)
    key = (settled.session_id, settled.operation_id)
    variants: dict[str, Callable[[], ComposerOperationRecord]] = {
        "epoch": lambda: replace(settled, session_operation_epoch=settled.session_operation_epoch + 1),
        "fence_operation": lambda: replace(settled, session_operation_id=str(uuid4())),
        "attempt": lambda: replace(settled, attempt=settled.attempt + 1),
        "other_job": lambda: replace(settled, operation_id=str(uuid4())),
        "completed": lambda: _completed(settled),
    }
    changed = variants[mismatch]()

    with pytest.raises(AuditIntegrityError):
        worker._discharge_reaped_failure_projection(key, changed)

    assert coordinator.all_completed is False
    assert worker._required_coordinators[key] is coordinator


@pytest.mark.asyncio
async def test_retained_work_beyond_the_projection_is_an_integrity_failure(tmp_path) -> None:
    worker, settled, coordinator = await _setup(tmp_path)
    key = (settled.session_id, settled.operation_id)
    coordinator.reserve(RequiredWorkSource.TERMINAL_WRITER_READ_SQL)

    with pytest.raises(AuditIntegrityError):
        worker._discharge_reaped_failure_projection(key, settled)

    assert worker._required_coordinators[key] is coordinator


@pytest.mark.asyncio
async def test_a_coordinator_bound_to_another_durable_operation_is_an_integrity_failure(tmp_path) -> None:
    worker, settled, coordinator = await _setup(tmp_path)
    key = (settled.session_id, settled.operation_id)
    authority = coordinator.authority
    corrupt = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.DURABLE_COMPOSE, authority.context, str(uuid4()), authority.claim_attempt)
    )
    ComposerAsyncWorker._reserve_failed_terminal_projection(corrupt)
    worker._required_coordinators[key] = corrupt

    with pytest.raises(AuditIntegrityError):
        worker._discharge_reaped_failure_projection(key, settled)

    assert corrupt.all_completed is False
    assert worker._required_coordinators[key] is corrupt
