"""Review requests and reviewer attestations for the workflow mailbox.

The authority decides role eligibility and requires an open request for the
exact state. Attestations are audit evidence; they do not gate execution.
"""

from __future__ import annotations

import hashlib
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.audit import MAX_AUTH_AUDIT_TEXT_LENGTH, AuthAuditWriter
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.config import WebSettings
from elspeth.web.coordination.review_authority import (
    MAX_REVIEW_NOTE_BYTES,
    RepositoryReviewAuthority,
    ReviewAttestationRecord,
    ReviewAuthorityRefusal,
    ReviewerRoleRequired,
    ReviewParticipantNotActive,
    ReviewRequestNotFound,
    ReviewRequestRecord,
    ReviewVerdict,
    SessionNotOwnedByRequester,
    StateNotInSession,
)
from elspeth.web.sessions.protocol import CompositionStateRecord, SessionServiceProtocol
from elspeth.web.sessions.routes._helpers import _verify_session_ownership


class RequestReviewBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    state_id: str = Field(min_length=1, max_length=64)
    reviewer_identity_id: str | None = Field(default=None, min_length=1, max_length=MAX_AUTH_AUDIT_TEXT_LENGTH)
    note: str | None = Field(default=None, max_length=MAX_REVIEW_NOTE_BYTES)


class AttestBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    verdict: ReviewVerdict
    note: str | None = Field(default=None, max_length=MAX_REVIEW_NOTE_BYTES)


