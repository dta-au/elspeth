"""Source locators for one actual Composer finalization projection.

The projection reader is joined to the settlement transaction by the service
owner. This module carries only complete nominal replay inputs, never a
caller-supplied permission flag or a second pipeline settlement algorithm.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.protocol import PIPELINE_STAGED_REVIEW_MESSAGE, ComposerResult, PipelineCommitIntent
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.models import proposal_events_table
from elspeth.web.sessions.pipeline_revocation import decode_composer_revocation_result
from elspeth.web.sessions.protocol import ChatMessageRecord, PreparedInterpretationEventDraft

if TYPE_CHECKING:
    from elspeth.web.sessions.routes.composer.pipeline_settlement import AutoCommitRevoked, PipelineRouteSettlement
    from elspeth.web.sessions.service import SessionServiceImpl


@dataclass(frozen=True, slots=True)
class PipelinePublicationReplayData:
    """Exact proposal and prepared cohort used by the real route settlement."""

    proposal_id: UUID
    draft_hash: str
    prepared_interpretations: tuple[PreparedInterpretationEventDraft, ...]

    def __post_init__(self) -> None:
        PipelineCommitIntent(self.proposal_id, self.draft_hash)
        if type(self.prepared_interpretations) is not tuple or any(
            type(item) is not PreparedInterpretationEventDraft for item in self.prepared_interpretations
        ):
            raise TypeError("publication replay requires its complete prepared interpretation cohort")


@dataclass(frozen=True, slots=True)
class PublicationProjectionInput:
    """Original Composer return and one actual auto-commit disposition.

    This is source data for the same-connection replay. It grants no
    publication authority until the original and resulting rows are proved.
    """

    original_result: ComposerResult
    settlement: PipelineRouteSettlement | None = None
    revocation: AutoCommitRevoked | None = None

    def __post_init__(self) -> None:
        from elspeth.web.sessions.routes.composer.pipeline_settlement import AutoCommitRevoked, PipelineRouteSettlement

        if type(self.original_result) is not ComposerResult or self.original_result.pipeline_commit_intent is None:
            raise TypeError("publication projection requires the original Composer auto-commit intent")
        if (self.settlement is not None and type(self.settlement) is not PipelineRouteSettlement) or (
            self.revocation is not None and type(self.revocation) is not AutoCommitRevoked
        ):
            raise TypeError("publication projection changed its actual settlement disposition type")
        if (self.settlement is None) == (self.revocation is None):
            raise TypeError("publication projection requires exactly one actual settlement disposition")


def require_publication_projection_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    service: SessionServiceImpl,
    *,
    original_result: ComposerResult,
    publication_result: ComposerResult,
    assistant: ChatMessageRecord,
    actor: str,
    settlement: PipelineRouteSettlement | None,
    revocation: AutoCommitRevoked | None,
) -> None:
    """Replay the actual SQL disposition and exact permitted public transformation."""
    from elspeth.web.coordination.composer_operation_authority import require_composer_settlement_actor_on_connection
    from elspeth.web.sessions.routes._helpers import composer_turn_end_assistant_row
    from elspeth.web.sessions.routes.composer.pipeline_settlement import AutoCommitRevoked, PipelineRouteSettlement

    projection = PublicationProjectionInput(original_result, settlement, revocation)
    if (
        type(conn) is not Connection
        or type(running) is not ComposerOperationRunning
        or type(publication_result) is not ComposerResult
        or type(assistant) is not ChatMessageRecord
        or type(actor) is not str
        or not actor
        or assistant.session_id != running.claim.session_id
        or assistant.role != "assistant"
        or assistant.writer_principal != "compose_loop"
    ):
        raise AuditIntegrityError("publication projection lost its actual SQL, assistant, or actor owner")
    if type(projection.settlement) is PipelineRouteSettlement:
        route = projection.settlement
        replay = route.publication_replay
        if (
            type(replay) is not PipelinePublicationReplayData
            or original_result.pipeline_commit_intent != PipelineCommitIntent(replay.proposal_id, replay.draft_hash)
            or publication_result != original_result
        ):
            raise AuditIntegrityError("pipeline projection changed the original accepted intent or result")
        proved = service.read_pipeline_publication_replay_on_connection(conn, session_id=running.claim.session_id, replay=replay)
        if proved != route.settlement:
            raise AuditIntegrityError("pipeline projection substituted its original accepted settlement")
        require_composer_settlement_actor_on_connection(conn, running, actor=actor)
        accepted = conn.execute(
            select(proposal_events_table.c.actor).where(
                proposal_events_table.c.session_id == str(running.claim.session_id),
                proposal_events_table.c.proposal_id == str(replay.proposal_id),
                proposal_events_table.c.event_type == "proposal.accepted",
            )
        ).all()
        if len(accepted) != 1 or accepted[0].actor != actor:
            raise AuditIntegrityError("pipeline projection changed its original accepted settlement actor")
        expected = proved.transition_message
        if expected is not None:
            if assistant != expected:
                raise AuditIntegrityError("pipeline projection substituted its accepted transition assistant")
        else:
            draft = composer_turn_end_assistant_row(original_result)
            if (assistant.content, assistant.raw_content) != (draft.content, draft.raw_content):
                raise AuditIntegrityError("pipeline projection substituted its original turn-end assistant")
    else:
        if type(projection.revocation) is not AutoCommitRevoked:
            raise AuditIntegrityError("publication projection lost its exact revocation disposition")
        original_intent = original_result.pipeline_commit_intent
        if original_intent is None or publication_result != replace(
            original_result, message=PIPELINE_STAGED_REVIEW_MESSAGE, pipeline_commit_intent=None
        ):
            raise AuditIntegrityError("revoked projection changed more than the original public notice")
        revoked = service.read_auto_commit_revocation_on_connection(
            conn,
            running=running,
            intent=original_intent,
            actor=actor,
            required=projection.revocation.required,
            current=projection.revocation.current,
        )
        decode_composer_revocation_result(revoked)
        draft = composer_turn_end_assistant_row(publication_result)
        if (assistant.content, assistant.raw_content) != (draft.content, draft.raw_content):
            raise AuditIntegrityError("revoked projection substituted its bounded turn-end assistant")
