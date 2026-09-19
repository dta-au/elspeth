"""Approval requests, decisions and mailbox projections.

The execution service compiles the state binding. All mutations then enter
the session-locked authority, which rechecks the current state and role grants
and records the audit event before committing.
"""

from __future__ import annotations

from typing import Annotated, Literal, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.audit import AuthAuditWriter
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.blobs.protocol import BlobNotFoundError
from elspeth.web.config import WebSettings
from elspeth.web.coordination.approval_authority import (
    MAX_APPROVAL_NOTE_BYTES,
    ApprovalAlreadyDecided,
    ApprovalBinding,
    ApprovalNotFound,
    ApprovalParticipantNotActive,
    ApprovalRecord,
    ApprovalRefusal,
    ApprovalStateNotCurrent,
    ApprovalSupersession,
    ApprovalTransactionAuthority,
    ApprovalWithdrawRequiresRequester,
    ApproverRoleRequired,
    RepositoryApprovalAuthority,
)
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.execution.errors import (
    BlobSourcePathMismatchError,
    ExecuteRequestValidationError,
    ExecutionReadinessError,
    PipelineValidationError,
    SemanticContractViolationError,
    UnresolvedInterpretationPlaceholderError,
)
from elspeth.web.execution.protocol import StateAccessError
from elspeth.web.sessions.protocol import SessionServiceProtocol
from elspeth.web.sessions.routes._helpers import _verify_session_ownership


class ApprovalBindingCompiler(Protocol):
    async def compile_approval_binding(
        self,
        session_id: UUID,
        state_id: UUID,
        *,
        user_id: str,
        session_operation_context: SessionOperationContext,
    ) -> ApprovalBinding: ...


class ApprovalRequestBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    state_id: UUID | None = Field(default=None, strict=False)
    approver_identity_id: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=MAX_APPROVAL_NOTE_BYTES)


class ApprovalDecisionBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    decision: Literal["approved", "rejected"]
    note: str | None = Field(default=None, max_length=MAX_APPROVAL_NOTE_BYTES)


