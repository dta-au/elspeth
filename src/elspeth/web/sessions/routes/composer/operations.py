"""Durable admission and observation for detached Composer turns."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator
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
from elspeth.web.sessions.composer_operation_projection import (
    ComposerOperationProjectionUnstable,
    project_operation_snapshot,
    project_operation_status,
    read_active_operation,
    read_projection_inputs,
)
from elspeth.web.sessions.composer_operation_resume import (
    COMPOSER_OPERATION_RESUME_TOKEN_INVALID,
    ComposerOperationResumeTokenInvalid,
    parse_last_event_id,
)
from elspeth.web.sessions.composer_operations import (
    ComposerOperationActiveError,
    ComposerOperationCapacityError,
    ComposerOperationConflictError,
    ComposerOperationKind,
    ComposerOperationPreconditionRefused,
    composer_operation_admission_request_hash,
    decode_composer_failure_cursor,
    encode_composer_failure_cursor,
)
from elspeth.web.sessions.schemas import (
    ComposerActiveOperationResponse,
    ComposerOperationFailurePageResponse,
    ComposerOperationProgress,
    ComposerOperationSnapshot,
    ComposerOperationStreamFrame,
    ComposerOperationStreamHeartbeatFrame,
    ComposerOperationStreamProgressFrame,
    ComposerOperationStreamReconnectFrame,
    ComposerOperationStreamStatusFrame,
    ComposerOperationStreamTerminalFrame,
    ComposerOperationTerminal,
    RecomposeRequest,
    SendMessageRequest,
    encode_composer_operation_stream_frame,
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
) -> ComposerOperationSnapshot:
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
            request_hash=composer_operation_admission_request_hash(session_id=session_id, kind=kind, request=body),
            actor_user_id=user.user_id,
            request_id=request.state.request_id,
            base_state_id=body.state_id,
            request_json=body.model_dump_json(),
            max_nonterminal=settings.composer_async_max_queued_operations,
            auth_provider_type=settings.auth_provider,
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
    try:
        inputs = await run_sync_in_worker(
            read_projection_inputs,
            _authority(request),
            request.app.state.session_engine,
            session_id=session_id,
            operation_id=row.operation_id,
        )
    except ComposerOperationProjectionUnstable:
        # Admission committed; the client observes it with GET.
        raise HTTPException(status_code=503, detail="Composer operation is changing; retry", headers={"Retry-After": "1"}) from None
    return project_operation_snapshot(inputs, poll_after_ms=settings.composer_async_poll_after_ms)


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


@router.get("/{session_id}/operations/{operation_id}", response_model=ComposerOperationSnapshot)
async def get_composer_operation(
    session_id: UUID,
    operation_id: UUID,
    request: Request,
    response: Response,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
) -> ComposerOperationSnapshot:
    try:
        await _verify_session_ownership(session_id, user, request)
    except HTTPException as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=exc.detail, headers={**(exc.headers or {}), "Cache-Control": "no-store"}
        ) from exc
    try:
        inputs = await run_sync_in_worker(
            read_projection_inputs,
            _authority(request),
            request.app.state.session_engine,
            session_id=session_id,
            operation_id=str(operation_id),
        )
    except ComposerOperationProjectionUnstable:
        raise HTTPException(
            status_code=503,
            detail="Composer operation is changing; retry",
            headers={"Retry-After": "1", "Cache-Control": "no-store"},
        ) from None
    row = inputs.record
    if row is None or row.actor_user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Operation not found", headers={"Cache-Control": "no-store"})
    response.headers["Cache-Control"] = "no-store"
    return project_operation_snapshot(inputs, poll_after_ms=request.app.state.settings.composer_async_poll_after_ms)


@router.post("/{session_id}/operations/{operation_id}/cancel", response_model=ComposerOperationSnapshot)
async def cancel_composer_operation(
    session_id: UUID,
    operation_id: UUID,
    request: Request,
    response: Response,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
) -> ComposerOperationSnapshot:
    try:
        await _verify_session_ownership(session_id, user, request)
    except HTTPException as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=exc.detail, headers={**(exc.headers or {}), "Cache-Control": "no-store"}
        ) from exc
    row = await run_sync_in_worker(_authority(request).get, session_id=session_id, operation_id=str(operation_id))
    if row is None or row.actor_user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Operation not found", headers={"Cache-Control": "no-store"})
    try:
        stopped = await run_sync_in_worker(
            _authority(request).request_owner_cancel,
            session_id=session_id,
            operation_id=str(operation_id),
            actor_user_id=user.user_id,
            auth_provider_type=request.app.state.settings.auth_provider,
        )
    except ComposerOperationPreconditionRefused as exc:
        raise HTTPException(
            status_code=exc.error.http_status, detail=exc.error.body["detail"], headers={"Cache-Control": "no-store"}
        ) from exc
    if stopped is None:
        raise HTTPException(status_code=404, detail="Operation not found", headers={"Cache-Control": "no-store"})
    result = await get_composer_operation(session_id, operation_id, request, response, user)
    response.status_code = 202 if result.status in ("queued", "running") else 200
    if result.status in ("queued", "running"):
        request.app.state.composer_async_worker.signal_local_cancel(session_id=session_id, operation_id=str(operation_id))
        request.app.state.composer_async_worker.notify_work()
    return result


@router.get("/{session_id}/operations/{operation_id}/stream")
async def stream_operation(
    session_id: UUID, operation_id: UUID, request: Request, user: Annotated[UserIdentity, Depends(require_pipeline_user)]
) -> BoundedComposerStreamResponse:
    authority = _authority(request)
    resumed_from_revision: int | None = None
    auth = cast(ComposerStreamAuthServices, request.app.state.composer_stream_auth)
    token = request.headers["Authorization"].split(" ", 1)[1].strip()
    try:
        claims = auth.decode(token)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header") from exc
    observed_database_seconds = 0.0
    observed_monotonic = asyncio.get_running_loop().time()
    pending_progress_binding: tuple[str, int, str] | None = None
    initial_authorization = True

    async def authorize() -> bool:
        nonlocal observed_database_seconds, observed_monotonic, initial_authorization, resumed_from_revision
        if not await auth.authorize(token=token, principal=user, session_id=session_id):
            return False
        job, database_now = await run_stream_read_in_worker(
            authority.get_with_database_now, session_id=session_id, operation_id=str(operation_id)
        )
        observed_database_seconds = database_now.timestamp()
        observed_monotonic = asyncio.get_running_loop().time()
        if job is None or job.actor_user_id != user.user_id:
            if initial_authorization:
                raise HTTPException(status_code=404, detail="Operation not found", headers={"Cache-Control": "no-store"})
            return False
        if initial_authorization:
            try:
                resumed_from_revision = parse_last_event_id(request.headers.getlist("Last-Event-ID"))
            except ComposerOperationResumeTokenInvalid as exc:
                raise HTTPException(
                    status_code=422,
                    detail={"error_type": COMPOSER_OPERATION_RESUME_TOKEN_INVALID, "detail": str(exc)},
                    headers={"Cache-Control": "no-store"},
                ) from exc
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
        raise HTTPException(status_code=503, detail="Composer stream capacity is currently occupied", headers={"Retry-After": "5"}) from exc

    draining = cast(threading.Event, request.app.state.instance_draining)
    v2_sequence = 0
    engine = request.app.state.session_engine
    poll_after_ms = request.app.state.settings.composer_async_poll_after_ms

    def v2_frame(frame: ComposerOperationStreamFrame, *, event_id: int | None = None) -> bytes:
        nonlocal v2_sequence
        encoded = encode_composer_operation_stream_frame(frame, event_id=event_id)
        v2_sequence += 1
        return encoded

    def reconnect_frame() -> bytes:
        return v2_frame(
            ComposerOperationStreamReconnectFrame(session_id=str(session_id), operation_id=str(operation_id), sequence=v2_sequence)
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
        structlog.get_logger(__name__).info("composer_stream.opened", resumed_from_revision=resumed_from_revision)
        while True:
            try:
                inputs = await run_stream_read_in_worker(
                    read_projection_inputs, authority, engine, session_id=session_id, operation_id=str(operation_id)
                )
            except ComposerOperationProjectionUnstable:
                yield b""
                await asyncio.sleep(1.0)
                continue
            record = inputs.record
            if record is None or record.actor_user_id != user.user_id:
                return
            pending_progress_binding = None
            sent = False
            if record.status_revision != last_revision:
                last_revision = record.status_revision
                sent = True
                yield v2_frame(
                    ComposerOperationStreamStatusFrame(
                        session_id=str(session_id),
                        operation_id=str(operation_id),
                        sequence=v2_sequence,
                        payload=project_operation_status(inputs, poll_after_ms=poll_after_ms),
                    ),
                    event_id=record.status_revision,
                )
            if record.status in ("completed", "failed"):
                yield v2_frame(
                    ComposerOperationStreamTerminalFrame(
                        session_id=str(session_id),
                        operation_id=str(operation_id),
                        sequence=v2_sequence,
                        payload=ComposerOperationTerminal(status=record.status, status_revision=record.status_revision),
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
                            ComposerOperationStreamProgressFrame(
                                session_id=str(session_id),
                                operation_id=str(operation_id),
                                sequence=v2_sequence,
                                payload=ComposerOperationProgress(
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
                    ComposerOperationStreamHeartbeatFrame(session_id=str(session_id), operation_id=str(operation_id), sequence=v2_sequence)
                )
            else:
                yield b""
            await asyncio.sleep(1.0)

    def lifetime_frame() -> bytes | None:
        return reconnect_frame()

    return BoundedComposerStreamResponse(
        frames=v2_frames(),
        authorize=authorize,
        permits=permits,
        permit=permit,
        may_send=may_send,
        lifetime_frame=lifetime_frame,
        draining=draining,
    )
