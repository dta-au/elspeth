"""Durable admission and observation for detached Composer turns."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Annotated, cast
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts.credential_material import scrub_credential_material
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import AsyncWorkerAdmissionTimeoutError, run_stream_read_in_worker, run_sync_in_worker
from elspeth.web.auth.models import AuthenticationError
from elspeth.web.compartments import compartment_ingress_record
from elspeth.web.composer_stream import BoundedComposerStreamResponse, ComposerStreamCapacityError, ComposerStreamPermits
from elspeth.web.composer_stream_auth import ComposerStreamAuthServices
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority, admit_composer_operation
from elspeth.web.coordination.rate_limit_authority import ComposerQuotaExceeded
from elspeth.web.credential_guard import CredentialMaterialRefused, require_no_credential_material
from elspeth.web.middleware.rate_limit import SharedRateLimiter
from elspeth.web.sessions.composer_operation_errors import request_cancelled_error
from elspeth.web.sessions.composer_operation_projection_v2 import (
    ComposerOperationProjectionUnstable,
    project_v2_snapshot,
    project_v2_status,
    read_active_operation,
    read_v2_projection_inputs,
)
from elspeth.web.sessions.composer_operation_versions import (
    COMPOSER_OPERATION_RESUME_TOKEN_INVALID,
    COMPOSER_OPERATION_VERSION_HEADER,
    COMPOSER_OPERATION_VERSION_UNSUPPORTED,
    ComposerOperationResumeTokenInvalid,
    ComposerOperationVersion,
    ComposerOperationVersionUnsupported,
    can_read,
    parse_composer_operation_version,
    parse_last_event_id,
    version_header,
)
from elspeth.web.sessions.composer_operations import (
    ComposerOperationActiveError,
    ComposerOperationCapacityError,
    ComposerOperationConflictError,
    ComposerOperationError,
    ComposerOperationKind,
    ComposerOperationPreconditionRefused,
    ComposerOperationRecord,
    composer_operation_request_hash,
    composer_operation_v2_request_hash,
    decode_composer_failure_cursor,
    encode_composer_failure_cursor,
)
from elspeth.web.sessions.schemas import (
    ComposerActiveOperationResponse,
    ComposerOperationAcceptedResponse,
    ComposerOperationFailurePageResponse,
    ComposerOperationProgressV2,
    ComposerOperationSnapshotV2,
    ComposerOperationStatusResponse,
    ComposerOperationStreamFrameV2,
    ComposerOperationStreamHeartbeatFrame,
    ComposerOperationStreamHeartbeatFrameV2,
    ComposerOperationStreamProgressFrame,
    ComposerOperationStreamProgressFrameV2,
    ComposerOperationStreamProgressPayload,
    ComposerOperationStreamReconnectFrameV2,
    ComposerOperationStreamStatusFrame,
    ComposerOperationStreamStatusFrameV2,
    ComposerOperationStreamStatusPayload,
    ComposerOperationStreamTerminalFrame,
    ComposerOperationStreamTerminalFrameV2,
    ComposerOperationStreamTerminalPayload,
    ComposerOperationTerminalV2,
    ComposerOperationVersionRequiredDetail,
    MessageWithStateResponse,
    RecomposeRequest,
    SendMessageRequest,
    encode_composer_operation_stream_frame,
    encode_composer_operation_stream_frame_v2,
)

from .._helpers import (
    UserIdentity,
    WebRateLimiter,
    _get_composer_progress_registry,
    _verify_session_ownership,
    require_pipeline_user,
)

router = APIRouter()


def _authority(request: Request) -> ComposerAsyncOperationAuthority:
    return cast(ComposerAsyncOperationAuthority, request.app.state.composer_async_operation_authority)


def client_version(request: Request) -> ComposerOperationVersion:
    """The client's Composer-Operation-Version; 406 before any side effect when unsupported."""
    try:
        return parse_composer_operation_version(request.headers.getlist(COMPOSER_OPERATION_VERSION_HEADER))
    except ComposerOperationVersionUnsupported as exc:
        raise HTTPException(
            status_code=406,
            detail={"error_type": COMPOSER_OPERATION_VERSION_UNSUPPORTED, "detail": str(exc)},
            headers={"Cache-Control": "no-store", **version_header(1)},
        ) from exc


