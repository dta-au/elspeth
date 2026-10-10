"""Real SQLite transaction controls for the atomic pipeline review amendment."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError

from elspeth.contracts.chargeable_admission import (
    AdmissionPolicyEvidence,
    AdmissionRefusalReason,
    ChargeableAdmissionDecision,
    ChargeableAdmissionRefused,
    QuotaDisposition,
)
from elspeth.contracts.composer_interpretation import InterpretationKind, InterpretationSurfaceOrigin
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.audit import begin_dispatch, finish_success
from elspeth.web.composer.pipeline_planner import PipelinePlanResult
from elspeth.web.composer.pipeline_proposal import AbsentBase, PipelineProposal, PresentBase
from elspeth.web.composer.redaction import redact_tool_call_arguments
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
    RequiredWorkSource,
)
from elspeth.web.sessions.composer_operations import ComposerOperationFenceLost
from elspeth.web.sessions.models import (
    chat_messages_table,
    composition_states_table,
    interpretation_events_table,
    proposal_events_table,
    quota_provider_attempts_table,
    token_usage_ledger_table,
)
from elspeth.web.sessions.pipeline_finish_once import (
    ComposerPipelineBusinessReturned,
    ComposerPipelineRaised,
    ComposerPipelineRevocationCompleted,
    ComposerRevocationSQLResult,
    _ComposerRevocationRequired,
    decode_composer_revocation_result,
)
from elspeth.web.sessions.pipeline_review_evidence import verify_review_event_material
from elspeth.web.sessions.pipeline_settlement_payloads import ComposerRevocationEvidence, PipelineAcceptedEvidence, ReviewCohortMember
from elspeth.web.sessions.proposal_authority import _composition_state_data_content_hash
from elspeth.web.sessions.protocol import (
    CompositionStateData,
    PreparedInterpretationEventDraft,
    TransitionAssistantDraft,
    TrustModeAutoCommitRevokedError,
)
from elspeth.web.sessions.routes._helpers import _persist_tool_invocations
from tests.fixtures.identities import grant_test_pipeline_user
from tests.unit.web.coordination.test_composer_operation_authority import failed, start
from tests.unit.web.sessions.test_interpretation_events_service import _llm_node
from tests.unit.web.sessions.test_token_usage_adapters import _call

pytest_plugins = ("tests.unit.web.coordination.test_composer_operation_authority",)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_read", [False, True])
async def test_required_preparation_read_joins_actual_sql_and_projects_before_cancellation(operation_store, failed_read):
    engine, repository, _authority, service, sid = operation_store
    _record, running = start(operation_store)
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE,
            running.session_operation_context,
            durable_operation_id=running.claim.operation_id,
            claim_attempt=running.claim.attempt,
        )
    )
    binding = RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN)
    entered, release = threading.Event(), threading.Event()
    original = OperationalError("controlled preparation SQL", {}, RuntimeError("controlled"))

    def park(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("SELECT sessions.user_id"):
            entered.set()
            assert release.wait(5)
            if failed_read:
                raise original

    event.listen(engine, "before_cursor_execute", park)
    task = asyncio.create_task(service._session_principal_context(str(sid), preparation_work=binding))
    try:
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        task.cancel("original preparation cancellation")
        await asyncio.sleep(0.03)
        assert not task.done()
        release.set()
        if failed_read:
            with pytest.raises(BaseExceptionGroup) as observed:
                await task
            assert observed.value.exceptions[0] is original
            assert isinstance(observed.value.exceptions[1], asyncio.CancelledError)
            assert observed.value.exceptions[1].args == ("original preparation cancellation",)
        else:
            with pytest.raises(asyncio.CancelledError) as observed_cancel:
                await task
            assert observed_cancel.value.args == ("original preparation cancellation",)
        coordinator.assert_completed()
        receipts = coordinator.failure_receipts()
        assert [receipt.key.source for receipt in receipts] == ([RequiredWorkSource.PREPARATION_READ_SQL] if failed_read else [])
        if failed_read:
            assert receipts[0].original_root is original
    finally:
        release.set()
        event.remove(engine, "before_cursor_execute", park)
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition_kind", ["undispatched", "terminal_audit"])
@pytest.mark.parametrize(
    "source,admission_source",
    [("composer", RequiredWorkSource.PROVIDER_ADMISSION_SQL), ("auto_title", RequiredWorkSource.TITLE_PROVIDER_ADMISSION_SQL)],
)
async def test_required_provider_admission_and_actual_undispatched_disposition(operation_store, source, admission_source, disposition_kind):
    engine, repository, _authority, service, sid = operation_store
    session = await service.get_session(sid)
    with engine.begin() as conn:
        grant_test_pipeline_user(conn, identity_id=session.user_id)
    _record, running = start(operation_store)
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE,
            running.session_operation_context,
            durable_operation_id=running.claim.operation_id,
            claim_attempt=running.claim.attempt,
        )
    )
    admission = coordinator.reserve(admission_source)
    attempt = await service.begin_provider_attempt(
        session_operation_context=running.session_operation_context, source=source, required_work=admission
    )
    with engine.connect() as conn:
        pending = conn.execute(
            select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.attempt_id == attempt.attempt_id)
        ).one()
    assert pending.source == source and pending.settled_at is None
    if disposition_kind == "undispatched":
        disposition = coordinator.reserve(RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL)
        await service.cancel_undispatched_provider_attempt(
            session_operation_context=running.session_operation_context,
            attempt_id=attempt.attempt_id,
            requested_model="controlled-no-sdk-entry",
            required_work=disposition,
        )
    else:
        settlement_source = (
            RequiredWorkSource.TITLE_PROVIDER_SETTLEMENT_SQL if source == "auto_title" else RequiredWorkSource.PROVIDER_SETTLEMENT_SQL
        )
        disposition = coordinator.reserve(settlement_source)
        call = replace(
            _call(prompt=3, completion=2),
            call_id=attempt.attempt_id,
            started_at=attempt.started_at,
            finished_at=attempt.started_at + timedelta(milliseconds=1),
        )
        await service.finish_provider_attempt(
            session_operation_context=running.session_operation_context, call=call, required_work=disposition
        )
    with engine.connect() as conn:
        settled = conn.execute(
            select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.attempt_id == attempt.attempt_id)
        ).one()
        usage = conn.execute(select(token_usage_ledger_table).where(token_usage_ledger_table.c.entry_id == settled.ledger_entry_id)).one()
    assert settled.settled_at is not None and usage.source == source
    expected_prompt, expected_completion = (0, 0) if disposition_kind == "undispatched" else (3, 2)
    assert usage.prompt_tokens == expected_prompt and usage.completion_tokens == expected_completion
    coordinator.assert_completed()
    assert coordinator.failure_receipts() == ()
    repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_required_provider_refusal_recorder_uses_one_physical_callable(operation_store, monkeypatch):
    import elspeth.web.sessions.service as service_module

    _engine, repository, _authority, service, _sid = operation_store
    _record, running = start(operation_store)
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE,
            running.session_operation_context,
            durable_operation_id=running.claim.operation_id,
            claim_attempt=running.claim.attempt,
        )
    )
    ticket = coordinator.reserve(RequiredWorkSource.PROVIDER_ADMISSION_SQL)
    refusal = ChargeableAdmissionRefused(
        ChargeableAdmissionDecision(
            refusal_reason=AdmissionRefusalReason.QUOTA_EXCEEDED,
            evidence=AdmissionPolicyEvidence(
                quota_disposition=QuotaDisposition.EXCEEDED,
                secret_wiring_hash="a" * 64,
                identity_policy_id="controlled-policy",
                dimension="tokens",
                cap=1,
                usage=1,
            ),
        )
    )
    entered, recorded, submissions = [], [], []

    def refuse(self, **_kwargs):
        entered.append(threading.get_ident())
        raise refusal

    def record(_outcome):
        recorded.append(threading.get_ident())

    original_bridge = service_module.run_required_sql_in_worker

    async def count_bridge(receipt, callable_):
        submissions.append(receipt)
        return await original_bridge(receipt, callable_)

    monkeypatch.setattr(type(service), "_begin_provider_attempt_sync", refuse)
    monkeypatch.setattr(service, "_quota_exceeded_recorder", record)
    monkeypatch.setattr(service_module, "run_required_sql_in_worker", count_bridge)
    with pytest.raises(ChargeableAdmissionRefused) as captured:
        await service.begin_provider_attempt(
            session_operation_context=running.session_operation_context, source="composer", required_work=ticket
        )
    assert captured.value is refusal
    assert submissions == [ticket] and entered == recorded and len(entered) == 1
    coordinator.assert_completed()
    repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", [False, True])
async def test_required_pipeline_finish_once_closes_exact_business_or_revocation_tickets(operation_store, revoked):
    engine, repository, _authority, service, sid = operation_store
    row, arguments, _drafts, _record, running = await _prepared_pipeline(operation_store, with_review=True)
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
    if revoked:
        await service.update_composer_preferences(sid, trust_mode="explicit_approve", density_default="medium", actor="user:alice")
        arguments["required_trust_mode"] = "auto_commit"
    result = await service.settle_pipeline_composition_proposal_finish_once(
        **arguments,
        coordinator=coordinator,
        required_work=tickets[RequiredWorkSource.PIPELINE_PUBLICATION_SQL],
        publication_projection_work=tickets[RequiredWorkSource.PIPELINE_PUBLICATION_PROJECTION],
        revocation_required_work=tickets[RequiredWorkSource.TRUST_REVOCATION_SQL],
        revocation_projection_work=tickets[RequiredWorkSource.TRUST_REVOCATION_PROJECTION],
    )
    projection = tickets[RequiredWorkSource.PIPELINE_PUBLICATION_PROJECTION]
    if revoked:
        assert isinstance(result, ComposerPipelineRevocationCompleted)
        assert result.event.event_type == "auto_commit.revoked"
        projection.complete_unused(result.publication_projection_unused)
        with engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(interpretation_events_table)).scalar_one() == 0
    else:
        assert isinstance(result, ComposerPipelineBusinessReturned)
        assert len(result.result.interpretation_events) == 1
        projection.begin_projection()
        projection.complete_owned()
    assert result.deferred_cancellations == ()
    coordinator.assert_completed()
    assert coordinator.failure_receipts() == ()
    repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked,park_stage", [(False, "publication"), (True, "publication"), (True, "revocation")])
async def test_required_pipeline_finish_once_retains_actual_result_after_repeated_cancellation(
    operation_store, monkeypatch, revoked, park_stage
):
    _engine, repository, _authority, service, sid = operation_store
    row, arguments, _drafts, _record, running = await _prepared_pipeline(operation_store)
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
    if revoked:
        await service.update_composer_preferences(sid, trust_mode="explicit_approve", density_default="medium", actor="user:alice")
        arguments["required_trust_mode"] = "auto_commit"
    entered, release = threading.Event(), threading.Event()
    if park_stage == "publication":
        original = type(service)._run_pipeline_publication_finish_once

        async def parked_publication(self, work, publication):
            def parked():
                result = publication()
                entered.set()
                assert release.wait(5), "publication SQL must be released by its independent owner"
                return result

            return await original(self, work, parked)

        monkeypatch.setattr(type(service), "_run_pipeline_publication_finish_once", parked_publication)
    else:
        original_revocation = type(service)._record_required_composer_revocation_sync

        def parked_revocation(self, eligibility):
            entered.set()
            assert release.wait(5), "revocation SQL must be released by its independent owner"
            return original_revocation(self, eligibility)

        monkeypatch.setattr(type(service), "_record_required_composer_revocation_sync", parked_revocation)
    task = asyncio.create_task(
        service.settle_pipeline_composition_proposal_finish_once(
            **arguments,
            coordinator=coordinator,
            required_work=tickets[RequiredWorkSource.PIPELINE_PUBLICATION_SQL],
            publication_projection_work=tickets[RequiredWorkSource.PIPELINE_PUBLICATION_PROJECTION],
            revocation_required_work=tickets[RequiredWorkSource.TRUST_REVOCATION_SQL],
            revocation_projection_work=tickets[RequiredWorkSource.TRUST_REVOCATION_PROJECTION],
        )
    )
    try:
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done()
    finally:
        release.set()
    result = await task
    assert len(result.deferred_cancellations) == 3
    assert all(isinstance(error, asyncio.CancelledError) for error in result.deferred_cancellations)
    projection = tickets[RequiredWorkSource.PIPELINE_PUBLICATION_PROJECTION]
    assert not projection.complete
    if revoked:
        assert isinstance(result, ComposerPipelineRevocationCompleted)
        projection.complete_unused(result.publication_projection_unused)
    else:
        assert isinstance(result, ComposerPipelineBusinessReturned)
        projection.begin_projection()
        projection.complete_owned()
    coordinator.assert_completed()
    repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["coordinator", "semantic"])
async def test_required_pipeline_ticket_bundle_mismatch_refuses_before_sql(operation_store, mismatch):
    engine, repository, _authority, service, _sid = operation_store
    row, arguments, _drafts, _record, running = await _prepared_pipeline(operation_store)
    scope = RequiredWorkAuthority(
        RequiredAuthorityKind.DURABLE_COMPOSE,
        running.session_operation_context,
        durable_operation_id=running.claim.operation_id,
        claim_attempt=running.claim.attempt,
        proposal_id=str(row.id),
        tool_call_id=row.tool_call_id,
    )
    coordinator = RequiredWorkCoordinator(scope)
    tickets = {
        source: coordinator.reserve(source)
        for source in (
            RequiredWorkSource.PIPELINE_PUBLICATION_SQL,
            RequiredWorkSource.PIPELINE_PUBLICATION_PROJECTION,
            RequiredWorkSource.TRUST_REVOCATION_SQL,
            RequiredWorkSource.TRUST_REVOCATION_PROJECTION,
        )
    }
    supplied = RequiredWorkCoordinator(scope) if mismatch == "coordinator" else coordinator
    if mismatch == "semantic":
        tickets[RequiredWorkSource.TRUST_REVOCATION_SQL] = coordinator.reserve(RequiredWorkSource.TRUST_REVOCATION_SQL, semantic_ordinal=1)
    statements = []

    def observe(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        with pytest.raises(AuditIntegrityError):
            await service.settle_pipeline_composition_proposal_finish_once(
                **arguments,
                coordinator=supplied,
                required_work=tickets[RequiredWorkSource.PIPELINE_PUBLICATION_SQL],
                publication_projection_work=tickets[RequiredWorkSource.PIPELINE_PUBLICATION_PROJECTION],
                revocation_required_work=tickets[RequiredWorkSource.TRUST_REVOCATION_SQL],
                revocation_projection_work=tickets[RequiredWorkSource.TRUST_REVOCATION_PROJECTION],
            )
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert statements == [], "foreign or cross-semantic bundle must refuse before physical SQL"
    repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_source", [RequiredWorkSource.TRUST_REVOCATION_SQL, RequiredWorkSource.TRUST_REVOCATION_PROJECTION])
async def test_required_revocation_fault_retains_exact_source_and_eligibility_disposition(operation_store, monkeypatch, failed_source):
    import elspeth.web.sessions.service as service_module

    engine, repository, _authority, service, sid = operation_store
    row, arguments, _drafts, _record, running = await _prepared_pipeline(operation_store)
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
    original = (
        OperationalError("controlled revocation INSERT", {}, RuntimeError("controlled storage fault"))
        if failed_source is RequiredWorkSource.TRUST_REVOCATION_SQL
        else AuditIntegrityError("controlled required projection fault")
    )

    def fail_sql(_conn, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith("INSERT INTO PROPOSAL_EVENTS") and "auto_commit.revoked" in parameters:
            raise original

    def fail_projection(_result):
        raise original

    if failed_source is RequiredWorkSource.TRUST_REVOCATION_SQL:
        event.listen(engine, "before_cursor_execute", fail_sql)
    else:
        monkeypatch.setattr(service_module, "decode_composer_revocation_result", fail_projection)
    try:
        result = await service.settle_pipeline_composition_proposal_finish_once(
            **arguments,
            coordinator=coordinator,
            required_work=tickets[RequiredWorkSource.PIPELINE_PUBLICATION_SQL],
            publication_projection_work=tickets[RequiredWorkSource.PIPELINE_PUBLICATION_PROJECTION],
            revocation_required_work=tickets[RequiredWorkSource.TRUST_REVOCATION_SQL],
            revocation_projection_work=tickets[RequiredWorkSource.TRUST_REVOCATION_PROJECTION],
        )
    finally:
        if failed_source is RequiredWorkSource.TRUST_REVOCATION_SQL:
            event.remove(engine, "before_cursor_execute", fail_sql)
    assert isinstance(result, ComposerPipelineRaised) and result.error is original
    assert result.publication_projection_disposition.value == "eligibility_selected"
    tickets[RequiredWorkSource.PIPELINE_PUBLICATION_PROJECTION].complete_unused(result.publication_projection_unused)
    coordinator.assert_completed()
    (receipt,) = coordinator.failure_receipts()
    assert receipt.key.source is failed_source and receipt.original_root is original
    with engine.connect() as conn:
        count = conn.execute(
            select(func.count()).select_from(proposal_events_table).where(proposal_events_table.c.event_type == "auto_commit.revoked")
        ).scalar_one()
    assert count == int(failed_source is RequiredWorkSource.TRUST_REVOCATION_PROJECTION)
    repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_phase", ["preflight", "publication"])
async def test_required_pipeline_failure_disposition_is_minted_by_actual_phase(operation_store, monkeypatch, failed_phase):
    engine, repository, _authority, service, _sid = operation_store
    row, arguments, _drafts, _record, running = await _prepared_pipeline(operation_store)
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
    original = AuditIntegrityError("same controlled phase error")
    if failed_phase == "preflight":
        import elspeth.web.sessions.service as service_module

        def fail_hash(_state):
            raise original

        monkeypatch.setattr(service_module, "_composition_state_data_content_hash", fail_hash)
    else:

        def fail_read(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith("SELECT"):
                raise original

        event.listen(engine, "before_cursor_execute", fail_read)
    try:
        result = await service.settle_pipeline_composition_proposal_finish_once(
            **arguments,
            coordinator=coordinator,
            required_work=tickets[RequiredWorkSource.PIPELINE_PUBLICATION_SQL],
            publication_projection_work=tickets[RequiredWorkSource.PIPELINE_PUBLICATION_PROJECTION],
            revocation_required_work=tickets[RequiredWorkSource.TRUST_REVOCATION_SQL],
            revocation_projection_work=tickets[RequiredWorkSource.TRUST_REVOCATION_PROJECTION],
        )
    finally:
        if failed_phase == "publication":
            event.remove(engine, "before_cursor_execute", fail_read)
    assert isinstance(result, ComposerPipelineRaised) and result.error is original
    expected = "preflight_refused" if failed_phase == "preflight" else "publication_failed"
    assert result.publication_projection_disposition.value == expected
    tickets[RequiredWorkSource.PIPELINE_PUBLICATION_PROJECTION].complete_unused(result.publication_projection_unused)
    coordinator.assert_completed()
    (receipt,) = coordinator.failure_receipts()
    assert receipt.original_root is original and receipt.key.source is RequiredWorkSource.PIPELINE_PUBLICATION_SQL
    repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown_source", [RequiredWorkSource.PIPELINE_PUBLICATION_SQL, RequiredWorkSource.TRUST_REVOCATION_SQL])
async def test_required_pipeline_unknown_handoff_cannot_issue_arm_or_complete_unused(operation_store, monkeypatch, unknown_source):
    import elspeth.web.sessions.service as service_module
    from elspeth.web.required_work import RequiredWorkIncomplete

    engine, _repository, _authority, service, sid = operation_store
    row, arguments, _drafts, _record, running = await _prepared_pipeline(operation_store)
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
    original_bridge = service_module.run_required_sql_finish_once
    unresolved = service_module.ComposerTerminalSQLCompletionUnknown("controlled unknown bridge handoff")

    async def unknown_bridge(ticket, callable_, *args):
        if ticket.key.source is unknown_source:
            # This is a facade handoff negative control. Actual generation/
            # executor anomaly proofs belong to transport; no SQL is launched.
            ticket.observe_submission_unknown(unresolved)
            raise unresolved
        return await original_bridge(ticket, callable_, *args)

    monkeypatch.setattr(service_module, "run_required_sql_finish_once", unknown_bridge)
    with pytest.raises(service_module.ComposerTerminalSQLCompletionUnknown) as captured:
        await service.settle_pipeline_composition_proposal_finish_once(
            **arguments,
            coordinator=coordinator,
            required_work=tickets[RequiredWorkSource.PIPELINE_PUBLICATION_SQL],
            publication_projection_work=tickets[RequiredWorkSource.PIPELINE_PUBLICATION_PROJECTION],
            revocation_required_work=tickets[RequiredWorkSource.TRUST_REVOCATION_SQL],
            revocation_projection_work=tickets[RequiredWorkSource.TRUST_REVOCATION_PROJECTION],
        )
    assert captured.value is unresolved
    assert not tickets[RequiredWorkSource.PIPELINE_PUBLICATION_PROJECTION].complete
    assert not tickets[RequiredWorkSource.TRUST_REVOCATION_SQL].complete
    assert not tickets[RequiredWorkSource.TRUST_REVOCATION_PROJECTION].complete
    with pytest.raises(RequiredWorkIncomplete):
        coordinator.assert_completed()
    with engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count()).select_from(proposal_events_table).where(proposal_events_table.c.event_type == "auto_commit.revoked")
            ).scalar_one()
            == 0
        )


@pytest.mark.asyncio
async def test_separate_revocation_callable_replays_actual_event_and_strict_projection(operation_store):
    _engine, repository, _authority, service, sid = operation_store
    row, arguments, _drafts, _record, running = await _prepared_pipeline(operation_store)
    await service.update_composer_preferences(sid, trust_mode="explicit_approve", density_default="medium", actor="user:alice")
    authority = await service.get_authoritative_pipeline_proposal(session_id=sid, proposal_id=row.id)
    eligibility = _ComposerRevocationRequired(
        running, authority, arguments["dispatch"], arguments["actor"], arguments["candidate_content_hash"]
    )
    result = service._record_required_composer_revocation_sync(eligibility)
    assert decode_composer_revocation_result(result) is result.event
    replay = service._record_required_composer_revocation_sync(eligibility)
    assert replay.event == result.event
    with pytest.raises(AuditIntegrityError):
        decode_composer_revocation_result(replace(result, event=replace(result.event, actor="user:mallory")))
    with pytest.raises(AuditIntegrityError):
        decode_composer_revocation_result(ComposerRevocationSQLResult(result.event, None))
    forged_payload = dict(deep_thaw(result.event.payload))
    forged_payload["unexpected"] = True
    with pytest.raises(AuditIntegrityError):
        decode_composer_revocation_result(replace(result, event=replace(result.event, payload=forged_payload)))
    for field in ("required_trust_mode", "current_trust_mode"):
        forged_trust = dict(deep_thaw(result.event.payload))
        forged_trust[field] = "freeform"
        with pytest.raises(AuditIntegrityError) as rejected:
            decode_composer_revocation_result(replace(result, event=replace(result.event, payload=forged_trust)))
        assert rejected.value.__cause__ is not None
    repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["trust", "lease"])
async def test_sealed_revocation_rechecks_actual_authority_after_eligibility_capture(operation_store, changed):
    engine, repository, _authority, service, sid = operation_store
    row, arguments, _drafts, _record, running = await _prepared_pipeline(operation_store)
    await service.update_composer_preferences(sid, trust_mode="explicit_approve", density_default="medium", actor="user:alice")
    authority = await service.get_authoritative_pipeline_proposal(session_id=sid, proposal_id=row.id)
    eligibility = _ComposerRevocationRequired(
        running, authority, arguments["dispatch"], arguments["actor"], arguments["candidate_content_hash"]
    )
    if changed == "trust":
        await service.update_composer_preferences(sid, trust_mode="auto_commit", density_default="medium", actor="user:alice")
    else:
        repository.release(running.session_operation_context)
    with pytest.raises(AuditIntegrityError):
        service._record_required_composer_revocation_sync(eligibility)
    with engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count()).select_from(proposal_events_table).where(proposal_events_table.c.event_type == "auto_commit.revoked")
            ).scalar_one()
            == 0
        )
        assert conn.execute(select(func.count()).select_from(composition_states_table)).scalar_one() == 0
    assert (await service.get_authoritative_pipeline_proposal(session_id=sid, proposal_id=row.id)).row.status == "pending"
    if changed == "trust":
        repository.release(running.session_operation_context)


async def _prepared_pipeline(store, *, with_review=False, opted_out=False, older_review=False, deadline_seconds=180.0):
    _engine, _repository, _authority, service, sid = store
    record, running = start(store, deadline_seconds=deadline_seconds)
    if opted_out:
        await service.record_session_interpretation_opt_out(
            session_id=sid,
            actor="user:alice",
            session_operation_context=running.session_operation_context,
        )
    user = await service.add_message_with_transcript(
        sid,
        "user",
        "Build",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    state = CompositionStateData(
        sources={}, nodes=[_llm_node()] if with_review else [], edges=[], outputs=[], metadata_={"name": "Atomic review", "description": ""}
    )
    pipeline = deep_thaw({"sources": state.sources, "nodes": state.nodes, "edges": state.edges, "outputs": state.outputs})
    base: AbsentBase | PresentBase = AbsentBase()
    if older_review:
        old_state = await service.save_composition_state(
            sid, state, provenance="tool_call", session_operation_context=running.session_operation_context
        )
        base = PresentBase(old_state.id, _composition_state_data_content_hash(state))
        await service.create_pending_interpretation_event(
            session_id=sid,
            composition_state_id=old_state.id,
            affected_node_id="llm_transform_1",
            tool_call_id="old-provider-string",
            user_term="cool",
            kind=InterpretationKind.VAGUE_TERM,
            llm_draft="polished",
            model_identifier="model",
            model_version="version",
            provider="test",
            composer_skill_hash=stable_hash("skill"),
            session_operation_context=running.session_operation_context,
        )
    proposal = PipelineProposal.create(pipeline=pipeline, base=base, repair_count=0, skill_hash=stable_hash("skill"))
    plan = PipelinePlanResult(
        proposal=proposal,
        tool_call_id="vendor_call-string",
        custody_result="not_required",
        model_identifier="model",
        model_version="version",
        provider="test",
    )
    redacted = redact_tool_call_arguments("set_pipeline", proposal.pipeline, telemetry=NoopRedactionTelemetry())
    row = await service.create_pipeline_composition_proposal(
        session_id=sid,
        plan=plan,
        summary="Build",
        rationale="Requested",
        affects=("graph",),
        arguments_redacted_json=redacted,
        actor="composer-web:user:alice",
        composer_model_identifier="model",
        composer_model_version="version",
        composer_provider="test",
        user_message_id=user.message.id,
        session_operation_context=running.session_operation_context,
    )
    audit = begin_dispatch(row.tool_call_id, "set_pipeline", proposal.pipeline, version_before=0, actor="composer-web:user:alice")
    content_hash = _composition_state_data_content_hash(state)
    invocation = finish_success(
        audit,
        result_payload={
            "success": True,
            "validation": {
                "is_valid": True,
                "errors": [],
                "warnings": [],
                "suggestions": [],
                "semantic_contracts": [],
                "graph_repair_suggestions": [],
            },
            "affected_nodes": [],
            "version": 1,
            "pipeline_content_hash_schema": "composer.pipeline-dispatch-result.v1",
            "pipeline_content_hash": content_hash,
        },
        version_after=1,
    )
    (dispatch,) = await _persist_tool_invocations(
        service,
        sid,
        (invocation,),
        None,
        plugin_crash_pending=False,
        required_audit=True,
        session_operation_context=running.session_operation_context,
    )
    drafts = ()
    if with_review:
        drafts = (
            PreparedInterpretationEventDraft(
                event_id=uuid4(),
                affected_node_id="llm_transform_1",
                tool_call_id="backend_auto_surface:" + str(uuid4()),
                user_term="cool",
                kind=InterpretationKind.VAGUE_TERM,
                llm_draft="polished",
                surface_origin=InterpretationSurfaceOrigin.COMPOSER_LLM,
                model_identifier="model",
                model_version="version",
                provider="test",
                composer_skill_hash=stable_hash("skill"),
            ),
        )
    arguments = {
        "session_id": sid,
        "proposal_id": row.id,
        "draft_hash": proposal.draft_hash,
        "state": state,
        "candidate_content_hash": content_hash,
        "executor_content_hash": content_hash,
        "final_composer_metadata": None,
        "dispatch": dispatch,
        "actor": "user:alice",
        "transition_assistant": TransitionAssistantDraft(content="Built", raw_content=None),
        "session_operation_context": running.session_operation_context,
        "prepared_interpretations": drafts,
        "running": running,
    }
    return row, arguments, drafts, record, running


@pytest.mark.asyncio
async def test_atomic_pipeline_replay_has_no_dml_and_ignores_later_head(operation_store):
    engine, repository, authority, service, sid = operation_store
    row, arguments, drafts, record, running = await _prepared_pipeline(operation_store)
    result = await service.settle_pipeline_composition_proposal(**arguments)
    assert result.accepted_state == result.state and result.transition_message is not None
    await service.save_composition_state(
        sid,
        CompositionStateData(sources={}, nodes=[], edges=[], outputs=[], metadata_={"name": "Later", "description": ""}),
        provenance="tool_call",
        session_operation_context=running.session_operation_context,
    )
    authority.request_cancel(
        session_id=sid,
        operation_id=record.operation_id,
        cancelled_failure=lambda request_id: pytest.fail("running Stop must not settle queued path"),
    )
    restored = await service.get_authoritative_pipeline_proposal(session_id=sid, proposal_id=row.id)
    statements = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split(None, 1)[0].upper())

    event.listen(engine, "before_cursor_execute", observe)
    try:
        replay = await service.replay_pipeline_composition_proposal(authority=restored, prepared_interpretations=drafts)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert replay == result
    assert "SELECT" in statements and not set(statements) & {"INSERT", "UPDATE", "DELETE"}
    repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault_site", ["review", "assistant", "accepted"])
async def test_atomic_pipeline_review_fault_rolls_back_candidate_and_assistant(operation_store, fault_site):
    engine, repository, _authority, service, _sid = operation_store
    _row, arguments, _drafts, _record, running = await _prepared_pipeline(operation_store, with_review=True)

    def fail_review(conn, cursor, statement, parameters, context, executemany):
        table = {"review": "interpretation_events", "assistant": "chat_messages", "accepted": "proposal_events"}[fault_site]
        if statement.startswith("INSERT INTO " + table):
            raise OperationalError(statement, parameters, RuntimeError("injected atomic publication write"))

    event.listen(engine, "before_cursor_execute", fail_review)
    try:
        with pytest.raises(OperationalError):
            await service.settle_pipeline_composition_proposal(**arguments)
    finally:
        event.remove(engine, "before_cursor_execute", fail_review)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(composition_states_table)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(interpretation_events_table)).scalar_one() == 0
        assert (
            conn.execute(
                select(func.count()).select_from(chat_messages_table).where(chat_messages_table.c.role == "assistant")
            ).scalar_one()
            == 0
        )
    repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_receipt", [False, True])
async def test_atomic_pipeline_review_is_candidate_bound(operation_store, with_receipt):
    _engine, repository, _authority, service, sid = operation_store
    row, arguments, drafts, _record, running = await _prepared_pipeline(operation_store, with_review=True)
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
    publication = coordinator.reserve(RequiredWorkSource.PIPELINE_PUBLICATION_SQL) if with_receipt else None
    arguments["required_work"] = publication
    result = await service.settle_pipeline_composition_proposal(**arguments)
    if publication is not None:
        assert publication.complete and publication.receipts() == ()
    assert len(result.interpretation_events) == 1
    assert result.interpretation_events[0].id == drafts[0].event_id
    assert result.interpretation_events[0].composition_state_id == result.accepted_state.id
    restored = await service.get_authoritative_pipeline_proposal(session_id=sid, proposal_id=row.id)
    replay = coordinator.reserve(RequiredWorkSource.POSTCOMMIT_REVIEW_READ_SQL) if with_receipt else None
    assert (
        await service.replay_pipeline_composition_proposal(authority=restored, prepared_interpretations=drafts, required_work=replay)
        == result
    )
    if replay is not None:
        assert replay.complete and replay.receipts() == ()
    with pytest.raises(AuditIntegrityError):
        await service.replay_pipeline_composition_proposal(authority=restored, prepared_interpretations=())
    repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["candidate", "proposal", "source"])
async def test_atomic_pipeline_required_ticket_refusal_is_completed_without_sql(operation_store, mismatch):
    engine, repository, _authority, service, _sid = operation_store
    row, arguments, _drafts, _record, running = await _prepared_pipeline(operation_store)
    scope = RequiredWorkAuthority(
        RequiredAuthorityKind.DURABLE_COMPOSE,
        running.session_operation_context,
        durable_operation_id=running.claim.operation_id,
        claim_attempt=running.claim.attempt,
        proposal_id=str(uuid4()) if mismatch == "proposal" else str(row.id),
        tool_call_id=row.tool_call_id,
    )
    coordinator = RequiredWorkCoordinator(scope)
    ticket = coordinator.reserve(RequiredWorkSource.INGRESS_SQL if mismatch == "source" else RequiredWorkSource.PIPELINE_PUBLICATION_SQL)
    arguments["required_work"] = ticket
    if mismatch == "candidate":
        arguments["candidate_content_hash"] = "0" * 64
    statements = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        with pytest.raises(AuditIntegrityError):
            await service.settle_pipeline_composition_proposal(**arguments)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert ticket.complete and len(ticket.receipts()) == 1
    assert statements == []
    repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", [False, True])
async def test_terminal_failure_required_coordinator_observes_each_actual_attempt(operation_store, monkeypatch, retry):
    import elspeth.web.sessions.service as service_module

    _engine, repository, authority, service, sid = operation_store
    record, running = start(operation_store)
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE,
            running.session_operation_context,
            durable_operation_id=running.claim.operation_id,
            claim_attempt=running.claim.attempt,
        )
    )
    original = service_module.settle_composer_operation_on_connection
    outcomes = []

    def lose_first_ack(*args, **kwargs):
        result = original(*args, **kwargs)
        outcomes.append(result)
        if retry and len(outcomes) == 1:
            raise RuntimeError("injected rolled-back terminal acknowledgment")
        return result

    monkeypatch.setattr(service_module, "settle_composer_operation_on_connection", lose_first_ack)
    projection = coordinator.reserve(
        RequiredWorkSource.TERMINAL_FAILURE_PROJECTION,
        transition_ordinal=0,
        semantic_ordinal=0,
        recurrence_ordinal=0,
    )
    terminal = await service.fail_composer_async_operation(
        running, failure=failed(), required_work=coordinator, failure_projection_work=projection
    )
    projection.begin_projection()
    assert terminal.status == "failed" and authority.get(session_id=sid, operation_id=record.operation_id) == terminal
    projection.complete_owned()
    coordinator.assert_completed()
    terminal_tickets = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.TERMINAL_FAILURE_SQL]
    assert [ticket.key.sql_attempt_ordinal for ticket in terminal_tickets] == ([0, 1] if retry else [0])
    reads = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.TERMINAL_WRITER_READ_SQL]
    assert len(reads) == int(retry)
    assert len(coordinator.failure_receipts()) == int(retry)
    repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [RequiredWorkSource.COMPOSE_CHECKPOINT_SQL, RequiredWorkSource.RECOVERY_PARTIAL_STATE_SQL])
async def test_state_checkpoint_required_ticket_observes_actual_completion(operation_store, source):
    _engine, repository, _authority, service, sid = operation_store
    _record, running = start(operation_store)
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE,
            running.session_operation_context,
            durable_operation_id=running.claim.operation_id,
            claim_attempt=running.claim.attempt,
        )
    )
    ticket = coordinator.reserve(source)
    state = await service.save_composition_state(
        sid,
        CompositionStateData(sources={}, nodes=[], edges=[], outputs=[], metadata_={"name": "Checkpoint", "description": ""}),
        provenance="tool_call",
        session_operation_context=running.session_operation_context,
        required_work=ticket,
    )
    assert state.session_id == sid and ticket.complete and ticket.receipts() == ()
    assert (await service.get_current_state(sid)).id == state.id
    repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_source", [False, True])
async def test_title_required_ticket_has_exact_source_and_actual_completion(operation_store, wrong_source):
    _engine, repository, _authority, service, sid = operation_store
    _record, running = start(operation_store)
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE,
            running.session_operation_context,
            durable_operation_id=running.claim.operation_id,
            claim_attempt=running.claim.attempt,
        )
    )
    ticket = coordinator.reserve(RequiredWorkSource.COMPOSE_CHECKPOINT_SQL if wrong_source else RequiredWorkSource.TITLE_ACCOUNTING_SQL)
    original = await service.get_session(sid)
    if wrong_source:
        with pytest.raises(AuditIntegrityError, match="source/session mismatch"):
            await service.update_session_title(
                sid, "Required title", session_operation_context=running.session_operation_context, required_work=ticket
            )
        assert (await service.get_session(sid)).title == original.title
        assert ticket.complete and len(ticket.receipts()) == 1
    else:
        result = await service.update_session_title(
            sid, "Required title", session_operation_context=running.session_operation_context, required_work=ticket
        )
        assert result.title == "Required title" and ticket.complete and ticket.receipts() == ()
        assert (await service.get_session(sid)).title == result.title
    repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_atomic_opt_out_binds_candidate_derived_head_and_assistant(operation_store):
    _engine, repository, _authority, service, sid = operation_store
    row, arguments, drafts, _record, running = await _prepared_pipeline(operation_store, with_review=True, opted_out=True)
    result = await service.settle_pipeline_composition_proposal(**arguments)
    assert result.accepted_state.id != result.state.id
    assert result.state.derived_from_state_id == result.accepted_state.id
    assert result.transition_message.composition_state_id == result.state.id
    assert result.interpretation_events[0].choice.value == "opted_out"
    assert result.interpretation_events[0].composition_state_id == result.accepted_state.id
    restored = await service.get_authoritative_pipeline_proposal(session_id=sid, proposal_id=row.id)
    assert await service.replay_pipeline_composition_proposal(authority=restored, prepared_interpretations=drafts) == result
    repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_atomic_candidate_supersedes_older_identical_pending_review(operation_store):
    _engine, repository, _authority, service, sid = operation_store
    _row, arguments, drafts, _record, running = await _prepared_pipeline(operation_store, with_review=True, older_review=True)
    (old,) = await service.list_interpretation_events(sid)
    result = await service.settle_pipeline_composition_proposal(**arguments)
    events = await service.list_interpretation_events(sid)
    retired = next(item for item in events if item.id == old.id)
    assert retired.choice.value == "superseded" and retired.resolved_at is not None
    assert retired.composition_state_id == old.composition_state_id and retired.tool_call_id == old.tool_call_id
    assert retired.created_at == old.created_at and retired.user_term == old.user_term and retired.llm_draft == old.llm_draft
    assert retired.accepted_value is None and retired.arguments_hash is None
    (pending,) = await service.list_interpretation_events(sid, status="pending")
    assert pending.id == drafts[0].event_id and pending.id != old.id
    assert pending.composition_state_id == result.accepted_state.id
    repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", ["stop", "deadline"])
@pytest.mark.parametrize("identity", ["exact", "actor", "attempt"])
async def test_sealed_trust_revocation_crosses_signal_only_for_exact_authority(operation_store, monkeypatch, signal, identity):
    import elspeth.web.coordination.composer_operation_authority as authority_module

    engine, repository, authority, service, sid = operation_store
    row, arguments, _drafts, record, running = await _prepared_pipeline(operation_store, deadline_seconds=10.0)
    await service.update_composer_preferences(sid, trust_mode="auto_commit", density_default="medium", actor="user:alice")
    await service.update_composer_preferences(sid, trust_mode="explicit_approve", density_default="medium", actor="user:alice")
    arguments["required_trust_mode"] = "auto_commit"
    if signal == "stop":
        authority.request_cancel(
            session_id=sid,
            operation_id=record.operation_id,
            cancelled_failure=lambda request_id: pytest.fail("running Stop must retain its owned action"),
        )
    else:
        current = authority.get(session_id=sid, operation_id=record.operation_id)
        monkeypatch.setattr(authority_module, "database_now", lambda conn: current.deadline_at + timedelta(microseconds=1))
    if identity == "actor":
        arguments["actor"] = "user:mallory"
    elif identity == "attempt":
        arguments["running"] = replace(running, claim=replace(running.claim, attempt=running.claim.attempt + 1))
    with engine.connect() as conn:
        before = tuple(
            conn.execute(select(func.count()).select_from(table)).scalar_one()
            for table in (composition_states_table, interpretation_events_table, chat_messages_table)
        )
    expected = TrustModeAutoCommitRevokedError if identity == "exact" else (AuditIntegrityError, ComposerOperationFenceLost)
    with pytest.raises(expected):
        await service.settle_pipeline_composition_proposal(**arguments)
    if identity == "exact":
        with pytest.raises(TrustModeAutoCommitRevokedError):
            await service.settle_pipeline_composition_proposal(**arguments)
    with engine.connect() as conn:
        after = tuple(
            conn.execute(select(func.count()).select_from(table)).scalar_one()
            for table in (composition_states_table, interpretation_events_table, chat_messages_table)
        )
        revocations = conn.execute(
            select(proposal_events_table).where(
                proposal_events_table.c.proposal_id == str(row.id), proposal_events_table.c.event_type == "auto_commit.revoked"
            )
        ).all()
    assert before == after
    assert len(revocations) == int(identity == "exact")
    if revocations:
        payload = ComposerRevocationEvidence.model_validate(revocations[0].payload)
        assert payload.composer_operation.operation_id == record.operation_id
        assert payload.composer_operation.attempt == running.claim.attempt
        assert payload.tool_call_id == row.tool_call_id
    assert (await service.get_authoritative_pipeline_proposal(session_id=sid, proposal_id=row.id)).row.status == "pending"
    repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_opt_out_initial_arguments_domain_is_verified_not_only_echoed(operation_store):
    engine, repository, _authority, service, _sid = operation_store
    row, arguments, _drafts, _record, running = await _prepared_pipeline(operation_store, with_review=True, opted_out=True)
    result = await service.settle_pipeline_composition_proposal(**arguments)
    with engine.connect() as conn:
        payload = conn.execute(
            select(proposal_events_table.c.payload).where(
                proposal_events_table.c.proposal_id == str(row.id), proposal_events_table.c.event_type == "proposal.accepted"
            )
        ).scalar_one()
    accepted = PipelineAcceptedEvidence.model_validate_json(json.dumps(payload))
    (member,) = accepted.review_cohort
    verify_review_event_material(member, result.interpretation_events[0])
    corrupted = member.model_dump(mode="json")
    corrupted["initial_resolution"]["arguments_hash"] = "0" * 64
    corrupted["initial_resolution_hash"] = stable_hash(corrupted["initial_resolution"])
    altered_member = ReviewCohortMember.model_validate_json(json.dumps(corrupted))
    altered_event = replace(result.interpretation_events[0], arguments_hash="0" * 64)
    with pytest.raises(AuditIntegrityError):
        verify_review_event_material(altered_member, altered_event)
    repository.release(running.session_operation_context)
