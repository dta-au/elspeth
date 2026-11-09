"""Dependency-free nominal results for actual pipeline publication branches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from elspeth.contracts.composer_interpretation import InterpretationEventRecord
    from elspeth.web.composer.pipeline_commit import PipelineDispatchAuditBinding
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning
    from elspeth.web.sessions.protocol import (
        AuthoritativePipelineProposal,
        ChatMessageRecord,
        CompositionProposalRecord,
        CompositionStateRecord,
    )


@dataclass(frozen=True, slots=True)
class PipelineProposalSettlementResult:
    """Atomic accepted proposal, immutable state, and optional response."""

    proposal: CompositionProposalRecord
    state: CompositionStateRecord
    transition_message: ChatMessageRecord | None = None
    accepted_state: CompositionStateRecord | None = None
    interpretation_events: tuple[InterpretationEventRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class _ComposerRevocationRequired:
    running: ComposerOperationRunning
    authority: AuthoritativePipelineProposal
    dispatch: PipelineDispatchAuditBinding
    actor: str
    expected_content_hash: str
    required_trust_mode: Literal["auto_commit"] = "auto_commit"
    current_trust_mode: Literal["explicit_approve"] = "explicit_approve"


type PipelinePublicationSQLResult = PipelineProposalSettlementResult | _ComposerRevocationRequired