def _version_required(operation_id: str) -> HTTPException:
    detail = ComposerOperationVersionRequiredDetail(
        detail="This operation needs a Composer-Operation-Version 2 reader.", operation_id=operation_id
    )
    return HTTPException(status_code=409, detail=detail.model_dump(mode="json"), headers={"Cache-Control": "no-store", **version_header(1)})


def with_version(exc: HTTPException, version: ComposerOperationVersion) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail, headers={**(exc.headers or {}), **version_header(version)})


def composer_operation_deadline_remaining_ms(*, deadline_at: datetime, db_now: datetime) -> int:
    if deadline_at.utcoffset() is None or db_now.utcoffset() is None:
        raise ValueError("Composer operation times must be timezone-aware")
    return max(0, (deadline_at - db_now) // timedelta(milliseconds=1))


def composer_operation_status_response(
    row: ComposerOperationRecord, *, db_now: datetime, poll_after_ms: int
) -> ComposerOperationStatusResponse:
    remaining_ms = composer_operation_deadline_remaining_ms(deadline_at=row.legacy_deadline_at, db_now=db_now)
    if row.status in ("completed", "failed") and row.result_json is None:
        raise AuditIntegrityError("Terminal operation has no sealed result")
    result = MessageWithStateResponse.model_validate_json(cast(str, row.result_json), strict=True) if row.status == "completed" else None
    error = ComposerOperationError.model_validate_json(cast(str, row.result_json), strict=True) if row.status == "failed" else None
    return ComposerOperationStatusResponse(
        operation_id=row.operation_id,
        kind=row.kind,
        status=row.status,
        poll_after_ms=poll_after_ms,
        cancel_requested=row.cancel_requested_at is not None and row.status in ("queued", "running"),
        deadline_at=row.legacy_deadline_at,
        deadline_remaining_ms=remaining_ms if row.status in ("queued", "running") else 0,
        result=result,
        error=error,
    )


def _legacy_continuation_stop_response(row: ComposerOperationRecord, *, poll_after_ms: int) -> ComposerOperationStatusResponse:
    """A stale reader may Stop V2 even though its ordinary GET requires V2.

    The deadline is an acknowledgement timestamp, never a V2 request deadline:
    the committed Stop marker while live, or the settlement time when terminal.
    """
    if row.lifecycle_version != 2:
        raise AuditIntegrityError("Legacy continuation Stop projection requires a lifecycle-2 operation")
    live = row.status in ("queued", "running")
    deadline_at = row.cancel_requested_at if live else row.settled_at
    if deadline_at is None:
        raise AuditIntegrityError("Continuation Stop acknowledgement has no committed timestamp")
    if not live and row.result_json is None:
        raise AuditIntegrityError("Terminal operation has no sealed result")
    result = MessageWithStateResponse.model_validate_json(cast(str, row.result_json), strict=True) if row.status == "completed" else None
    error = ComposerOperationError.model_validate_json(cast(str, row.result_json), strict=True) if row.status == "failed" else None
    return ComposerOperationStatusResponse(
        operation_id=row.operation_id,
        kind=row.kind,
        status=row.status,
        poll_after_ms=poll_after_ms,
        cancel_requested=live and row.cancel_requested_at is not None,
        deadline_at=deadline_at,
        deadline_remaining_ms=0,
        result=result,
        error=error,
    )


async def _owned_json_body(request: Request, session_id: UUID, user: UserIdentity) -> bytes:
    encoded = await request.body()
    try:
        json.loads(encoded)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid JSON request body") from exc
    await _verify_session_ownership(session_id, user, request)
    return encoded


async def parse_message_body(request: Request, session_id: UUID, user: UserIdentity) -> SendMessageRequest:
    encoded = await _owned_json_body(request, session_id, user)
    try:
        return SendMessageRequest.model_validate_json(encoded, strict=True)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


async def parse_recompose_body(request: Request, session_id: UUID, user: UserIdentity) -> RecomposeRequest:
    encoded = await _owned_json_body(request, session_id, user)
    try:
        return RecomposeRequest.model_validate_json(encoded, strict=True)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


async def admit(
    request: Request,
    response: Response,
    session_id: UUID,
    body: SendMessageRequest | RecomposeRequest,
    user: UserIdentity,
    rate_limiter: WebRateLimiter,
    kind: ComposerOperationKind,
    version: ComposerOperationVersion,
) -> ComposerOperationAcceptedResponse | ComposerOperationSnapshotV2:
    try:
        return await _admit(request, response, session_id, body, user, rate_limiter, kind, version)
    except HTTPException as exc:
        # Every admission refusal projects the v1 error body.
        raise with_version(exc, 1) from exc


async def _admit(
    request: Request,
    response: Response,
    session_id: UUID,
    body: SendMessageRequest | RecomposeRequest,
    user: UserIdentity,
    rate_limiter: WebRateLimiter,
    kind: ComposerOperationKind,
    version: ComposerOperationVersion,
) -> ComposerOperationAcceptedResponse | ComposerOperationSnapshotV2:
    await _verify_session_ownership(session_id, user, request)
    settings = request.app.state.settings
    if isinstance(body, SendMessageRequest):
        try:
            require_no_credential_material(body.content, surface="web_message_ingress")
        except CredentialMaterialRefused as exc:
            raise HTTPException(status_code=422, detail=exc.to_payload()) from exc
        compartment_ingress_record(body.content, own_compartment_id=settings.compartment_id)

    if not isinstance(rate_limiter, SharedRateLimiter):
        raise AuditIntegrityError("Composer admission requires the application's shared SQL composer budget")

    try:
        row, _fresh = await admit_composer_operation(
            _authority(request),
            quota=rate_limiter.composer_admission,
            session_id=session_id,
            operation_id=body.operation_id,
            kind=kind,
            request_hash=(
                composer_operation_request_hash(session_id=session_id, kind=kind, request=body)
                if version == 1
                else composer_operation_v2_request_hash(session_id=session_id, kind=kind, request=body)
            ),
            actor_user_id=user.user_id,
            request_id=request.state.request_id,
            base_state_id=body.state_id,
            request_json=body.model_dump_json(),
            deadline_seconds=settings.composer_timeout_seconds,
            max_nonterminal=settings.composer_async_max_queued_operations,
            auth_provider_type=settings.auth_provider,
            lifecycle_version=version,
            continuation_enabled=settings.composer_continuation_enabled,
        )
    except ComposerQuotaExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "error_type": "rate_limited",
                "detail": f"Rate limit exceeded. Try again in {exc.retry_after} seconds.",
                "retry_after": exc.retry_after,
            },
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    except (SQLAlchemyError, AsyncWorkerAdmissionTimeoutError):
        raise HTTPException(status_code=503, detail="Rate limit service unavailable") from None
    except ComposerOperationPreconditionRefused as exc:
        raise HTTPException(status_code=exc.error.http_status, detail=exc.error.body["detail"]) from exc
    except ComposerOperationCapacityError as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "error_type": "composer_queue_full",
                "detail": "The composer is busy. Retry shortly.",
                "retry_after": exc.retry_after_seconds,
            },
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except ComposerOperationActiveError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error_type": "composer_operation_active",
                "operation_id": exc.operation_id,
                "kind": exc.kind,
                "detail": "This session already has a composer request in progress.",
            },
        ) from exc
    except ComposerOperationConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error_type": "composer_operation_conflict", "detail": "This operation id was already used for a different request."},
        ) from exc
    request.app.state.composer_async_worker.notify_work()
    if row.lifecycle_version == 2:
        if not can_read(version, 2):
            raise _version_required(row.operation_id)
        try:
            inputs = await run_sync_in_worker(
                read_v2_projection_inputs,
                _authority(request),
                request.app.state.session_engine,
                session_id=session_id,
                operation_id=row.operation_id,
            )
        except ComposerOperationProjectionUnstable:
            # Admission committed; the client observes it with GET.
            raise HTTPException(status_code=503, detail="Composer operation is changing; retry", headers={"Retry-After": "1"}) from None
        response.headers.update(version_header(2))
        return project_v2_snapshot(inputs, poll_after_ms=settings.composer_async_poll_after_ms)
    response.headers.update(version_header(1))
    return ComposerOperationAcceptedResponse(
        operation_id=row.operation_id, kind=row.kind, status=row.status, poll_after_ms=settings.composer_async_poll_after_ms
    )


