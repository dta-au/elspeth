"""Mounted handler domains with genuine completed manual projection custody."""

from __future__ import annotations

import asyncio
import errno
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.routing import Mount
from structlog.testing import capture_logs

from elspeth.contracts.errors import ComposerOwnedSettlementFailure
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.async_workers import AsyncWorkerAdmissionTimeoutError
from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.required_executor import RequiredGenerationUnavailable
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkAuthority, RequiredWorkCoordinator, RequiredWorkSource
from elspeth.web.sessions.composer_operations import ComposerOperationCancel, ComposerOperationCancelReason
from elspeth.web.sessions.manual_proposal_failure import (
    ComposerManualProposalFailure,
    consume_manual_proposal_failure,
    issue_manual_proposal_failure,
)
from elspeth.web.sessions.pipeline_rejection_custody import (
    close_required_proposal_lease,
    original_outcome_group,
    raise_required_proposal_failure,
)
from tests.unit.web.sessions.test_manual_proposal_failure_carrier import assert_omission_cannot_obtain_consumed_manual_presentation
from tests.unit.web.sessions.test_pipeline_rejection_callers import mounted_app
from tests.unit.web.sessions.test_pipeline_rejection_required import prepared_rejection

pytest_plugins = ("tests.unit.web.coordination.test_composer_operation_authority",)
pytestmark = pytest.mark.timeout(30, method="thread")


async def actual_completed_manual_projection(store, errors):
    _engine, repository, _authority, service, sid = store
    expected, running = await prepared_rejection(store)
    repository.release(running.session_operation_context)
    lease = await SessionOperationLease.acquire(
        repository,
        session_id=sid,
        operation_kind=SessionOperationKind.PROPOSAL,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=30,
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.MANUAL_PROPOSAL,
            lease.context,
            proposal_id=str(expected.authority.row.id),
            invocation_id=str(uuid4()),
            tool_call_id=expected.authority.row.tool_call_id,
        )
    )
    lease.bind_required_work(coordinator)
    for error in errors:
        projection = coordinator.reserve(RequiredWorkSource.POSTCOMMIT_REVIEW_PROJECTION, recurrence_ordinal=len(coordinator.tickets))
        projection.begin_projection()
        projection.complete_owned(error)
    close_originals = await close_required_proposal_lease(lease, coordinator=coordinator)
    assert close_originals == ()
    root = original_outcome_group("actual registered manual projection originals", *errors)
    return lease, coordinator, root


def _place_test_route_before_spa(app, path: str) -> None:
    routes = app.router.routes
    appended = routes[-1]
    assert appended.path == path
    spa_indexes = [index for index, route in enumerate(routes[:-1]) if type(route) is Mount and route.path == "" and route.name == "spa"]
    assert len(spa_indexes) <= 1
    if spa_indexes:
        routes.insert(spa_indexes[0], routes.pop())


async def mounted_carrier_response(store, tmp_path, carrier):
    app = mounted_app(store, tmp_path)

    @app.get("/_tests/manual-carrier")
    async def raise_actual_carrier():
        raise carrier

    _place_test_route_before_spa(app, "/_tests/manual-carrier")
    request_id = str(uuid4())
    with capture_logs() as logs:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/_tests/manual-carrier", headers={"X-Request-ID": request_id})
    return response, request_id, logs


@pytest.mark.asyncio
async def test_mounted_owned_http_projection_preserves_headers(operation_store, tmp_path):
    status = 409
    error = HTTPException(
        status, detail={"error_type": "manual_test_error", "detail": "controlled reviewed detail"}, headers={"Retry-After": "7"}
    )
    lease, coordinator, root = await actual_completed_manual_projection(operation_store, (error,))
    carrier = issue_manual_proposal_failure(coordinator, lease, root)
    assert type(carrier) is ComposerManualProposalFailure
    response, request_id, _logs = await mounted_carrier_response(operation_store, tmp_path, carrier)
    assert response.status_code == status and response.headers["Retry-After"] == "7"
    assert response.json()["detail"]["request_id"] == request_id


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [204, 304])
async def test_mounted_actual_manual_boundary_preserves_original_http_outside_durable_error_dto(operation_store, tmp_path, status):
    error = HTTPException(status, detail={"error_type": "controlled_bodiless_original"}, headers={"Retry-After": "7"})
    lease, coordinator, root = await actual_completed_manual_projection(operation_store, (error,))
    assert root is error
    app = mounted_app(operation_store, tmp_path)
    actual_handler = app.exception_handlers[HTTPException]
    captured = []

    async def observe_actual_http_handler(request, original):
        captured.append(original)
        return await actual_handler(request, original)

    app.exception_handlers[HTTPException] = observe_actual_http_handler

    @app.get("/_tests/manual-original-boundary")
    async def actual_manual_boundary():
        raise_required_proposal_failure(coordinator, root, lease=lease)

    _place_test_route_before_spa(app, "/_tests/manual-original-boundary")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/_tests/manual-original-boundary", headers={"X-Request-ID": str(uuid4())})
    assert response.status_code == status
    assert response.headers["Retry-After"] == "7" and response.content == b""
    assert captured == [error] and captured[0] is error
    assert coordinator._manual_proposal_carrier is None
    assert coordinator._manual_proposal_close is not None and coordinator._manual_proposal_close.presentation_attempted


