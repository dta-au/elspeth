"""Pure immutable publication eligibility and sealed revocation evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import ValidationError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.sessions.pipeline_publication import (
    PipelinePublicationSQLResult as PipelinePublicationSQLResult,
)
from elspeth.web.sessions.pipeline_publication import (
    _ComposerRevocationRequired as _ComposerRevocationRequired,
)
from elspeth.web.sessions.pipeline_settlement_payloads import ComposerRevocationEvidence
from elspeth.web.sessions.protocol import ProposalEventRecord


@dataclass(frozen=True, slots=True)
class ComposerRevocationExpected:
    session_id: str
    proposal_id: str
    actor: str
    payload: ComposerRevocationEvidence

    def __post_init__(self) -> None:
        if type(self.payload) is not ComposerRevocationEvidence or type(self.actor) is not str or not self.actor:
            raise AuditIntegrityError("sealed revocation expected evidence has invalid owned types")
        for value in (self.session_id, self.proposal_id):
            if type(value) is not str:
                raise AuditIntegrityError("sealed revocation expected identity is not a string")
            try:
                parsed = UUID(value)
            except ValueError as error:
                raise AuditIntegrityError("sealed revocation expected identity is invalid") from error
            if str(parsed) != value:
                raise AuditIntegrityError("sealed revocation expected identity is not canonical")
        if self.payload.proposal_id != self.proposal_id:
            raise AuditIntegrityError("sealed revocation expected proposal identity mismatch")


@dataclass(frozen=True, slots=True)
class ComposerRevocationSQLResult:
    event: ProposalEventRecord
    expected: ComposerRevocationExpected


def decode_composer_revocation_result(result: ComposerRevocationSQLResult) -> ProposalEventRecord:
    """Validate the actual sealed row without repairing or reallocating evidence."""
    if type(result) is not ComposerRevocationSQLResult or type(result.event) is not ProposalEventRecord:
        raise AuditIntegrityError("sealed revocation projection requires owned SQL evidence")
    if type(result.expected) is not ComposerRevocationExpected or type(result.expected.payload) is not ComposerRevocationEvidence:
        raise AuditIntegrityError("sealed revocation projection requires owned expected evidence")
    event, expected = result.event, result.expected
    if type(event.id) is not UUID or type(event.session_id) is not UUID or type(event.proposal_id) is not UUID:
        raise AuditIntegrityError("sealed revocation projection row identities are not owned UUIDs")
    if type(event.created_at) is not datetime or event.created_at.tzinfo is None or event.created_at.utcoffset() is None:
        raise AuditIntegrityError("sealed revocation projection timestamp is not an aware owned datetime")
    try:
        payload = ComposerRevocationEvidence.model_validate(deep_thaw(event.payload))
    except (ValidationError, TypeError, ValueError) as error:
        raise AuditIntegrityError("sealed revocation projection payload is invalid") from error
    if (
        str(event.session_id) != expected.session_id
        or str(event.proposal_id) != expected.proposal_id
        or event.actor != expected.actor
        or event.event_type != "auto_commit.revoked"
        or payload != expected.payload
    ):
        raise AuditIntegrityError("sealed revocation projection identity mismatch")
    return event