@router.get("/{session_id}/operations", response_model=ComposerOperationFailurePageResponse)
async def list_composer_operation_failures(
    session_id: UUID,
    request: Request,
    response: Response,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    operation_id: UUID | None = None,
) -> ComposerOperationFailurePageResponse:
    try:
        await _verify_session_ownership(session_id, user, request)
    except HTTPException as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=exc.detail, headers={**(exc.headers or {}), "Cache-Control": "no-store"}
        ) from exc
    if operation_id is not None and (cursor is not None or limit is not None):
        raise HTTPException(status_code=422, detail="Exact operation lookup cannot be paginated", headers={"Cache-Control": "no-store"})
    try:
        position = decode_composer_failure_cursor(cursor) if cursor is not None else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid operation history cursor", headers={"Cache-Control": "no-store"}) from exc
    items, next_cursor = await run_sync_in_worker(
        _authority(request).list_failure_outcomes,
        session_id=session_id,
        actor_user_id=user.user_id,
        limit=limit if limit is not None else 50,
        cursor=position,
        operation_id=str(operation_id) if operation_id is not None else None,
    )
    response.headers["Cache-Control"] = "no-store"
    return ComposerOperationFailurePageResponse(
        session_id=session_id,
        items=list(items),
        next_cursor=encode_composer_failure_cursor(next_cursor) if next_cursor is not None else None,
    )


