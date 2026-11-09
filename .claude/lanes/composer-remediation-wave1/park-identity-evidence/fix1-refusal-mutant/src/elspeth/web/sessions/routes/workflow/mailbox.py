"""Workflow mailbox and live approver picker for authenticated identities.

The approval authority owns eligibility and addressed-first ordering. This
route projects that decision for the inbox and badge without granting access
to a state merely because its request appears in the sent folder.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from elspeth.contracts.auth import IdentityRole
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.config import WebSettings
from elspeth.web.coordination.approval_authority import (
    ApprovalNotFound,
    ApprovalRecord,
    ApprovalTransactionAuthority,
    RepositoryApprovalAuthority,
)
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.coordination.review_authority import (
    RepositoryReviewAuthority,
    ReviewerRoleRequired,
    ReviewParticipantNotActive,
    ReviewRequestRecord,
    ReviewSentRecord,
)
from elspeth.web.sessions.routes.workflow.approvals import ApprovalView
from elspeth.web.sessions.routes.workflow.approvals import _view as _approval_view
from elspeth.web.sessions.routes.workflow.reviews import ReviewAttestationView, ReviewRequestView, _attestation_view, _request_view

MAILBOX_ROLES: Final[tuple[IdentityRole, ...]] = ("admin", "approver", "reviewer", "curator", "oversight", "user")
_DIRECTORY_PAGE: Final = 200


class MailboxSummaryResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    governance: Literal["on", "off"]
    roles: list[IdentityRole]
    approvals_to_decide: int
    reviews_to_attest: int
    decisions_unseen: int


class MailboxInboxResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    approvals: list[ApprovalView]
    reviews: list[ReviewRequestView]


class ReviewSentView(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    request: ReviewRequestView
    attestations: list[ReviewAttestationView]


class MailboxSentResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    approvals: list[ApprovalView]
    reviews: list[ReviewSentView]


class ApproverEntry(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    identity_id: str
    username: str


class ApproverDirectoryResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    approvers: list[ApproverEntry]
    suggested_identity_ids: list[str]


def decision_is_unseen(record: ApprovalRecord, *, caller: str) -> bool:
    """Exclude open requests and outcomes caused by the requester."""
    if (
        record.requested_by_identity_id != caller
        or record.decision is None
        or record.decision_seen_at is not None
        or record.decision == "superseded"
    ):
        return False
    return not (record.decision == "revoked" and record.revocation_actor_kind == "identity" and record.revoked_by_identity_id == caller)


def _settings(request: Request) -> WebSettings:
    settings: WebSettings = request.app.state.settings
    return settings


def _governance_on(request: Request) -> bool:
    return _settings(request).workflow_governance == "on"


def _identity_authority(request: Request) -> RepositoryIdentityAuthority:
    authority: RepositoryIdentityAuthority = request.app.state.identity_authority
    return authority


def _approval_authority(request: Request) -> ApprovalTransactionAuthority:
    authority: ApprovalTransactionAuthority = request.app.state.approval_authority
    return authority


def _review_authority(request: Request) -> RepositoryReviewAuthority:
    authority: RepositoryReviewAuthority = request.app.state.review_authority
    return authority


def _uncacheable(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _not_found(approval_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail={"error_type": "approval_not_found", "detail": f"approval {approval_id} not found"})


def _review_sent_view(record: ReviewSentRecord) -> ReviewSentView:
    return ReviewSentView(
        request=_request_view(record.request),
        attestations=[_attestation_view(attestation) for attestation in record.attestations],
    )


def _held_roles(authority: RepositoryIdentityAuthority, identity_id: str) -> list[IdentityRole]:
    """Only live, unscoped grants of an active human appear in navigation."""
    identity = authority.read_identity_summary(identity_id=identity_id)
    if identity is None or identity.access_state != "active" or identity.kind != "human" or identity.provider == "service":
        return []
    held = {grant.role for grant in authority.active_roles(identity_id=identity_id) if grant.scope is None}
    return [role for role in MAILBOX_ROLES if role in held]


def _approver_directory(authority: RepositoryIdentityAuthority, caller: str) -> ApproverDirectoryResponse:
    caller_identity = authority.read_identity_summary(identity_id=caller)
    if (
        caller_identity is None
        or caller_identity.access_state != "active"
        or caller_identity.kind != "human"
        or caller_identity.provider == "service"
    ):
        return ApproverDirectoryResponse(approvers=[], suggested_identity_ids=[])
    candidates: set[str] = set()
    offset = 0
    while True:
        grants = authority.list_roles(identity_id=None, include_revoked=False, limit=_DIRECTORY_PAGE, offset=offset)
        candidates.update(
            grant.identity_id for grant in grants if grant.role == "approver" and grant.scope is None and grant.identity_id != caller
        )
        if len(grants) < _DIRECTORY_PAGE:
            break
        offset += _DIRECTORY_PAGE
    approvers: list[ApproverEntry] = []
    for identity_id in sorted(candidates):
        if not authority.holds_active_role(identity_id=identity_id, role="approver"):
            continue
        summary = authority.read_identity_summary(identity_id=identity_id)
        if summary is None or summary.access_state != "active" or summary.kind != "human" or summary.provider == "service":
            continue
        approvers.append(ApproverEntry(identity_id=identity_id, username=summary.username))
    eligible = {entry.identity_id for entry in approvers}
    suggested: set[str] = set()
    offset = 0
    while True:
        edges = authority.list_relationships(identity_id=caller, include_revoked=False, limit=_DIRECTORY_PAGE, offset=offset)
        suggested.update(
            edge.from_identity_id
            for edge in edges
            if edge.to_identity_id == caller and edge.relationship_type == "approver" and edge.from_identity_id in eligible
        )
        if len(edges) < _DIRECTORY_PAGE:
            break
        offset += _DIRECTORY_PAGE
    return ApproverDirectoryResponse(approvers=approvers, suggested_identity_ids=sorted(suggested))


def create_mailbox_router() -> APIRouter:
    router = APIRouter(tags=["workflow-mailbox"])

    @router.get("/api/workflow/mailbox/summary", response_model=MailboxSummaryResponse)
    async def mailbox_summary(
        request: Request,
        response: Response,
        user: Annotated[UserIdentity, Depends(get_current_user)],
    ) -> MailboxSummaryResponse:
        _uncacheable(response)
        roles = await run_sync_in_worker(_held_roles, _identity_authority(request), user.user_id)
        if not _governance_on(request):
            return MailboxSummaryResponse(governance="off", roles=roles, approvals_to_decide=0, reviews_to_attest=0, decisions_unseen=0)
        approvals = (
            await run_sync_in_worker(_approval_authority(request).inbox, approver_identity_id=user.user_id) if "approver" in roles else ()
        )
        reviews = await _open_reviews_for_mailbox(request, user.user_id) if "reviewer" in roles else ()
        sent = await run_sync_in_worker(_approval_authority(request).sent, requested_by_identity_id=user.user_id)
        return MailboxSummaryResponse(
            governance="on",
            roles=roles,
            approvals_to_decide=len(approvals),
            reviews_to_attest=len(reviews),
            decisions_unseen=sum(1 for record in sent if decision_is_unseen(record, caller=user.user_id)),
        )

    @router.get("/api/workflow/mailbox/inbox", response_model=MailboxInboxResponse)
    async def mailbox_inbox(
        request: Request,
        response: Response,
        user: Annotated[UserIdentity, Depends(get_current_user)],
    ) -> MailboxInboxResponse:
        _uncacheable(response)
        if not _governance_on(request):
            return MailboxInboxResponse(approvals=[], reviews=[])
        roles = await run_sync_in_worker(_held_roles, _identity_authority(request), user.user_id)
        approvals = (
            await run_sync_in_worker(_approval_authority(request).inbox, approver_identity_id=user.user_id) if "approver" in roles else ()
        )
        reviews = await _open_reviews_for_mailbox(request, user.user_id) if "reviewer" in roles else ()
        return MailboxInboxResponse(
            approvals=[_approval_view(record) for record in approvals], reviews=[_request_view(record) for record in reviews]
        )

    @router.get("/api/workflow/mailbox/sent", response_model=MailboxSentResponse)
    async def mailbox_sent(
        request: Request,
        response: Response,
        user: Annotated[UserIdentity, Depends(get_current_user)],
    ) -> MailboxSentResponse:
        _uncacheable(response)
        if not _governance_on(request):
            return MailboxSentResponse(approvals=[], reviews=[])
        approvals = await run_sync_in_worker(_approval_authority(request).sent, requested_by_identity_id=user.user_id)
        reviews = await run_sync_in_worker(_review_authority(request).sent_for, requested_by=user.user_id)
        return MailboxSentResponse(
            approvals=[_approval_view(record) for record in approvals],
            reviews=[_review_sent_view(record) for record in reviews],
        )

    @router.get("/api/workflow/mailbox/approvers", response_model=ApproverDirectoryResponse)
    async def mailbox_approvers(
        request: Request,
        response: Response,
        user: Annotated[UserIdentity, Depends(get_current_user)],
    ) -> ApproverDirectoryResponse:
        _uncacheable(response)
        if not _governance_on(request):
            return ApproverDirectoryResponse(approvers=[], suggested_identity_ids=[])
        return await run_sync_in_worker(_approver_directory, _identity_authority(request), user.user_id)

    @router.post("/api/workflow/mailbox/{approval_id}/seen", response_model=ApprovalView)
    async def mailbox_mark_seen(
        approval_id: str,
        request: Request,
        response: Response,
        user: Annotated[UserIdentity, Depends(get_current_user)],
    ) -> ApprovalView:
        if not _governance_on(request):
            raise HTTPException(status_code=409, detail={"error_type": "workflow_governance_off", "detail": "Workflow governance is off"})
        authority = _approval_authority(request)
        session_id = await run_sync_in_worker(authority.session_id_of, approval_id)
        if session_id is None:
            raise _not_found(approval_id)

        def mutation(token: str) -> ApprovalRecord:
            return RepositoryApprovalAuthority.mark_decision_seen(token, approval_id=approval_id, requester_identity_id=user.user_id)

        try:
            record = await run_sync_in_worker(authority.run, session_id, mutation)
        except ApprovalNotFound as exc:
            raise _not_found(approval_id) from exc
        _uncacheable(response)
        return _approval_view(record)

    return router


async def _open_reviews_for_mailbox(request: Request, reviewer: str) -> tuple[ReviewRequestRecord, ...]:
    """Report a role change between the navigation and authority reads."""
    try:
        return await run_sync_in_worker(_review_authority(request).open_for, reviewer=reviewer)
    except (ReviewParticipantNotActive, ReviewerRoleRequired) as exc:
        raise HTTPException(
            status_code=409,
            detail={"error_type": "reviewer_scope_changed", "detail": "Reviewer eligibility changed; refresh the mailbox."},
        ) from exc
