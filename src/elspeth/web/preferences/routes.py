"""FastAPI router for composer-preferences endpoints.

Two endpoints under ``/api/composer-preferences``:
  - ``GET`` — returns the authenticated user's preferences.
  - ``PATCH`` — partial update; missing fields are preserved. Empty
    payload is a no-op success.

Both endpoints require auth (the standard ``get_current_user``
dependency). Cross-user isolation is via ``user.user_id`` scoping at the
service layer.
"""

from fastapi import APIRouter, Depends, HTTPException, Request

from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.middleware.rate_limit import (
    WebRateLimiter,
    get_write_rate_limiter,
)
from elspeth.web.preferences.models import (
    ComposerPreferences,
    UpdateComposerPreferencesRequest,
)
from elspeth.web.preferences.service import PreferencesService, TutorialProgressConflict


def create_preferences_router() -> APIRouter:
    router = APIRouter(prefix="/api/composer-preferences", tags=["preferences"])

    @router.get("", response_model=ComposerPreferences)
    async def get_preferences(
        request: Request,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> ComposerPreferences:
        service: PreferencesService = request.app.state.preferences_service
        return await service.get_composer_preferences(user.user_id)

    @router.patch("", response_model=ComposerPreferences)
    async def update_preferences(
        body: UpdateComposerPreferencesRequest,
        request: Request,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
        rate_limiter: WebRateLimiter = Depends(get_write_rate_limiter),  # noqa: B008
    ) -> ComposerPreferences:
        # Panel C1: per-user rate limit — metered by the WRITE bucket
        # (app.state.write_rate_limiter), not the composer LLM bucket.
        # This PATCH is a cheap DB upsert, and the tutorial legitimately
        # fires resume-state persists in bursts; sharing the LLM-sized
        # bucket let those bursts 429 the tutorial-completion save.
        # Read GET is intentionally unguarded — idempotent, safe to
        # spam, no write amplification.
        await rate_limiter.check(user.user_id)
        service: PreferencesService = request.app.state.preferences_service
        try:
            transition = await service.update_composer_preferences(user.user_id, body)
        except TutorialProgressConflict as exc:
            raise HTTPException(status_code=409, detail={"code": "tutorial_progress_conflict", "message": str(exc)}) from exc
        return transition.current

    return router
