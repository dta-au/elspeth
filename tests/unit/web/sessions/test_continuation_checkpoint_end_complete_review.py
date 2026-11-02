"""Paid V4 END Review remains a semantic action until a later paid Stop."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.continuation_checkpoint_end_complete_review import (
    ProjectedEndCompleteReview,
    project_end_complete_review,
)
from elspeth.web.composer.continuation_checkpoint_end_computation import compute_checkpoint_end_gate
from elspeth.web.composer.continuation_checkpoint_end_physical import (
    checkpoint_end_physical_custody,
    observe_checkpoint_end_physical_return,
    prepare_checkpoint_end_physical_source,
)
from elspeth.web.composer.continuation_checkpoint_end_terminal import (
    EndCheckpointCompleteReview,
    _EndCheckpointCompleteReviewControl,
)
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_checkpoint_end_complete_review import persist_end_complete_review_on_connection
from elspeth.web.sessions.continuation_checkpoint_end_owed import retain_v4_checkpoint_end_owed_on_connection
from elspeth.web.sessions.continuation_semantic_review_result import read_original_semantic_review_result_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as attempts
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.sessions import test_continuation_end_shared_execution as shared_fixture
from tests.unit.web.sessions import test_continuation_initialization as initialization_fixture
from tests.unit.web.sessions import test_continuation_semantic_advisor as semantic_fixture

operation_store = shared_fixture.operation_store


@pytest.mark.asyncio
async def test_actual_v4_paid_end_review_retains_blocked_action_without_early_publication(operation_store, tmp_path, monkeypatch):
    policy = continuation_policy_v4_from_settings(
        continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
    )
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    monkeypatch.setattr(initialization_fixture, "minimal_continuation_policy", lambda: policy)
    original = AdvisorCheckpointOwner._run_advisor_checkpoint
    original_settings = semantic_fixture._required_guard_settings

    def last_pass_settings(tmp_path, monkeypatch):
        settings, requests = original_settings(tmp_path, monkeypatch)
        return settings.model_copy(update={"composer_advisor_checkpoint_max_passes": 1}), requests

    monkeypatch.setattr(semantic_fixture, "_required_guard_settings", last_pass_settings)

    async def checkpoint(self, **arguments):
        phase_owner = arguments["checkpoint_phase_owner"]
        gate_locals = arguments["checkpoint_end_input"]
        if phase_owner is None or gate_locals is None:
            return await original(self, **arguments)
        source = await prepare_checkpoint_end_physical_source(phase_owner=phase_owner, gate_locals=gate_locals)
        custody = checkpoint_end_physical_custody(source)
        prepared = custody.continuation_checkpoint_v4
        admission = custody.continuation_checkpoint_v4_admission
        assert prepared is not None and admission is not None
        bindings = phase_owner.runtime.bindings
        from elspeth.web.composer.content_scope import composition_scope_for_operation

        scope = composition_scope_for_operation(
            phase_owner.runtime.live.state,
            session_id=bindings.session_id,
            session_operation_context=bindings.session_operation_context,
            session_operation_authority=self._require_sessions_service().session_operation_authority,
        )
        request = self._build_checkpoint_arguments(
            phase="end",
            state=phase_owner.runtime.live.state,
            content_scope=scope,
            env_ref_names=phase_owner.runtime.env_ref_names,
            user_message=bindings.message,
            advisor_review_state=gate_locals.review_state,
            publication_review=gate_locals.publication_review,
        )
        guidance, _metadata = await self._call_advisor_with_audit(
            request,
            recorder=phase_owner.runtime.live.recorder,
            provider_owner=phase_owner.phases.provider_owner,
            structured_output=True,
            _outcome=source.capture,
            checkpoint_end_source=source,
        )
        assert '"FLAGGED"' in guidance
        returned = observe_checkpoint_end_physical_return(source)
        running = prepared.running
        sid = str(running.claim.session_id)
        with locked_session_transaction(store[0], sid) as conn:
            owed = retain_v4_checkpoint_end_owed_on_connection(
                conn, running=running, prepared=prepared, admission=admission, returned=returned
            )
        selected = await load_selected_continuation(
            authority=phase_owner.phases.authority,
            running=running,
            selected=owed.owed_selected,
            required_work=phase_owner.phases.work,
        )
        computation = compute_checkpoint_end_gate(source=source, returned=returned)
        projected = await project_end_complete_review(
            physical=source, owed=owed, selected=selected, returned=returned, computation=computation
        )
        assert type(projected) is ProjectedEndCompleteReview
        with locked_session_transaction(store[0], sid) as conn:
            review = persist_end_complete_review_on_connection(
                conn,
                service=self._require_sessions_service(),
                physical=source,
                prepared=prepared,
                owed_admission=owed,
                selected=selected,
                returned=returned,
                projected=projected,
            )
        raise _EndCheckpointCompleteReviewControl(EndCheckpointCompleteReview(review))

    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", checkpoint)
    flagged = shared_fixture._end_admit()
    flagged.update(verdict="FLAGGED", findings="The candidate does not yet meet the request.", publication_verdict="WITHHOLD")
    monkeypatch.setattr(shared_fixture, "_end_admit", lambda: flagged)
    async with shared_fixture.actual_shared_end_case(store, tmp_path, monkeypatch, outcome="clean", timed_read=True) as case:
        completed = case["execution"]
        assert type(completed) is EndCheckpointCompleteReview
        review = completed.review
        assert review.outcome == "blocked" and review.publication_binding is None
        assert review.original_source == case["admission"].paid.source
        sid, operation = str(case["running"].claim.session_id), case["running"].claim.operation_id
        with locked_session_transaction(store[0], sid) as conn:
            job = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
            attempt = conn.execute(select(attempts).where(attempts.c.attempt_id == review.result.llm_calls[-1].call_id)).one()
            hold = conn.execute(select(holds).where(holds.c.provider_attempt_id == attempt.attempt_id)).one()
            assert job.status == "running" and job.result_json is None
            assert attempt.settled_at is not None and hold.state == "consumed"
            current = load_snapshot(conn, session_id=sid, operation_id=operation)
            assert current == review.completed
            assert decode_continuation_snapshot(current.payload_json).frames[-1].phase == "repair_needed"
            assert (
                read_original_semantic_review_result_on_connection(
                    conn,
                    session_id=case["running"].claim.session_id,
                    operation_id=operation,
                    review_effect_key=review.owner_effect_key,
                )
                == review
            )
            with pytest.raises(AuditIntegrityError):
                read_original_semantic_review_result_on_connection(
                    conn,
                    session_id=case["running"].claim.session_id,
                    operation_id=operation,
                    review_effect_key="foreign-end-review",
                )