class ApprovalView(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    approval_id: str
    session_id: str
    state_id: str
    binding: dict[str, str]
    requested_by_identity_id: str
    approver_identity_id: str
    requested_at: AwareDatetime
    decided_at: AwareDatetime | None
    decision: Literal["approved", "rejected", "revoked", "superseded"] | None
    request_note: str | None
    decision_seen_at: AwareDatetime | None
    decided_by_identity_id: str | None
    decision_note: str | None
    revoked_by_identity_id: str | None
    revocation_actor_kind: str | None
    revocation_event_id: str | None


class ApprovalListResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    approvals: list[ApprovalView]


def _view(record: ApprovalRecord) -> ApprovalView:
    return ApprovalView(
        approval_id=record.approval_id,
        session_id=record.session_id,
        state_id=record.state_id,
        binding=record.binding.as_json(),
        requested_by_identity_id=record.requested_by_identity_id,
        approver_identity_id=record.approver_identity_id,
        requested_at=record.requested_at,
        decided_at=record.decided_at,
        decision=record.decision,
        request_note=record.request_note,
        decision_seen_at=record.decision_seen_at,
        decided_by_identity_id=record.decided_by_identity_id,
        decision_note=record.decision_note,
        revoked_by_identity_id=record.revoked_by_identity_id,
        revocation_actor_kind=record.revocation_actor_kind,
        revocation_event_id=record.revocation_event_id,
    )


def _authority(request: Request) -> ApprovalTransactionAuthority:
    authority: ApprovalTransactionAuthority = request.app.state.approval_authority
    return authority


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


def _refused(exc: ApprovalRefusal) -> HTTPException:
    if type(exc) is ApprovalStateNotCurrent:
        return _state_not_found()
    name = type(exc).__name__
    code = "".join(f"_{letter.lower()}" if letter.isupper() else letter for letter in name).lstrip("_")
    detail: dict[str, str] = {"error_type": code, "detail": str(exc)}
    if isinstance(exc, ApprovalAlreadyDecided):
        detail["current_state"] = exc.current_state
    return HTTPException(status_code=404 if type(exc) is ApprovalNotFound else 409, detail=detail)


def _state_not_found() -> HTTPException:
    return HTTPException(status_code=404, detail={"error_type": "state_not_found", "detail": "State not found"})


def _uncacheable(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def create_approvals_router() -> APIRouter:
    router = APIRouter(tags=["workflow-approvals"])

    @router.post("/api/sessions/{session_id}/approvals", response_model=ApprovalView, status_code=201)
    async def request_approval(
        session_id: UUID,
        body: ApprovalRequestBody,
        request: Request,
        response: Response,
        user: Annotated[UserIdentity, Depends(get_current_user)],
    ) -> ApprovalView:
        _require_governance(request)
        session = await _verify_session_ownership(session_id, user, request)
        service: SessionServiceProtocol = request.app.state.session_service
        if body.state_id is None:
            current = await service.get_current_state(session_id)
            if current is None:
                raise _state_not_found()
            state_id = current.id
        else:
            try:
                state = await service.get_state(body.state_id)
            except ValueError as exc:
                raise _state_not_found() from exc
            if state.session_id != session_id:
                raise _state_not_found()
            state_id = state.id

        lease = await SessionOperationLease.acquire(
            service.session_operation_authority,
            session_id=session_id,
            operation_kind=SessionOperationKind.BLOB_READ,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
        try:
            execution: ApprovalBindingCompiler = request.app.state.execution_service
            try:
                binding = await execution.compile_approval_binding(
                    session_id,
                    state_id,
                    user_id=user.user_id,
                    session_operation_context=lease.context,
                )
            except StateAccessError as exc:
                raise _state_not_found() from exc
            except BlobNotFoundError as exc:
                raise HTTPException(status_code=404, detail="Blob not found") from exc
            except BlobSourcePathMismatchError as exc:
                raise HTTPException(
                    status_code=500,
                    detail={
                        "error_type": "blob_source_path_mismatch",
                        "detail": "Persisted blob source path failed integrity validation.",
                    },
                ) from exc
            except SemanticContractViolationError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error_type": "semantic_contract_violation",
                        "detail": str(exc),
                        "errors": [
                            {"component": entry.component, "message": entry.message, "severity": entry.severity} for entry in exc.entries
                        ],
                    },
                ) from exc
            except UnresolvedInterpretationPlaceholderError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error_type": "interpretation_placeholder_unresolved",
                        "detail": str(exc),
                        "placeholders": [{"node_id": node_id, "term": term} for node_id, term in exc.placeholders],
                    },
                ) from exc
            except PipelineValidationError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error_type": "pipeline_validation_failure",
                        "detail": str(exc),
                        "errors": [
                            {
                                "component_id": error.component_id,
                                "component_type": error.component_type,
                                "message": error.message,
                                "suggestion": error.suggestion,
                                "error_code": error.error_code,
                            }
                            for error in exc.errors
                        ],
                    },
                ) from exc
            except ExecutionReadinessError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error_type": "execution_not_ready",
                        "detail": str(exc),
                        "blockers": [item.model_dump() for item in exc.blockers],
                    },
                ) from exc
            except ExecuteRequestValidationError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            await lease.close()

        recorder: AuthAuditWriter = request.app.state.auth_audit_recorder
        provider = _settings(request).auth_provider

        def record(created: ApprovalRecord) -> None:
            recorder.record_approval_requested(request, provider=provider, approval=created)

        def mutate(token: str) -> ApprovalRecord:
            return RepositoryApprovalAuthority.request(
                token,
                session_id=str(session.id),
                state_id=str(state_id),
                binding=binding,
                requested_by=user.user_id,
                approver=body.approver_identity_id,
                note=body.note,
                record=record,
            )

        try:
            created = await run_sync_in_worker(_authority(request).run, str(session.id), mutate)
        except ApprovalRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return _view(created)

    @router.get("/api/approvals/inbox", response_model=ApprovalListResponse)
    async def inbox(
        request: Request,
        response: Response,
        user: Annotated[UserIdentity, Depends(get_current_user)],
    ) -> ApprovalListResponse:
        _uncacheable(response)
        if not _governance_on(request):
            return ApprovalListResponse(approvals=[])
        rows = await run_sync_in_worker(_authority(request).inbox, approver_identity_id=user.user_id)
        return ApprovalListResponse(approvals=[_view(row) for row in rows])

    @router.get("/api/approvals/sent", response_model=ApprovalListResponse)
    async def sent(
        request: Request,
        response: Response,
        user: Annotated[UserIdentity, Depends(get_current_user)],
    ) -> ApprovalListResponse:
        _uncacheable(response)
        if not _governance_on(request):
            return ApprovalListResponse(approvals=[])
        rows = await run_sync_in_worker(_authority(request).sent, requested_by_identity_id=user.user_id)
        return ApprovalListResponse(approvals=[_view(row) for row in rows])

    @router.post("/api/approvals/{approval_id}/decide", response_model=ApprovalView)
    async def decide(
        approval_id: str,
        body: ApprovalDecisionBody,
        request: Request,
        response: Response,
        user: Annotated[UserIdentity, Depends(get_current_user)],
    ) -> ApprovalView:
        _require_governance(request)
        authority = _authority(request)
        session_id = await run_sync_in_worker(authority.session_id_of, approval_id)
        if session_id is None:
            raise _refused(ApprovalNotFound())
        recorder: AuthAuditWriter = request.app.state.auth_audit_recorder
        provider = _settings(request).auth_provider

        def record(decided: ApprovalRecord) -> None:
            recorder.record_approval_decided(request, provider=provider, approval=decided, actor_identity_id=user.user_id)

        def record_rejection_bundle(decided: ApprovalRecord, retired: tuple[ApprovalSupersession, ...]) -> None:
            recorder.record_approval_rejection_with_supersessions(
                request,
                provider=provider,
                approval=decided,
                actor_identity_id=user.user_id,
                supersessions=retired,
            )

        def mutate(token: str) -> ApprovalRecord:
            return RepositoryApprovalAuthority.decide(
                token,
                approval_id=approval_id,
                decided_by=user.user_id,
                decision=body.decision,
                note=body.note,
                record=record,
                record_rejection_bundle=record_rejection_bundle,
            )

        try:
            decided = await run_sync_in_worker(authority.run, session_id, mutate)
        except (ApprovalParticipantNotActive, ApproverRoleRequired) as exc:
            raise _refused(ApprovalNotFound()) from exc
        except ApprovalRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return _view(decided)

    @router.post("/api/approvals/{approval_id}/withdraw", response_model=ApprovalView)
    async def withdraw(
        approval_id: str,
        request: Request,
        response: Response,
        user: Annotated[UserIdentity, Depends(get_current_user)],
    ) -> ApprovalView:
        _require_governance(request)
        authority = _authority(request)
        session_id = await run_sync_in_worker(authority.session_id_of, approval_id)
        if session_id is None:
            raise _refused(ApprovalNotFound())
        recorder: AuthAuditWriter = request.app.state.auth_audit_recorder
        provider = _settings(request).auth_provider

        def record(withdrawn: ApprovalRecord) -> None:
            recorder.record_approval_decided(request, provider=provider, approval=withdrawn, actor_identity_id=user.user_id)

        def mutate(token: str) -> ApprovalRecord:
            return RepositoryApprovalAuthority.withdraw(
                token,
                approval_id=approval_id,
                requested_by=user.user_id,
                record=record,
            )

        try:
            withdrawn = await run_sync_in_worker(authority.run, session_id, mutate)
        except ApprovalWithdrawRequiresRequester as exc:
            raise _refused(ApprovalNotFound()) from exc
        except ApprovalRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return _view(withdrawn)

    return router