@router.get(
    "/{session_id}/operations/active",
    response_model=ComposerActiveOperationResponse,
    responses={204: {"description": "No live Composer operation in this session"}},
)
async def get_active_composer_operation(
    session_id: UUID,
    request: Request,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
) -> Response:
    """Declared before /{operation_id}, which would otherwise refuse ``active`` as a UUID."""
    try:
        await _verify_session_ownership(session_id, user, request)
    except HTTPException as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=exc.detail, headers={**(exc.headers or {}), "Cache-Control": "no-store"}
        ) from exc
    active = await run_sync_in_worker(
        read_active_operation, request.app.state.session_engine, session_id=session_id, actor_user_id=user.user_id
    )
    if active is None:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    return JSONResponse(content=active.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


@router.get("/{session_id}/operations/{operation_id}", response_model=ComposerOperationStatusResponse | ComposerOperationSnapshotV2)
async def get_composer_operation(
    session_id: UUID,
    operation_id: UUID,
    request: Request,
    response: Response,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
) -> ComposerOperationStatusResponse | ComposerOperationSnapshotV2:
    version = client_version(request)
    try:
        await _verify_session_ownership(session_id, user, request)
    except HTTPException as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers={**(exc.headers or {}), "Cache-Control": "no-store", **version_header(1)},
        ) from exc
    try:
        inputs = await run_sync_in_worker(
            read_v2_projection_inputs,
            _authority(request),
            request.app.state.session_engine,
            session_id=session_id,
            operation_id=str(operation_id),
        )
    except ComposerOperationProjectionUnstable:
        raise HTTPException(
            status_code=503,
            detail="Composer operation is changing; retry",
            headers={"Retry-After": "1", "Cache-Control": "no-store", **version_header(1)},
        ) from None
    row = inputs.record
    if row is None or row.actor_user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Operation not found", headers={"Cache-Control": "no-store", **version_header(1)})
    poll_after_ms = request.app.state.settings.composer_async_poll_after_ms
    if row.lifecycle_version == 1:
        response.headers["Cache-Control"] = "no-store"
        response.headers.update(version_header(1))
        return composer_operation_status_response(row, db_now=inputs.database_now, poll_after_ms=poll_after_ms)
    if not can_read(version, row.lifecycle_version):
        raise _version_required(row.operation_id)
    response.headers["Cache-Control"] = "no-store"
    response.headers.update(version_header(2))
    return project_v2_snapshot(inputs, poll_after_ms=poll_after_ms)


