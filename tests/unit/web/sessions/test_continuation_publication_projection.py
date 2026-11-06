"""Same-connection pipeline replay proves actual accepted publication data."""

from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.protocol import PIPELINE_STAGED_REVIEW_MESSAGE, ComposerResult, PipelineCommitIntent
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkAuthority, RequiredWorkCoordinator, RequiredWorkSource
from elspeth.web.sessions.continuation_publication_projection import (
    PipelinePublicationReplayData,
    require_publication_projection_on_connection,
)
from elspeth.web.sessions.pipeline_finish_once import ComposerPipelineRevocationCompleted
from elspeth.web.sessions.routes.composer.pipeline_settlement import AutoCommitRevoked, PipelineRouteSettlement
from tests.unit.web.sessions import test_atomic_pipeline_review_evidence as pipeline_fixture

pytest_plugins = ("tests.unit.web.coordination.test_composer_operation_authority",)


@pytest.mark.asyncio
async def test_accepted_projection_replays_original_sql_cohort_and_refuses_assistant_substitution(operation_store):
    engine, repository, _authority, service, _sid = operation_store
    row, arguments, drafts, _record, running = await pipeline_fixture._prepared_pipeline(operation_store, with_review=True)
    settled = await service.settle_pipeline_composition_proposal(**arguments)
    assistant = settled.transition_message
    assert assistant is not None
    original = ComposerResult(
        message="Built",
        state=CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1),
        pipeline_commit_intent=PipelineCommitIntent(row.id, arguments["draft_hash"]),
    )
    route = PipelineRouteSettlement(settled, None, PipelinePublicationReplayData(row.id, arguments["draft_hash"], drafts))
    with engine.connect() as conn:
        require_publication_projection_on_connection(
            conn,
            running,
            service,
            original_result=original,
            publication_result=original,
            assistant=assistant,
            actor="user:alice",
            settlement=route,
            revocation=None,
        )
        with pytest.raises(AuditIntegrityError):
            require_publication_projection_on_connection(
                conn,
                running,
                service,
                original_result=original,
                publication_result=original,
                assistant=replace(assistant, content="Substituted assistant."),
                actor="user:alice",
                settlement=route,
                revocation=None,
            )
        with pytest.raises(AuditIntegrityError):
            require_publication_projection_on_connection(
                conn,
                running,
                service,
                original_result=original,
                publication_result=original,
                assistant=assistant,
                actor="user:another",
                settlement=route,
                revocation=None,
            )
    repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_revoked_projection_replays_original_event_and_only_fixed_public_notice(operation_store):
    engine, repository, _authority, service, sid = operation_store
    row, arguments, _drafts, _record, running = await pipeline_fixture._prepared_pipeline(operation_store, with_review=True)
    await service.update_composer_preferences(sid, trust_mode="explicit_approve", density_default="medium", actor="user:alice")
    arguments["required_trust_mode"] = "auto_commit"
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE,
            running.session_operation_context,
            durable_operation_id=running.claim.operation_id,
            claim_attempt=running.claim.attempt,
            proposal_id=str(row.id),
            tool_call_id=row.tool_call_id,
        )
    )
    tickets = {
        source: coordinator.reserve(source)
        for source in (
            RequiredWorkSource.PIPELINE_PUBLICATION_SQL,
            RequiredWorkSource.PIPELINE_PUBLICATION_PROJECTION,
            RequiredWorkSource.TRUST_REVOCATION_SQL,
            RequiredWorkSource.TRUST_REVOCATION_PROJECTION,
        )
    }
    outcome = await service.settle_pipeline_composition_proposal_finish_once(
        **arguments,
        coordinator=coordinator,
        required_work=tickets[RequiredWorkSource.PIPELINE_PUBLICATION_SQL],
        publication_projection_work=tickets[RequiredWorkSource.PIPELINE_PUBLICATION_PROJECTION],
        revocation_required_work=tickets[RequiredWorkSource.TRUST_REVOCATION_SQL],
        revocation_projection_work=tickets[RequiredWorkSource.TRUST_REVOCATION_PROJECTION],
    )
    assert type(outcome) is ComposerPipelineRevocationCompleted
    assert outcome.event.event_type == "auto_commit.revoked"
    assert outcome.deferred_cancellations == ()
    tickets[RequiredWorkSource.PIPELINE_PUBLICATION_PROJECTION].complete_unused(outcome.publication_projection_unused)
    coordinator.assert_completed()
    assert coordinator.failure_receipts() == ()
    original = ComposerResult(
        message="Built",
        state=CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1),
        pipeline_commit_intent=PipelineCommitIntent(row.id, arguments["draft_hash"]),
    )
    projected = replace(original, message=PIPELINE_STAGED_REVIEW_MESSAGE, pipeline_commit_intent=None)
    assistant = await service.add_message(
        sid,
        "assistant",
        PIPELINE_STAGED_REVIEW_MESSAGE,
        writer_principal="compose_loop",
        session_operation_context=running.session_operation_context,
    )
    revoked = AutoCommitRevoked("auto_commit", "explicit_approve")
    with engine.connect() as conn:
        require_publication_projection_on_connection(
            conn,
            running,
            service,
            original_result=original,
            publication_result=projected,
            assistant=assistant,
            actor="user:alice",
            settlement=None,
            revocation=revoked,
        )
        with pytest.raises(AuditIntegrityError):
            require_publication_projection_on_connection(
                conn,
                running,
                service,
                original_result=original,
                publication_result=replace(projected, message="Different review notice."),
                assistant=assistant,
                actor="user:alice",
                settlement=None,
                revocation=revoked,
            )
    repository.release(running.session_operation_context)
