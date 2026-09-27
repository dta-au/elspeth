"""Tutorial run endpoint."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer.tutorial_models import (
    TutorialCancelRequest,
    TutorialCancelResponse,
    TutorialOrphanCleanupResponse,
    TutorialReadinessResponse,
    TutorialRunRequest,
    TutorialRunResponse,
    TutorialSampleResponse,
)
from elspeth.web.composer.tutorial_sample import resolve_tutorial_sample_urls, tutorial_sample_base_url
from elspeth.web.composer.tutorial_service import (
    cancel_tutorial_run,
    cleanup_tutorial_orphans,
    get_tutorial_readiness,
    run_tutorial_pipeline,
)
from elspeth.web.middleware.rate_limit import WebRateLimiter, get_rate_limiter
from elspeth.web.sessions.ownership import verify_session_ownership


def create_tutorial_run_router() -> APIRouter:
    """Create the tutorial API router."""
    router = APIRouter(prefix="/api/tutorial", tags=["tutorial"])

    @router.get("/{session_id}/sample", response_model=TutorialSampleResponse)
    async def get_tutorial_sample(
        session_id: UUID,
        request: Request,
        user: Annotated[UserIdentity, Depends(get_current_user)],
    ) -> TutorialSampleResponse:
        """Return only the public sample addresses for an owned session."""
        await verify_session_ownership(session_id, user, request)
        base_url = tutorial_sample_base_url(settings=request.app.state.settings)
        return TutorialSampleResponse(sample_urls=resolve_tutorial_sample_urls(base_url=base_url))

    @router.get("/{session_id}/readiness", response_model=TutorialReadinessResponse)
    async def get_readiness(
        session_id: UUID,
        request: Request,
        user: Annotated[UserIdentity, Depends(get_current_user)],
    ) -> TutorialReadinessResponse:
        state_id = await get_tutorial_readiness(request=request, user=user, session_id=session_id)
        return TutorialReadinessResponse(state_id=state_id)

    @router.post("/run", response_model=TutorialRunResponse)
    async def run_tutorial(
        body: TutorialRunRequest,
        request: Request,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
        rate_limiter: WebRateLimiter = Depends(get_rate_limiter),  # noqa: B008
    ) -> TutorialRunResponse:
        await rate_limiter.check(user.user_id)
        return await run_tutorial_pipeline(
            request=request,
            user=user,
            session_id=str(body.session_id),
        )

    @router.post("/cancel", response_model=TutorialCancelResponse)
    async def cancel_tutorial(
        body: TutorialCancelRequest,
        request: Request,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> TutorialCancelResponse:
        return await cancel_tutorial_run(
            request=request,
            user=user,
            session_id=str(body.session_id),
        )

    @router.delete("/orphans", response_model=TutorialOrphanCleanupResponse)
    async def delete_tutorial_orphans(
        request: Request,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> TutorialOrphanCleanupResponse:
        return await cleanup_tutorial_orphans(request=request, user=user)

    return router