@router.post("/{session_id}/operations/{operation_id}/cancel", response_model=ComposerOperationStatusResponse | ComposerOperationSnapshotV2)
async def cancel_composer_operation(
    session_id: UUID,
    operation_id: UUID,
    request: Request,
    response: Response,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
) -> ComposerOperationStatusResponse | ComposerOperationSnapshotV2:
    version = client_version(request)
    try:
        await _verify_session_ownership(session_id, user, request)
    except HTTPException as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers={**(exc.headers or {}), "Cache-Control": "no-store", **version_header(1)},
        ) from exc
    row = await run_sync_in_worker(_authority(request).get, session_id=session_id, operation_id=str(operation_id))
    if row is None or row.actor_user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Operation not found", headers={"Cache-Control": "no-store", **version_header(1)})
    try:
        stopped = await run_sync_in_worker(
            _authority(request).request_owner_cancel,
            session_id=session_id,
            operation_id=str(operation_id),
            actor_user_id=user.user_id,
            auth_provider_type=request.app.state.settings.auth_provider,
            cancelled_failure=request_cancelled_error,
        )
    except ComposerOperationPreconditionRefused as exc:
        raise HTTPException(
            status_code=exc.error.http_status,
            detail=exc.error.body["detail"],
            headers={"Cache-Control": "no-store", **version_header(1)},
        ) from exc
    if stopped is None:
        raise HTTPException(status_code=404, detail="Operation not found", headers={"Cache-Control": "no-store", **version_header(1)})
    if row.lifecycle_version == 2 and version == 1:
        try:
            await _verify_session_ownership(session_id, user, request)
        except HTTPException as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.detail,
                headers={**(exc.headers or {}), "Cache-Control": "no-store", **version_header(1)},
            ) from exc
        current = await run_sync_in_worker(_authority(request).get, session_id=session_id, operation_id=str(operation_id))
        if current is None or current.actor_user_id != user.user_id:
            raise HTTPException(status_code=404, detail="Operation not found", headers={"Cache-Control": "no-store", **version_header(1)})
        result: ComposerOperationStatusResponse | ComposerOperationSnapshotV2 = _legacy_continuation_stop_response(
            current, poll_after_ms=request.app.state.settings.composer_async_poll_after_ms
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers.update(version_header(1))
    else:
        result = await get_composer_operation(session_id, operation_id, request, response, user)
    response.status_code = 202 if result.status in ("queued", "running") else 200
    if result.status in ("queued", "running"):
        request.app.state.composer_async_worker.signal_local_cancel(session_id=session_id, operation_id=str(operation_id))
        if row.lifecycle_version == 2:
            request.app.state.composer_async_worker.notify_work()
    return result


@router.get("/{session_id}/operations/{operation_id}/stream")
async def stream_operation(
    session_id: UUID, operation_id: UUID, request: Request, user: Annotated[UserIdentity, Depends(require_pipeline_user)]
) -> BoundedComposerStreamResponse:
    version = client_version(request)
    authority = _authority(request)
    # Chosen by the first (bounded) authorization, after session authorization,
    # so a lifecycle-dependent refusal never precedes the non-disclosing 404.
    selected_lifecycle: int | None = None
    resumed_from_revision: int | None = None
    auth = cast(ComposerStreamAuthServices, request.app.state.composer_stream_auth)
    token = request.headers["Authorization"].split(" ", 1)[1].strip()
    try:
        claims = auth.decode(token)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header", headers=version_header(1)) from exc
    observed_database_seconds = 0.0
    observed_monotonic = asyncio.get_running_loop().time()
    pending_progress_binding: tuple[str, int, str] | None = None
    initial_authorization = True

    async def authorize() -> bool:
        nonlocal observed_database_seconds, observed_monotonic, initial_authorization, selected_lifecycle, resumed_from_revision
        if not await auth.authorize(token=token, principal=user, session_id=session_id):
            return False
        job, database_now = await run_stream_read_in_worker(
            authority.get_with_database_now, session_id=session_id, operation_id=str(operation_id)
        )
        observed_database_seconds = database_now.timestamp()
        observed_monotonic = asyncio.get_running_loop().time()
        if job is None or job.actor_user_id != user.user_id:
            if initial_authorization:
                raise HTTPException(
                    status_code=404, detail="Operation not found", headers={"Cache-Control": "no-store", **version_header(1)}
                )
            return False
        if initial_authorization:
            if not can_read(version, job.lifecycle_version):
                raise _version_required(job.operation_id)
            if job.lifecycle_version == 2:
                try:
                    resumed_from_revision = parse_last_event_id(request.headers.getlist("Last-Event-ID"))
                except ComposerOperationResumeTokenInvalid as exc:
                    raise HTTPException(
                        status_code=422,
                        detail={"error_type": COMPOSER_OPERATION_RESUME_TOKEN_INVALID, "detail": str(exc)},
                        headers={"Cache-Control": "no-store", **version_header(1)},
                    ) from exc
            selected_lifecycle = job.lifecycle_version
            # Headers are emitted only after this first authorization returns.
            stream.raw_headers.append((COMPOSER_OPERATION_VERSION_HEADER.lower().encode("latin-1"), str(job.lifecycle_version).encode()))
        elif job.lifecycle_version != selected_lifecycle:
            raise AuditIntegrityError("A Composer operation changed lifecycle during observation")
        initial_authorization = False
        if pending_progress_binding is not None:
            fence_id, fence_epoch, request_token = pending_progress_binding
            if job.session_operation_id != fence_id or job.session_operation_epoch != fence_epoch:
                return False
            current_progress = await _get_composer_progress_registry(request).get_for_operation(
                session_id=str(session_id),
                user_id=user.user_id,
                operation_id=str(operation_id),
                session_operation_id=fence_id,
                session_operation_epoch=fence_epoch,
            )
            if current_progress is None or current_progress.request_token != request_token:
                return False
        return may_send()

    def may_send() -> bool:
        monotonic_elapsed = asyncio.get_running_loop().time() - observed_monotonic
        return max(time.time(), observed_database_seconds + monotonic_elapsed) < claims.expires_at

    permits = cast(ComposerStreamPermits, request.app.state.composer_stream_permits)
    try:
        permit = permits.acquire(str(operation_id), user.user_id)
    except ComposerStreamCapacityError as exc:
        raise HTTPException(
            status_code=503, detail="Composer stream capacity is currently occupied", headers={"Retry-After": "5", **version_header(1)}
        ) from exc

    async def v1_frames() -> AsyncIterator[bytes]:
        nonlocal pending_progress_binding
        sequence = 0
        last_heartbeat = asyncio.get_running_loop().time()
        while True:
            snapshot, now = await run_stream_read_in_worker(
                authority.get_with_database_now, session_id=session_id, operation_id=str(operation_id)
            )
            if snapshot is None or snapshot.actor_user_id != user.user_id:
                return
            pending_progress_binding = None
            if snapshot.status in ("completed", "failed"):
                yield encode_composer_operation_stream_frame(
                    ComposerOperationStreamTerminalFrame(
                        session_id=str(session_id),
                        operation_id=str(operation_id),
                        sequence=sequence,
                        payload=ComposerOperationStreamTerminalPayload(status=snapshot.status),
                    )
                )
                return
            yield encode_composer_operation_stream_frame(
                ComposerOperationStreamStatusFrame(
                    session_id=str(session_id),
                    operation_id=str(operation_id),
                    sequence=sequence,
                    payload=ComposerOperationStreamStatusPayload(
                        status="queued" if snapshot.status == "queued" else "running",
                        cancel_requested=snapshot.cancel_requested_at is not None,
                        deadline_remaining_ms=composer_operation_deadline_remaining_ms(deadline_at=snapshot.legacy_deadline_at, db_now=now),
                    ),
                )
            )
            sequence += 1
            if snapshot.session_operation_id is not None and snapshot.session_operation_epoch is not None:
                progress = await _get_composer_progress_registry(request).get_for_operation(
                    session_id=str(session_id),
                    user_id=user.user_id,
                    operation_id=str(operation_id),
                    session_operation_id=snapshot.session_operation_id,
                    session_operation_epoch=snapshot.session_operation_epoch,
                )
                if progress is not None:
                    if progress.request_token is None:
                        raise AuditIntegrityError("Operation progress has no request custody token")
                    pending_progress_binding = (snapshot.session_operation_id, snapshot.session_operation_epoch, progress.request_token)
                    yield encode_composer_operation_stream_frame(
                        ComposerOperationStreamProgressFrame(
                            session_id=str(session_id),
                            operation_id=str(operation_id),
                            sequence=sequence,
                            payload=ComposerOperationStreamProgressPayload(
                                session_operation_id=snapshot.session_operation_id,
                                session_operation_epoch=snapshot.session_operation_epoch,
                                request_token=progress.request_token,
                                request_id=progress.request_id,
                                phase=progress.phase,
                                headline=cast(str, scrub_credential_material(progress.headline)),
                                evidence=tuple(cast(str, scrub_credential_material(item)) for item in progress.evidence),
                                likely_next=cast(str, scrub_credential_material(progress.likely_next)),
                                reason=progress.reason,
                                updated_at=progress.updated_at,
                            ),
                        )
                    )
                    sequence += 1
            if asyncio.get_running_loop().time() - last_heartbeat >= 10:
                yield encode_composer_operation_stream_frame(
                    ComposerOperationStreamHeartbeatFrame(session_id=str(session_id), operation_id=str(operation_id), sequence=sequence)
                )
                sequence += 1
                last_heartbeat = asyncio.get_running_loop().time()
            await asyncio.sleep(1.0)

    draining = cast(threading.Event, request.app.state.instance_draining)
    v2_sequence = 0
    engine = request.app.state.session_engine
    poll_after_ms = request.app.state.settings.composer_async_poll_after_ms

    def v2_frame(frame: ComposerOperationStreamFrameV2, *, event_id: int | None = None) -> bytes:
        nonlocal v2_sequence
        encoded = encode_composer_operation_stream_frame_v2(frame, event_id=event_id)
        v2_sequence += 1
        return encoded

    def reconnect_frame() -> bytes:
        return v2_frame(
            ComposerOperationStreamReconnectFrameV2(session_id=str(session_id), operation_id=str(operation_id), sequence=v2_sequence)
        )

    async def v2_frames() -> AsyncIterator[bytes]:
        """State-transfer stream: status on connect and on each revision change.

        Every poll yields exactly one item. An empty tick tells the bounded
        response that a read completed with nothing to send.
        """
        nonlocal pending_progress_binding
        loop = asyncio.get_running_loop()
        last_revision: int | None = None
        last_progress: tuple[str, str] | None = None
        last_sent = loop.time()
        structlog.get_logger(__name__).info("composer_stream.opened", protocol_version=2, resumed_from_revision=resumed_from_revision)
        while True:
            try:
                inputs = await run_stream_read_in_worker(
                    read_v2_projection_inputs, authority, engine, session_id=session_id, operation_id=str(operation_id)
                )
            except ComposerOperationProjectionUnstable:
                yield b""
                await asyncio.sleep(1.0)
                continue
            record = inputs.record
            if record is None or record.actor_user_id != user.user_id:
                return
            if record.lifecycle_version != 2:
                raise AuditIntegrityError("A lifecycle-2 stream observed a lifecycle-1 record")
            pending_progress_binding = None
            sent = False
            if record.status_revision != last_revision:
                last_revision = record.status_revision
                sent = True
                yield v2_frame(
                    ComposerOperationStreamStatusFrameV2(
                        session_id=str(session_id),
                        operation_id=str(operation_id),
                        sequence=v2_sequence,
                        payload=project_v2_status(inputs, poll_after_ms=poll_after_ms),
                    ),
                    event_id=record.status_revision,
                )
            if record.status in ("completed", "failed"):
                yield v2_frame(
                    ComposerOperationStreamTerminalFrameV2(
                        session_id=str(session_id),
                        operation_id=str(operation_id),
                        sequence=v2_sequence,
                        payload=ComposerOperationTerminalV2(status=record.status, status_revision=record.status_revision),
                    ),
                    event_id=record.status_revision,
                )
                return
            if inputs.segment is not None and record.session_operation_id is not None and record.session_operation_epoch is not None:
                progress = await _get_composer_progress_registry(request).get_for_operation(
                    session_id=str(session_id),
                    user_id=user.user_id,
                    operation_id=str(operation_id),
                    session_operation_id=record.session_operation_id,
                    session_operation_epoch=record.session_operation_epoch,
                )
                if progress is not None:
                    if progress.request_token is None:
                        raise AuditIntegrityError("Operation progress has no request custody token")
                    key = (progress.request_token, progress.updated_at.isoformat())
                    if key != last_progress:
                        last_progress = key
                        sent = True
                        pending_progress_binding = (record.session_operation_id, record.session_operation_epoch, progress.request_token)
                        yield v2_frame(
                            ComposerOperationStreamProgressFrameV2(
                                session_id=str(session_id),
                                operation_id=str(operation_id),
                                sequence=v2_sequence,
                                payload=ComposerOperationProgressV2(
                                    request_id=progress.request_id,
                                    phase=progress.phase,
                                    headline=cast(str, scrub_credential_material(progress.headline)),
                                    evidence=tuple(cast(str, scrub_credential_material(item)) for item in progress.evidence),
                                    likely_next=cast(str, scrub_credential_material(progress.likely_next)),
                                    reason=progress.reason,
                                    updated_at=progress.updated_at,
                                    segment_sequence=inputs.segment.sequence,
                                    segment_generation=inputs.segment.generation,
                                ),
                            )
                        )
            if sent:
                last_sent = loop.time()
            elif loop.time() - last_sent >= 10:
                last_sent = loop.time()
                yield v2_frame(
                    ComposerOperationStreamHeartbeatFrameV2(
                        session_id=str(session_id), operation_id=str(operation_id), sequence=v2_sequence
                    )
                )
            else:
                yield b""
            await asyncio.sleep(1.0)

    async def frames() -> AsyncIterator[bytes]:
        if selected_lifecycle == 2:
            async for frame in v2_frames():
                yield frame
        else:
            async for frame in v1_frames():
                yield frame

    def lifetime_frame() -> bytes | None:
        return reconnect_frame() if selected_lifecycle == 2 else None

    stream = BoundedComposerStreamResponse(
        frames=frames(),
        authorize=authorize,
        permits=permits,
        permit=permit,
        may_send=may_send,
        lifetime_frame=lifetime_frame,
        draining=draining,
    )
    return stream
