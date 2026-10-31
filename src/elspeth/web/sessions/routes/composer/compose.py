from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from elspeth.web.sessions.schemas import ComposerOperationAcceptedResponse, ComposerOperationSnapshotV2

from .._helpers import UserIdentity, WebRateLimiter, get_rate_limiter, require_pipeline_user
from .operations import admit, client_version, parse_recompose_body, with_version

router = APIRouter()


@router.post("/{session_id}/recompose", response_model=ComposerOperationAcceptedResponse | ComposerOperationSnapshotV2, status_code=202)
async def recompose(
    session_id: UUID,
    request: Request,
    response: Response,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
    rate_limiter: Annotated[WebRateLimiter, Depends(get_rate_limiter)],
) -> ComposerOperationAcceptedResponse | ComposerOperationSnapshotV2:
    try:
        version = client_version(request)
        body = await parse_recompose_body(request, session_id, user)
        return await admit(request, response, session_id, body, user, rate_limiter, "compose_recompose", version)
    except HTTPException as exc:
        # Refusals before admission project the v1 error body.
        raise with_version(exc, 1) from exc