class ReviewRequestView(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    request_id: str
    session_id: str
    state_id: str
    requested_by_identity_id: str
    reviewer_identity_id: str | None
    requested_at: AwareDatetime
    cancelled_at: AwareDatetime | None
    request_note: str | None
    open: bool


class ReviewAttestationView(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    attestation_id: str
    session_id: str
    state_id: str
    payload_digest: str
    reviewer_identity_id: str
    author_identity_id: str
    attested_at: AwareDatetime
    verdict: ReviewVerdict
    note: str | None


class ReviewInboxResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    requests: list[ReviewRequestView]


def review_payload_digest(record: CompositionStateRecord) -> str:
    """Hash only the persisted state content that a reviewer can inspect."""
    content = {
        "version": record.version,
        "sources": record.sources,
        "source": record.source,
        "nodes": record.nodes,
        "edges": record.edges,
        "outputs": record.outputs,
        "metadata": record.metadata_,
    }
    return "sha256:" + hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def _authority(request: Request) -> RepositoryReviewAuthority:
    authority: RepositoryReviewAuthority = request.app.state.review_authority
    return authority


def _recorder(request: Request) -> AuthAuditWriter:
    recorder: AuthAuditWriter = request.app.state.auth_audit_recorder
    return recorder


def _settings(request: Request) -> WebSettings:
    settings: WebSettings = request.app.state.settings
    return settings


def _governance_on(request: Request) -> bool:
    return _settings(request).workflow_governance == "on"


def _require_governance(request: Request) -> None:
    if not _governance_on(request):
        raise HTTPException(
            status_code=409,
            detail={"error_type": "workflow_governance_off", "detail": "Workflow governance is off"},
        )


_NOT_FOUND_REFUSALS = frozenset({ReviewRequestNotFound, SessionNotOwnedByRequester, StateNotInSession})


def _refused(exc: ReviewAuthorityRefusal) -> HTTPException:
    name = type(exc).__name__
    code = "".join(f"_{letter.lower()}" if letter.isupper() else letter for letter in name).lstrip("_")
    status = 404 if type(exc) in _NOT_FOUND_REFUSALS else 409
    return HTTPException(status_code=status, detail={"error_type": code, "detail": str(exc)})


def _uncacheable(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _request_view(record: ReviewRequestRecord) -> ReviewRequestView:
    return ReviewRequestView(
        request_id=record.request_id,
        session_id=record.session_id,
        state_id=record.state_id,
        requested_by_identity_id=record.requested_by_identity_id,
        reviewer_identity_id=record.reviewer_identity_id,
        requested_at=record.requested_at,
        cancelled_at=record.cancelled_at,
        request_note=record.request_note,
        open=record.open,
    )


def _attestation_view(record: ReviewAttestationRecord) -> ReviewAttestationView:
    return ReviewAttestationView(
        attestation_id=record.attestation_id,
        session_id=record.session_id,
        state_id=record.state_id,
        payload_digest=record.payload_digest,
        reviewer_identity_id=record.reviewer_identity_id,
        author_identity_id=record.author_identity_id,
        attested_at=record.attested_at,
        verdict=record.verdict,
        note=record.note,
    )


def create_reviews_router() -> APIRouter:
    router = APIRouter(tags=["workflow-reviews"])

    @router.post("/api/sessions/{session_id}/reviews", response_model=ReviewRequestView, status_code=201)
    async def request_review(
        session_id: UUID,
        body: RequestReviewBody,
        request: Request,
        response: Response,
        user: Annotated[UserIdentity, Depends(get_current_user)],
    ) -> ReviewRequestView:
        _require_governance(request)
        session = await _verify_session_ownership(session_id, user, request)
        recorder = _recorder(request)
        provider = _settings(request).auth_provider

        def record(created: ReviewRequestRecord) -> None:
            recorder.record_review_requested(
                request,
                provider=provider,
                request_id=created.request_id,
                session_id=created.session_id,
                state_id=created.state_id,
                requested_by_identity_id=created.requested_by_identity_id,
                reviewer_identity_id=created.reviewer_identity_id,
                note=created.request_note,
            )

        try:
            created = await run_sync_in_worker(
                _authority(request).request,
                session_id=str(session.id),
                state_id=body.state_id,
                requested_by=user.user_id,
                reviewer=body.reviewer_identity_id,
                note=body.note,
                record=record,
            )
        except ReviewAuthorityRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return _request_view(created)

    @router.get("/api/reviews/inbox", response_model=ReviewInboxResponse)
    async def review_inbox(
        request: Request,
        response: Response,
        user: Annotated[UserIdentity, Depends(get_current_user)],
    ) -> ReviewInboxResponse:
        _uncacheable(response)
        if not _governance_on(request):
            return ReviewInboxResponse(requests=[])
        try:
            rows = await run_sync_in_worker(_authority(request).open_for, reviewer=user.user_id)
        except (ReviewParticipantNotActive, ReviewerRoleRequired):
            return ReviewInboxResponse(requests=[])
        return ReviewInboxResponse(requests=[_request_view(row) for row in rows])

    @router.post("/api/reviews/{request_id}/attest", response_model=ReviewAttestationView, status_code=201)
    async def attest_review(
        request_id: str,
        body: AttestBody,
        request: Request,
        response: Response,
        user: Annotated[UserIdentity, Depends(get_current_user)],
    ) -> ReviewAttestationView:
        _require_governance(request)
        authority = _authority(request)
        pointer = await run_sync_in_worker(authority.read_request, request_id=request_id)
        # Conceal addressed request IDs from everyone except their addressee
        # and author before reading the referenced composition state. The
        # authority still decides whether an open request currently covers
        # the state and reviewer; this check is only an ID disclosure fence.
        if pointer is None or (
            pointer.reviewer_identity_id is not None
            and pointer.reviewer_identity_id != user.user_id
            and pointer.requested_by_identity_id != user.user_id
        ):
            raise _refused(ReviewRequestNotFound("Review request not found"))
        service: SessionServiceProtocol = request.app.state.session_service
        try:
            state = await service.get_state(UUID(pointer.state_id))
        except ValueError as exc:
            raise _refused(StateNotInSession("State not found")) from exc
        digest = review_payload_digest(state)
        recorder = _recorder(request)
        provider = _settings(request).auth_provider

        def record(attested: ReviewAttestationRecord) -> None:
            authorizing_request_id = attested.authorizing_request_id
            if authorizing_request_id is None:
                raise AuditIntegrityError("review attestation has no authorizing request")
            recorder.record_review_attested(
                request,
                provider=provider,
                attestation_id=attested.attestation_id,
                authorizing_request_id=authorizing_request_id,
                session_id=attested.session_id,
                state_id=attested.state_id,
                payload_digest=attested.payload_digest,
                reviewer_identity_id=attested.reviewer_identity_id,
                author_identity_id=attested.author_identity_id,
                verdict=attested.verdict,
                note=attested.note,
            )

        try:
            attested = await run_sync_in_worker(
                authority.attest,
                session_id=pointer.session_id,
                state_id=pointer.state_id,
                payload_digest=digest,
                reviewer=user.user_id,
                verdict=body.verdict,
                note=body.note,
                record=record,
            )
        except ReviewAuthorityRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return _attestation_view(attested)

    @router.post("/api/reviews/{request_id}/cancel", response_model=ReviewRequestView)
    async def cancel_review(
        request_id: str,
        request: Request,
        response: Response,
        user: Annotated[UserIdentity, Depends(get_current_user)],
    ) -> ReviewRequestView:
        _require_governance(request)
        recorder = _recorder(request)
        provider = _settings(request).auth_provider

        def record(cancelled: ReviewRequestRecord) -> None:
            recorder.record_review_request_cancelled(
                request,
                provider=provider,
                request_id=cancelled.request_id,
                session_id=cancelled.session_id,
                state_id=cancelled.state_id,
                requested_by_identity_id=cancelled.requested_by_identity_id,
            )

        try:
            cancelled = await run_sync_in_worker(
                _authority(request).cancel, request_id=request_id, requested_by=user.user_id, record=record
            )
        except ReviewAuthorityRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return _request_view(cancelled)

    return router
