"""Explicit private reads of credential-filtered Composer evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.auth.middleware import require_pipeline_user
from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence, is_diagnostic_evidence_envelope

from ._helpers import UserIdentity, _verify_session_ownership


@dataclass(frozen=True, slots=True)
class DiagnosticEvidenceResponse:
    message_id: UUID
    created_at: datetime
    evidence: ComposerDiagnosticEvidence


def register_diagnostic_routes(router: APIRouter) -> None:
    @router.get("/{session_id}/diagnostic-evidence", response_model=list[DiagnosticEvidenceResponse])
    async def get_diagnostic_evidence(
        session_id: UUID,
        request: Request,
        user: Annotated[UserIdentity, Depends(require_pipeline_user)],
        limit: int = Query(1, ge=1, le=5),
        offset: int = Query(0, ge=0),
    ) -> list[DiagnosticEvidenceResponse]:
        """Return private authoring evidence only after recording owned access."""
        session = await _verify_session_ownership(session_id, user, request)
        service = request.app.state.session_service
        await service.record_audit_grade_view_async(
            session_id=str(session.id),
            requesting_principal=user.user_id,
            auth_provider_type=session.auth_provider_type,
            request_path=request.url.path,
            query_args={key: value for key, value in request.query_params.items() if key in {"limit", "offset"}},
            ip_address=request.client.host if request.client else None,
        )
        evidence_rows: list[DiagnosticEvidenceResponse] = []
        message_offset = 0
        evidence_offset = 0
        # A record can retain four separately bounded payloads. Scan one
        # storage row at a time and stop at the requested private page; an
        # ordinary chat page size would multiply that retained content.
        # Integrity checks cover the scanned prefix, not unrequested suffixes.
        while len(evidence_rows) < limit:
            messages = await service.get_messages(session.id, limit=1, offset=message_offset)
            if not messages:
                break
            message = messages[0]
            message_offset += 1
            if message.tool_calls is None:
                continue
            for envelope in message.tool_calls:
                if not is_diagnostic_evidence_envelope(envelope):
                    continue
                if message.role != "audit" or len(message.tool_calls) != 1 or message.writer_principal != "compose_loop":
                    raise AuditIntegrityError("Private Composer evidence requires a dedicated Composer audit row")
                evidence = ComposerDiagnosticEvidence.from_envelope(envelope, expected_session_id=str(session.id))
                if evidence_offset >= offset:
                    evidence_rows.append(DiagnosticEvidenceResponse(message.id, message.created_at, evidence))
                evidence_offset += 1
        return evidence_rows
