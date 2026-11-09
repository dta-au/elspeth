"""Dependency-free immutable physical rejection and creation outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from elspeth.web.composer.pipeline_commit import PipelineDispatchAuditBinding
    from elspeth.web.coordination.contracts import SessionOperationContext
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning
    from elspeth.web.sessions.protocol import (
        AuthoritativePipelineProposal,
        CompositionProposalRecord,
        PipelineProposalRejectionReason,
        ProposalEventRecord,
    )


@dataclass(frozen=True, slots=True)
class PipelineRejectionExpected:
    authority: AuthoritativePipelineProposal
    reason: PipelineProposalRejectionReason
    dispatch: PipelineDispatchAuditBinding | None
    actor: str
    actor_user_id: str
    context: SessionOperationContext
    running: ComposerOperationRunning | None


@dataclass(frozen=True, slots=True)
class PipelineRejectionSQLResult:
    record: CompositionProposalRecord
    event: ProposalEventRecord
    expected: PipelineRejectionExpected
    transitioned: bool


@dataclass(frozen=True, slots=True)
class PipelineRejectionWriteResult:
    record: CompositionProposalRecord
    event: ProposalEventRecord
    transitioned: bool
