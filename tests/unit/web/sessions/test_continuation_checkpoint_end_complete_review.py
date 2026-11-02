"""Paid V4 END Review remains a semantic action until a later paid Stop."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_checkpoint_end_terminal import EndCheckpointCompleteReview
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_cost import read_continuation_cost_snapshot_on_connection
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
    original_settings = semantic_fixture._required_guard_settings

    def last_pass_settings(tmp_path, monkeypatch):
        settings, requests = original_settings(tmp_path, monkeypatch)
        return settings.model_copy(update={"composer_advisor_checkpoint_max_passes": 1}), requests

    monkeypatch.setattr(semantic_fixture, "_required_guard_settings", last_pass_settings)

    flagged = shared_fixture._end_admit()
    flagged.update(verdict="FLAGGED", findings="The candidate does not yet meet the request.", publication_verdict="WITHHOLD")
    monkeypatch.setattr(shared_fixture, "_end_admit", lambda: flagged)
    async with shared_fixture.actual_shared_end_case(store, tmp_path, monkeypatch, outcome="clean", timed_read=True) as case:
        completed = case["execution"]
        assert type(completed) is EndCheckpointCompleteReview
        review = completed.review
        assert review.outcome == "blocked" and review.publication_binding is None
        assert not review.result.state.sources and not review.result.state.nodes and not review.result.state.outputs
        assert review.result.runtime_preflight is None and review.result.pipeline_commit_intent is None
        assert review.original_source == case["admission"].paid.source
        sid, operation = str(case["running"].claim.session_id), case["running"].claim.operation_id
        budget = case["phases"].provider_owner.cost_budget
        assert budget is not None
        with locked_session_transaction(store[0], sid) as conn:
            job = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
            attempt = conn.execute(select(attempts).where(attempts.c.attempt_id == review.result.llm_calls[-1].call_id)).one()
            hold = conn.execute(select(holds).where(holds.c.provider_attempt_id == attempt.attempt_id)).one()
            assert job.status == "running" and job.result_json is None
            assert attempt.settled_at is not None and hold.state == "consumed"
            current = load_snapshot(conn, session_id=sid, operation_id=operation)
            durable_cost = read_continuation_cost_snapshot_on_connection(conn, decode_continuation_snapshot(current.payload_json))
            assert budget.snapshot() == durable_cost
            assert budget.snapshot().physical_calls == len(budget.snapshot().charges)
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