@pytest.mark.asyncio
async def test_manual_http_outside_dto_domain_keeps_complete_group_unresolved(operation_store):
    originals = (HTTPException(204, "controlled bodiless original"), HTTPException(409, "controlled ordinary conflict"))
    lease, coordinator, root = await actual_completed_manual_projection(operation_store, originals)
    with pytest.raises(BaseExceptionGroup) as caught:
        raise_required_proposal_failure(coordinator, root, lease=lease)
    assert caught.value is root
    assert all(any(leaf is original for leaf in root.exceptions) for original in originals)
    assert coordinator._manual_proposal_carrier is None


@pytest.mark.asyncio
async def test_mounted_equal_public_conflict_uses_existing_generic_policy(operation_store, tmp_path):
    errors = (HTTPException(409, "first controlled detail"), HTTPException(422, "second controlled detail"))
    lease, coordinator, root = await actual_completed_manual_projection(operation_store, errors)
    carrier = issue_manual_proposal_failure(coordinator, lease, root)
    assert type(carrier) is ComposerManualProposalFailure
    response, request_id, _logs = await mounted_carrier_response(operation_store, tmp_path, carrier)
    assert response.status_code == 500
    assert response.json()["detail"]["error_type"] == "operation_failed"
    assert response.json()["detail"]["request_id"] == request_id
    assert all(error.detail not in response.text for error in errors)


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["retryable_storage", "unsupported_storage", "generation", "admission", "generic_sql", "accounting"])
async def test_mounted_owned_projection_domain_preserves_existing_redaction_and_category(operation_store, tmp_path, domain):
    errors = {
        "retryable_storage": OSError(errno.EIO, "SECRET_IO_PATH"),
        "unsupported_storage": FileNotFoundError(errno.ENOENT, "SECRET_IO_PATH"),
        "generation": RequiredGenerationUnavailable("SECRET_GENERATION_DETAIL"),
        "admission": AsyncWorkerAdmissionTimeoutError("SECRET_ADMISSION_DETAIL"),
        "generic_sql": SQLAlchemyError("SECRET_SQL_DETAIL"),
        "accounting": ComposerOwnedSettlementFailure(),
    }
    error = errors[domain]
    lease, coordinator, root = await actual_completed_manual_projection(operation_store, (error,))
    carrier = issue_manual_proposal_failure(coordinator, lease, root)
    assert type(carrier) is ComposerManualProposalFailure
    response, request_id, logs = await mounted_carrier_response(operation_store, tmp_path, carrier)
    if domain == "retryable_storage":
        assert response.status_code == 503 and response.json()["error_type"] == "storage_unavailable"
    elif domain in ("generation", "admission"):
        assert response.status_code == 503 and response.json()["error_type"] == "database_unavailable"
    else:
        assert response.status_code == 500 and response.json()["detail"]["error_type"] == "operation_failed"
    assert request_id in response.text
    assert all(
        secret not in response.text and secret not in repr(logs)
        for secret in ("SECRET_IO_PATH", "SECRET_GENERATION_DETAIL", "SECRET_ADMISSION_DETAIL", "SECRET_SQL_DETAIL")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["cancel", "deadline", "fence"])
async def test_actual_manual_projection_never_maps_temporal_or_fence_original_to_durable_dto(operation_store, domain):
    if domain == "cancel":
        error = asyncio.CancelledError("actual manual projection cancellation")
    elif domain == "deadline":
        error = asyncio.CancelledError(ComposerOperationCancel(ComposerOperationCancelReason.DEADLINE))
    else:
        error = SessionOperationFenceLost(FenceLossReason.STALE_EPOCH)
    lease, coordinator, root = await actual_completed_manual_projection(operation_store, (error,))
    assert issue_manual_proposal_failure(coordinator, lease, root) is None
    assert root is error


@pytest.mark.asyncio
@pytest.mark.parametrize("omit_registered_wrapper", [False, True])
async def test_registered_owned_cancellation_wrapper_requires_raw_identity_coverage(operation_store, omit_registered_wrapper):
    sql_cause = OperationalError("controlled known projection cause", {}, RuntimeError("controlled driver domain"))
    original = asyncio.CancelledError("actual registered owned projection original")
    original.__cause__ = sql_cause
    lease, coordinator, _root = await actual_completed_manual_projection(operation_store, (original,))
    root = sql_cause if omit_registered_wrapper else original_outcome_group("explicit registered wrapper and cause", original, sql_cause)
    carrier = issue_manual_proposal_failure(coordinator, lease, root)
    if omit_registered_wrapper:
        assert_omission_cannot_obtain_consumed_manual_presentation(carrier)
    else:
        assert type(carrier) is ComposerManualProposalFailure
        observation = consume_manual_proposal_failure(carrier)
        assert observation.original_root is root and observation.selected_original is sql_cause
        assert any(leaf is original for leaf in root.exceptions)
