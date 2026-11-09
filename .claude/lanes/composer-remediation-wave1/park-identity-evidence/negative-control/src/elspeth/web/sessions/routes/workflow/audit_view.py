"""Approver audit view over scoped Sessions and read-only Landscape facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import bindparam, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import auth_events_table, run_attributions_table, runs_table
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.config import WebSettings
from elspeth.web.coordination.workflow_scope_reader import MAX_AUDIT_VIEW_ROWS, RepositoryWorkflowScopeReader
from elspeth.web.execution.discard_summary import _sqlite_database_file_missing

_RUNS_OF_SCOPE: Final = (
    select(
        run_attributions_table.c.run_id,
        run_attributions_table.c.initiated_by_user_id,
        run_attributions_table.c.recorded_at,
        runs_table.c.status,
        runs_table.c.started_at,
        runs_table.c.completed_at,
    )
    .select_from(run_attributions_table.join(runs_table, run_attributions_table.c.run_id == runs_table.c.run_id))
    .where(run_attributions_table.c.initiated_by_user_id.in_(bindparam("identity_ids", expanding=True)))
    .order_by(run_attributions_table.c.recorded_at.desc(), run_attributions_table.c.run_id)
    .limit(MAX_AUDIT_VIEW_ROWS)
)
_AUTH_EVENTS_OF_SCOPE: Final = (
    select(
        auth_events_table.c.event_id,
        auth_events_table.c.occurred_at,
        auth_events_table.c.event_type,
        auth_events_table.c.outcome,
        auth_events_table.c.identity_id,
        auth_events_table.c.metadata_json,
    )
    .where(auth_events_table.c.identity_id.in_(bindparam("identity_ids", expanding=True)))
    .order_by(auth_events_table.c.occurred_at.desc(), auth_events_table.c.event_id)
    .limit(MAX_AUDIT_VIEW_ROWS)
)


class _View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AuditViewRun(_View):
    run_id: str
    initiated_by_identity_id: str
    recorded_at: datetime
    status: str
    started_at: datetime
    completed_at: datetime | None


class AuditViewApproval(_View):
    approval_id: str
    session_id: str
    state_id: str
    requested_by_identity_id: str
    approver_identity_id: str
    requested_at: datetime
    decided_at: datetime | None
    decision: str | None


class AuditViewAttestation(_View):
    attestation_id: str
    session_id: str
    state_id: str
    payload_digest: str
    reviewer_identity_id: str
    author_identity_id: str
    attested_at: datetime
    verdict: str


class AuditViewAuthEvent(_View):
    event_id: str
    occurred_at: datetime
    event_type: str
    outcome: str
    identity_id: str
    metadata_json: str


class WorkflowAuditViewResponse(_View):
    identity_ids: list[str]
    truncated: bool
    runs: list[AuditViewRun]
    approvals: list[AuditViewApproval]
    attestations: list[AuditViewAttestation]
    auth_events: list[AuditViewAuthEvent]


@dataclass(frozen=True, slots=True)
class _LandscapeHalf:
    runs: tuple[AuditViewRun, ...]
    auth_events: tuple[AuditViewAuthEvent, ...]


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _read_landscape_half(settings: WebSettings, identity_ids: tuple[str, ...]) -> _LandscapeHalf:
    landscape_url = settings.get_landscape_url()
    if _sqlite_database_file_missing(landscape_url):
        raise AuditIntegrityError(f"Landscape audit database is missing at {landscape_url}")
    parameters = {"identity_ids": list(identity_ids)}
    with (
        LandscapeDB.from_url(landscape_url, passphrase=settings.landscape_passphrase, create_tables=False, read_only=True) as db,
        db.read_only_connection() as conn,
    ):
        run_rows = conn.execute(_RUNS_OF_SCOPE, parameters).all()
        event_rows = conn.execute(_AUTH_EVENTS_OF_SCOPE, parameters).all()
    return _LandscapeHalf(
        runs=tuple(
            AuditViewRun(
                run_id=row.run_id,
                initiated_by_identity_id=row.initiated_by_user_id,
                recorded_at=_utc(row.recorded_at),
                status=row.status,
                started_at=_utc(row.started_at),
                completed_at=None if row.completed_at is None else _utc(row.completed_at),
            )
            for row in run_rows
        ),
        auth_events=tuple(
            AuditViewAuthEvent(
                event_id=row.event_id,
                occurred_at=_utc(row.occurred_at),
                event_type=row.event_type,
                outcome=row.outcome,
                identity_id=row.identity_id,
                metadata_json=row.metadata_json,
            )
            for row in event_rows
        ),
    )


def create_workflow_audit_view_router() -> APIRouter:
    router = APIRouter(tags=["workflow-audit-view"])

    @router.get("/api/workflow/audit-view", response_model=WorkflowAuditViewResponse)
    async def workflow_audit_view(
        request: Request,
        response: Response,
        user: Annotated[UserIdentity, Depends(get_current_user)],
    ) -> WorkflowAuditViewResponse:
        settings: WebSettings = request.app.state.settings
        if settings.workflow_governance != "on":
            raise HTTPException(
                status_code=409,
                detail={"error_type": "workflow_governance_off", "detail": "Workflow governance is off"},
            )
        reader: RepositoryWorkflowScopeReader = request.app.state.workflow_scope_reader
        scope = await run_sync_in_worker(reader.read_audit_scope, caller=user.user_id)
        if not scope.caller_is_approver:
            raise HTTPException(status_code=404, detail="Not found")
        landscape = (
            _LandscapeHalf(runs=(), auth_events=())
            if not scope.identity_ids
            else await run_sync_in_worker(_read_landscape_half, settings, scope.identity_ids)
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return WorkflowAuditViewResponse(
            identity_ids=list(scope.identity_ids),
            truncated=scope.truncated,
            runs=list(landscape.runs),
            approvals=[
                AuditViewApproval(
                    approval_id=item.approval_id,
                    session_id=item.session_id,
                    state_id=item.state_id,
                    requested_by_identity_id=item.requested_by_identity_id,
                    approver_identity_id=item.approver_identity_id,
                    requested_at=item.requested_at,
                    decided_at=item.decided_at,
                    decision=item.decision,
                )
                for item in scope.approvals
            ],
            attestations=[
                AuditViewAttestation(
                    attestation_id=item.attestation_id,
                    session_id=item.session_id,
                    state_id=item.state_id,
                    payload_digest=item.payload_digest,
                    reviewer_identity_id=item.reviewer_identity_id,
                    author_identity_id=item.author_identity_id,
                    attested_at=item.attested_at,
                    verdict=item.verdict,
                )
                for item in scope.attestations
            ],
            auth_events=list(landscape.auth_events),
        )

    return router
