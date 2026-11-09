"""Request-scoped, audited inspection of another identity's composition."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer.yaml_generator import generate_public_composition_dict, generate_public_yaml
from elspeth.web.config import WebSettings
from elspeth.web.coordination.review_authority import RepositoryReviewAuthority
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.protocol import AuditAccessLogAuthority, AuditAccessLogWriteError, SessionServiceProtocol, WorkflowInspectDenied
from elspeth.web.sessions.routes.workflow.reviews import ReviewAttestationView
from elspeth.web.shareable_reviews.models import CompositionStateResponse as PublicCompositionStateResponse


class WorkflowInspectResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    state_id: str
    access_log_id: str
    composition_snapshot: PublicCompositionStateResponse
    yaml: str
    attestations: list[ReviewAttestationView]


def create_workflow_inspect_router() -> APIRouter:
    router = APIRouter(tags=["workflow-inspect"])

    @router.get("/api/workflow/inspect/{session_id}/{state_id}", response_model=WorkflowInspectResponse)
    async def inspect_workflow_state(
        session_id: UUID,
        state_id: UUID,
        request: Request,
        response: Response,
        user: Annotated[UserIdentity, Depends(get_current_user)],
    ) -> WorkflowInspectResponse:
        settings: WebSettings = request.app.state.settings
        if settings.workflow_governance != "on":
            raise HTTPException(
                status_code=409,
                detail={"error_type": "workflow_governance_off", "detail": "Workflow governance is off"},
            )

        authority: AuditAccessLogAuthority = request.app.state.audit_access_log_authority
        try:
            access = await run_sync_in_worker(
                authority.record_workflow_inspect,
                session_id=str(session_id),
                state_id=str(state_id),
                requesting_principal=user.user_id,
                ip_address=request.client.host if request.client else None,
            )
        except WorkflowInspectDenied:
            raise HTTPException(status_code=404, detail="Not found") from None
        except SQLAlchemyError as exc:
            raise AuditAccessLogWriteError("workflow inspection audit access could not be recorded") from exc

        service: SessionServiceProtocol = request.app.state.session_service
        record = await service.get_state_in_session(state_id, session_id)
        state = state_from_record(record)
        reviews: RepositoryReviewAuthority = request.app.state.review_authority
        attestations = await run_sync_in_worker(reviews.attestations_for, session_id=str(session_id), state_id=str(state_id))

        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return WorkflowInspectResponse(
            session_id=str(session_id),
            state_id=str(state_id),
            access_log_id=access.id,
            composition_snapshot=PublicCompositionStateResponse.model_validate(generate_public_composition_dict(state)),
            yaml=generate_public_yaml(state),
            attestations=[
                ReviewAttestationView(
                    attestation_id=item.attestation_id,
                    session_id=item.session_id,
                    state_id=item.state_id,
                    payload_digest=item.payload_digest,
                    reviewer_identity_id=item.reviewer_identity_id,
                    author_identity_id=item.author_identity_id,
                    attested_at=item.attested_at,
                    verdict=item.verdict,
                    note=item.note,
                )
                for item in attestations
            ],
        )

    return router
