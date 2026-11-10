"""FastAPI router for the shareable-reviews endpoints.

Phase 6A Task 6 (UX redesign 2026-05). Five routes:

* ``POST /api/sessions/{session_id}/mark-ready-for-review``
* ``GET  /api/sessions/{session_id}/shareable-link``
* ``GET  /api/sessions/{session_id}/shareable-files``
* ``GET  /api/sessions/shared/{token}``
* ``POST /api/sessions/shared/{token}/import``

The first three require ``verify_session_ownership`` — same byte-identical
404 contract used by every other session-scoped route. The last two are the
recipient's: the inspect view and the import. They require authentication
(the token is a capability, NOT an authenticator — see plan §"Capability vs
authenticator"), but do NOT call ownership verification because the
recipient is deliberately a different user from the session owner.
Token-verification failures map to 401; expired payload-store blobs map to
404.

Layer: L3 (application). Reads ``app.state.shareable_review_service``
which is wired in ``web/app.py``.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from elspeth.contracts.payload_store import PayloadNotFoundError
from elspeth.web.auth.middleware import get_current_user, require_pipeline_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.coordination.contracts import SessionOperationKind
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.middleware.rate_limit import (
    WebRateLimiter,
    get_rate_limiter,
    get_write_rate_limiter,
)
from elspeth.web.sessions.ownership import verify_session_ownership
from elspeth.web.sessions.protocol import SessionServiceProtocol
from elspeth.web.shareable_reviews.importer import ImportNotAllowedError, import_shared_snapshot
from elspeth.web.shareable_reviews.models import (
    MarkReadyForReviewRequest,
    MarkReadyForReviewResponse,
    ShareableFilesResponse,
    ShareableLinkResponse,
    SharedImportResponse,
    SharedInspectResponse,
)
from elspeth.web.shareable_reviews.service import (
    CompositionNotRunnableError,
    ShareableReviewService,
)
from elspeth.web.shareable_reviews.signer import InvalidToken

_NO_STORE = "no-store"


def create_shareable_reviews_router() -> APIRouter:
    """Create the shareable-reviews router.

    Five endpoints; see module docstring for the access-control matrix.
    """
    router = APIRouter(tags=["shareable-reviews"])

    @router.post(
        "/api/sessions/{session_id}/mark-ready-for-review",
        response_model=MarkReadyForReviewResponse,
    )
    async def mark_ready_for_review(
        session_id: UUID,
        request: Request,
        user: Annotated[UserIdentity, Depends(require_pipeline_user)],
        # Write bucket: cheap DB write / token mint, not an LLM call.
        rate_limiter: WebRateLimiter = Depends(get_write_rate_limiter),  # noqa: B008
        # Optional: an absent body is a view-only link at the deployment's
        # maximum lifetime (the behaviour before the body existed).
        body: MarkReadyForReviewRequest | None = Body(default=None),  # noqa: B008
    ) -> JSONResponse:
        """Mint a signed share artifact for the current composition state.

        Returns 409 with a generic detail if the composition fails either
        gate (validation or readiness-error), or — when import is allowed —
        if a file it uses cannot be shared (the detail names the file) or
        the files differ from the ones the owner previewed
        (``error_type: share_files_changed``).
        Returns 422 for a lifetime outside the offered set or above the
        deployment maximum. Returns 404 (byte-identical to "session does
        not exist") if the session belongs to a different user (IDOR).
        Returns 401 if the request is unauthenticated.
        """
        options = body if body is not None else MarkReadyForReviewRequest()
        await rate_limiter.check(user.user_id)
        await verify_session_ownership(session_id, user, request)
        service: ShareableReviewService = request.app.state.shareable_review_service
        session_service: SessionServiceProtocol = request.app.state.session_service
        # Marking ready writes a composer-completion event, so this is a
        # writer: it takes COMPOSE authority and answers 409 while another
        # compose is live (elspeth-bf52d495a2, option A).
        lease = await SessionOperationLease.acquire(
            session_service.session_operation_authority,
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=session_service.session_operation_owner_instance_id,
            lease_seconds=session_service.session_operation_lease_seconds,
        )
        try:
            try:
                result = await service.mark_ready_for_review(
                    session_id=session_id,
                    user_id=user.user_id,
                    # The identity id is what everything downstream authorises
                    # and signs on; the username rides alongside purely so the
                    # shared view can name the sharer to a recipient who cannot
                    # resolve an opaque id.
                    username=user.username,
                    session_operation_context=lease.context,
                    allow_import=options.allow_import,
                    lifetime_seconds=options.lifetime_seconds,
                    confirmed_file_manifest=options.file_manifest_fingerprint,
                )
            except CompositionNotRunnableError as exc:
                if exc.reason == "lifetime_exceeds_maximum":
                    # A request the owner can fix by choosing a shorter
                    # lifetime, not a composition-state conflict.
                    raise HTTPException(status_code=422, detail=exc.detail or exc.reason) from exc
                if exc.reason == "share_files_changed":
                    # Typed so the dialog reloads the file list and asks the
                    # owner to confirm again, instead of offering a retry
                    # that would present the same stale fingerprint.
                    raise HTTPException(status_code=409, detail={"error_type": exc.reason, "detail": exc.detail}) from exc
                # ``from exc``: preserves the server-side __context__ chain for
                # logs. Wire-facing ``detail`` is unaffected — only the internal
                # traceback chain. The probing-attacker rationale that justifies
                # ``from None`` on InvalidToken (below) does NOT apply here: a
                # 409 leaks no signal an authenticated session owner could not
                # already obtain by inspecting their own session.
                raise HTTPException(status_code=409, detail=exc.detail or exc.reason) from exc
            return JSONResponse(
                content=result.model_dump(mode="json"),
                headers={"Cache-Control": _NO_STORE},
            )
        finally:
            await lease.close()

    @router.get(
        "/api/sessions/{session_id}/shareable-link",
        response_model=ShareableLinkResponse,
    )
    async def get_shareable_link(
        session_id: UUID,
        request: Request,
        user: Annotated[UserIdentity, Depends(require_pipeline_user)],
        # Write bucket: cheap DB write / token mint, not an LLM call.
        rate_limiter: WebRateLimiter = Depends(get_write_rate_limiter),  # noqa: B008
    ) -> JSONResponse:
        """Re-mint a fresh token for the current (session, state).

        Content-addressing guarantees identical ``payload_digest`` across
        successive calls on an unchanged composition; only the token
        string differs (different nonce each call).
        """
        await rate_limiter.check(user.user_id)
        await verify_session_ownership(session_id, user, request)
        service: ShareableReviewService = request.app.state.shareable_review_service
        try:
            result = await service.get_shareable_link(session_id=session_id, user_id=user.user_id)
        except CompositionNotRunnableError as exc:
            raise HTTPException(status_code=409, detail=exc.detail or exc.reason) from exc
        return JSONResponse(
            content=result.model_dump(mode="json"),
            headers={"Cache-Control": _NO_STORE},
        )

    @router.get(
        "/api/sessions/{session_id}/shareable-files",
        response_model=ShareableFilesResponse,
    )
    async def get_shareable_files(
        session_id: UUID,
        request: Request,
        user: Annotated[UserIdentity, Depends(require_pipeline_user)],
        # Write bucket: a cheap DB read for the owner's share dialog, not an LLM call.
        rate_limiter: WebRateLimiter = Depends(get_write_rate_limiter),  # noqa: B008
    ) -> JSONResponse:
        """List the files an importable share of the current state would carry.

        Owner-only (404 for anyone else). Reads blob rows (never file
        content) and writes nothing, so it takes BLOB_READ authority rather
        than a COMPOSE lease and never conflicts with a live compose. A graph
        that cannot be shared for import answers 200 with
        ``importable: false`` and the reason. ``max_lifetime_seconds`` is the
        longest link lifetime the share accepts on this deployment.
        """
        await rate_limiter.check(user.user_id)
        await verify_session_ownership(session_id, user, request)
        service: ShareableReviewService = request.app.state.shareable_review_service
        session_service: SessionServiceProtocol = request.app.state.session_service
        lease = await SessionOperationLease.acquire(
            session_service.session_operation_authority,
            session_id=session_id,
            operation_kind=SessionOperationKind.BLOB_READ,
            owner_instance_id=session_service.session_operation_owner_instance_id,
            lease_seconds=session_service.session_operation_lease_seconds,
        )
        try:
            result = await service.preview_shareable_files(session_id=session_id, session_operation_context=lease.context)
        finally:
            await lease.close()
        return JSONResponse(
            content=result.model_dump(mode="json"),
            headers={"Cache-Control": _NO_STORE},
        )

    @router.get(
        "/api/sessions/shared/{token}",
        response_model=SharedInspectResponse,
    )
    async def get_shared_inspect(
        token: str,
        request: Request,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
        # Strict bucket: abuse-sensitive token probe stays strict.
        rate_limiter: WebRateLimiter = Depends(get_rate_limiter),  # noqa: B008
    ) -> JSONResponse:
        """Read-only inspect view of a shared composition.

        The token is a CAPABILITY, not an authenticator — the recipient
        must still authenticate (``Depends(get_current_user)``). Without
        the authentication dependency, anyone with the URL gets in; that
        is NOT the designed behaviour. Do NOT "simplify" this dep away.

        ``InvalidToken`` (tampered/expired/malformed) → 401.
        ``PayloadNotFoundError`` (token verifies but the payload-store
        blob has been reaped under retention) → 404 with a message
        directing the recipient to request a fresh link.
        """
        await rate_limiter.check(user.user_id)
        service: ShareableReviewService = request.app.state.shareable_review_service
        try:
            result = await service.resolve_token(token=token, requesting_user_id=user.user_id)
        except InvalidToken:
            raise HTTPException(status_code=401, detail="Invalid or expired share token") from None
        except PayloadNotFoundError:
            raise HTTPException(
                status_code=404,
                detail="Shared snapshot is no longer available; ask the sender for a fresh link",
            ) from None
        return JSONResponse(
            content=result.model_dump(mode="json"),
            headers={"Cache-Control": _NO_STORE},
        )

    @router.post(
        "/api/sessions/shared/{token}/import",
        status_code=201,
        response_model=SharedImportResponse,
    )
    async def import_shared_pipeline(
        token: str,
        request: Request,
        user: Annotated[UserIdentity, Depends(require_pipeline_user)],
        # Strict bucket: a token probe that also creates a session and copies files.
        rate_limiter: WebRateLimiter = Depends(get_rate_limiter),  # noqa: B008
    ) -> JSONResponse:
        """Import a copy of a shared pipeline and its files into a new session.

        Any authenticated pipeline user holding a link that allows import may
        import it, the sharer included; the token is the capability, so no
        ownership check applies. Nothing is created unless the token, the
        snapshot and every frozen file read cleanly.

        ``InvalidToken`` → 401; a view-only link → 403; a snapshot or frozen
        file no longer stored → 404 (ask the sender for a fresh link). Quota
        refusals and seeding refusals surface as their own errors, and the
        half-built session is archived.
        """
        await rate_limiter.check(user.user_id)
        try:
            result = await import_shared_snapshot(token=token, request=request, user=user)
        except InvalidToken:
            raise HTTPException(status_code=401, detail="Invalid or expired share token") from None
        except ImportNotAllowedError:
            raise HTTPException(status_code=403, detail="This share link does not allow import") from None
        except PayloadNotFoundError:
            raise HTTPException(
                status_code=404,
                detail="Shared snapshot is no longer available; ask the sender for a fresh link",
            ) from None
        return JSONResponse(
            status_code=201,
            content=result.model_dump(mode="json"),
            headers={"Cache-Control": _NO_STORE},
        )

    return router
