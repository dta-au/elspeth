"""Durable admission and observation for detached Composer turns."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from elspeth.contracts.credential_material import scrub_credential_material
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_stream_read_in_worker, run_sync_in_worker
from elspeth.web.auth.models import AuthenticationError
from elspeth.web.compartments import compartment_ingress_record
from elspeth.web.composer_stream import BoundedComposerStreamResponse, ComposerStreamCapacityError, ComposerStreamPermits
from elspeth.web.composer_stream_auth import ComposerStreamAuthServices
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority, admit_composer_operation
from elspeth.web.credential_guard import CredentialMaterialRefused, require_no_credential_material
from elspeth.web.sessions.composer_operation_errors import request_cancelled_error
from elspeth.web.sessions.composer_operations import (
    ComposerOperationActiveError,
    ComposerOperationCapacityError,
    ComposerOperationConflictError,
    ComposerOperationError,
    ComposerOperationKind,
    ComposerOperationPreconditionRefused,
    ComposerOperationRecord,
    composer_operation_request_hash,
)
from elspeth.web.sessions.schemas import (
    ComposerOperationAcceptedResponse,
    ComposerOperationStatusResponse,
    ComposerOperationStreamHeartbeatFrame,
    ComposerOperationStreamProgressFrame,
    ComposerOperationStreamProgressPayload,
    ComposerOperationStreamStatusFrame,
    ComposerOperationStreamStatusPayload,
    ComposerOperationStreamTerminalFrame,
    ComposerOperationStreamTerminalPayload,
    MessageWithStateResponse,
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


def composer_operation_deadline_remaining_ms(*, deadline_at: datetime, db_now: datetime) -> int:
    if deadline_at.utcoffset() is None or db_now.utcoffset() is None:
        raise ValueError("Composer operation times must be timezone-aware")
    return max(0, (deadline_at - db_now) // timedelta(milliseconds=1))


def composer_operation_status_response(
    row: ComposerOperationRecord, *, db_now: datetime, poll_after_ms: int
) -> ComposerOperationStatusResponse:
    remaining_ms = composer_operation_deadline_remaining_ms(deadline_at=row.deadline_at, db_now=db_now)
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
        deadline_at=row.deadline_at,
        deadline_remaining_ms=remaining_ms if row.status in ("queued", "running") else 0,
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
    session_id: UUID,
    body: SendMessageRequest | RecomposeRequest,
    user: UserIdentity,
    rate_limiter: WebRateLimiter,
    kind: ComposerOperationKind,
) -> ComposerOperationAcceptedResponse:
    await _verify_session_ownership(session_id, user, request)
    settings = request.app.state.settings
    if isinstance(body, SendMessageRequest):
        try:
            require_no_credential_material(body.content, surface="web_message_ingress")
        except CredentialMaterialRefused as exc:
            raise HTTPException(status_code=422, detail=exc.to_payload()) from exc
        compartment_ingress_record(body.content, own_compartment_id=settings.compartment_id)

    async def rate_limit() -> None:
        await rate_limiter.check(user.user_id)

    try:
        row, _fresh = await admit_composer_operation(
            _authority(request),
            rate_limit=rate_limit,
            session_id=session_id,
            operation_id=body.operation_id,
            kind=kind,
            request_hash=composer_operation_request_hash(session_id=session_id, kind=kind, request=body),
            actor_user_id=user.user_id,
            request_id=request.state.request_id,
            base_state_id=body.state_id,
            request_json=body.model_dump_json(),
            deadline_seconds=settings.composer_timeout_seconds,
            max_nonterminal=settings.composer_async_max_queued_operations,
            auth_provider_type=settings.auth_provider,
        )
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
    return ComposerOperationAcceptedResponse(
        operation_id=row.operation_id, kind=row.kind, status=row.status, poll_after_ms=settings.composer_async_poll_after_ms
    )


@router.get("/{session_id}/operations/{operation_id}", response_model=ComposerOperationStatusResponse)
async def get_composer_operation(
    session_id: UUID,
    operation_id: UUID,
    request: Request,
    response: Response,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
) -> ComposerOperationStatusResponse:
    try:
        await _verify_session_ownership(session_id, user, request)
    except HTTPException as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=exc.detail, headers={**(exc.headers or {}), "Cache-Control": "no-store"}
        ) from exc
    row, now = await run_sync_in_worker(_authority(request).get_with_database_now, session_id=session_id, operation_id=str(operation_id))
    if row is None or row.actor_user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Operation not found", headers={"Cache-Control": "no-store"})
    response.headers["Cache-Control"] = "no-store"
    return composer_operation_status_response(row, db_now=now, poll_after_ms=request.app.state.settings.composer_async_poll_after_ms)


@router.post("/{session_id}/operations/{operation_id}/cancel", response_model=ComposerOperationStatusResponse)
async def cancel_composer_operation(
    session_id: UUID,
    operation_id: UUID,
    request: Request,
    response: Response,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
) -> ComposerOperationStatusResponse:
    try:
        await _verify_session_ownership(session_id, user, request)
    except HTTPException as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=exc.detail, headers={**(exc.headers or {}), "Cache-Control": "no-store"}
        ) from exc
    row = await run_sync_in_worker(_authority(request).get, session_id=session_id, operation_id=str(operation_id))
    if row is None or row.actor_user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Operation not found", headers={"Cache-Control": "no-store"})
    await run_sync_in_worker(
        _authority(request).request_cancel, session_id=session_id, operation_id=str(operation_id), cancelled_failure=request_cancelled_error
    )
    result = await get_composer_operation(session_id, operation_id, request, response, user)
    response.status_code = 202 if result.status in ("queued", "running") else 200
    if result.status in ("queued", "running"):
        request.app.state.composer_async_worker.signal_local_cancel(session_id=session_id, operation_id=str(operation_id))
    return result


@router.get("/{session_id}/operations/{operation_id}/stream")
async def stream_operation(
    session_id: UUID, operation_id: UUID, request: Request, user: Annotated[UserIdentity, Depends(require_pipeline_user)]
) -> BoundedComposerStreamResponse:
    authority = _authority(request)
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
        nonlocal observed_database_seconds, observed_monotonic, initial_authorization
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

    async def frames() -> AsyncIterator[bytes]:
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
                        deadline_remaining_ms=composer_operation_deadline_remaining_ms(deadline_at=snapshot.deadline_at, db_now=now),
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

    return BoundedComposerStreamResponse(frames=frames(), authorize=authorize, permits=permits, permit=permit, may_send=may_send)
