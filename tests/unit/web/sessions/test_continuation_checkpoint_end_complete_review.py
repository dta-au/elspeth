"""Paid V4 END Review remains a semantic action until a later paid Stop."""

from __future__ import annotations

from hashlib import sha256

import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_checkpoint_end_terminal import EndCheckpointCompleteReview
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_cost import read_continuation_cost_snapshot_on_connection
from elspeth.web.sessions.continuation_semantic_review_result import read_original_semantic_review_result_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
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
        preflight = review.result.runtime_preflight
        assert preflight is not None and preflight.is_valid is False
        assert [(check.name, check.passed) for check in preflight.checks] == [("advisor_signoff", False)]
        assert [error.error_code for error in preflight.errors] == ["advisor_signoff_blocked"]
        assert preflight.readiness.authoring_valid is False
        assert preflight.readiness.execution_ready is False
        assert preflight.readiness.completion_ready is False
        assert [blocker.code for blocker in preflight.readiness.blockers] == ["advisor_signoff_blocked"]
        assert review.result.pipeline_commit_intent is None
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


@pytest.mark.asyncio
async def test_actual_paid_end_review_refuses_order_only_completed_frontier_corruption(operation_store, tmp_path, monkeypatch):
    from tests.unit.web.sessions.test_continuation_primary_result import stored_rows

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
        assert type(case["execution"]) is EndCheckpointCompleteReview
        review = case["execution"].review
        assert review.outcome == "blocked" and review.original_source == case["admission"].paid.source
        running = case["running"]
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        before = stored_rows(store[0])
        with locked_session_transaction(store[0], sid) as conn:
            assert (
                read_original_semantic_review_result_on_connection(
                    conn, session_id=running.claim.session_id, operation_id=operation, review_effect_key=review.owner_effect_key
                )
                == review
            )
            attempt_id = review.result.llm_calls[-1].call_id
            hold = conn.execute(select(holds).where(holds.c.provider_attempt_id == attempt_id)).one()
            current = load_snapshot(conn, session_id=sid, operation_id=operation)
            assert current == review.completed
            root = decode_continuation_snapshot(current.payload_json)
            (owner_index,) = tuple(i for i, item in enumerate(root.effect_frontier) if item.effect_id == review.owner_effect_key)
            (child_index,) = tuple(i for i, item in enumerate(root.effect_frontier) if item.effect_id == hold.effect_key)
            assert owner_index != child_index
            assert root.effect_frontier[owner_index].disposition == root.effect_frontier[child_index].disposition == "committed"
            reordered = list(root.effect_frontier)
            reordered[owner_index], reordered[child_index] = reordered[child_index], reordered[owner_index]
            changed = root.model_copy(update={"effect_frontier": tuple(reordered)})
            assert changed.effect_frontier != root.effect_frontier
            assert changed.model_copy(update={"effect_frontier": root.effect_frontier}) == root
            encoded = encode_continuation_snapshot(changed)
            assert decode_continuation_snapshot(encoded) == changed
            payload = encoded.encode("utf-8")
            assert len(payload) == len(current.payload_json.encode("utf-8"))
            key = conn.execute(
                select(checkpoints.c.snapshot_chunk_key).where(
                    checkpoints.c.session_id == sid,
                    checkpoints.c.operation_id == operation,
                    checkpoints.c.checkpoint_id == current.checkpoint_id,
                )
            ).scalar_one()
            assert key == f"snapshot:{current.checkpoint_id}"
            changed_storage = conn.begin_nested()
            try:
                updated = conn.execute(
                    update(chunks)
                    .where(chunks.c.session_id == sid, chunks.c.operation_id == operation, chunks.c.chunk_key == key)
                    .values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
                )
                assert updated.rowcount == 1
                with pytest.raises(AuditIntegrityError, match=r"^paid END Review changed its complete committed successor root$"):
                    read_original_semantic_review_result_on_connection(
                        conn, session_id=running.claim.session_id, operation_id=operation, review_effect_key=review.owner_effect_key
                    )
            finally:
                changed_storage.rollback()
            assert (
                read_original_semantic_review_result_on_connection(
                    conn, session_id=running.claim.session_id, operation_id=operation, review_effect_key=review.owner_effect_key
                )
                == review
            )
            assert load_snapshot(conn, session_id=sid, operation_id=operation) == current
        assert stored_rows(store[0]) == before
        assert tuple(name for name, _ in case["sdk"]) == ("semantic", "end")


@pytest.mark.asyncio
async def test_actual_clean_empty_review_keeps_warm_and_cold_projection_truthful(operation_store, tmp_path, monkeypatch):
    from dataclasses import replace

    from elspeth.web.composer import continuation_checkpoint_end_complete_review as review_projection
    from elspeth.web.composer.advisor_policy import advisor_signoff_unverified_validation
    from elspeth.web.composer.composer_preflight import _state_contains_blob_ref
    from elspeth.web.composer.continuation_checkpoint_end_cold_consequence import (
        compute_first_cold_checkpoint_end_gate,
        read_cold_checkpoint_end_consequence_source,
    )
    from elspeth.web.composer.continuation_checkpoint_end_cold_review import ColdProjectedEndReview, project_first_cold_end_review
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
    from elspeth.web.composer.continuation_selection import load_selected_continuation
    from elspeth.web.composer.no_tool_policy import state_is_structurally_empty
    from elspeth.web.composer.provider_quota import ProviderInvocationOwner
    from elspeth.web.composer.state import EdgeSpec, OutputSpec, SourceSpec
    from elspeth.web.execution.schemas import ValidationResult
    from tests.unit.web.sessions.test_continuation_v4_pipeline_live import _uploaded_source

    policy = continuation_policy_v4_from_settings(
        continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
    )
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    monkeypatch.setattr(initialization_fixture, "minimal_continuation_policy", lambda: policy)
    blob = await _uploaded_source(store)
    actual_project = review_projection.project_end_complete_clean
    projections = []

    async def paired_project(*, physical, returned, computation):
        phases = physical.phase_owner.phases
        running = phases.work.running
        assert running is not None and computation.action == "fallthrough" and computation.publication_admitted
        selected = await load_selected_continuation(
            authority=phases.authority,
            running=running,
            selected=phases.authority.load_continuation_snapshot(running),
            required_work=phases.work,
        )
        provider = ProviderInvocationOwner(service=phases.service._require_sessions_service(), required_work=phases.work)
        cold_phases = ContinuationLivePhases(phases.service, phases.authority, phases.work, provider, phases.lease)
        source = await read_cold_checkpoint_end_consequence_source(phases=cold_phases, selected=selected)
        gate = await compute_first_cold_checkpoint_end_gate(source=source)
        live = source.runtime.live
        state = live.state
        assert state_is_structurally_empty(state) and not state.validate().is_valid
        assert live.last_runtime_preflight is None and source.end_input.runtime_preflight_json is None
        assert source.end_input.caller.proof_runtime_preflight_json is None
        assert source.end_input.orphan_events_json == "[]" and not source.end_input.orphaned_precheck
        assert not live.advisor_repair_context_introduced and not _state_contains_blob_ref(state)
        assert live.recorder is not physical.detached.live.recorder
        assert live.llm_messages is not physical.detached.live.llm_messages

        output = OutputSpec("unconnected", "json", {"path": "unconnected.json"}, "discard")
        output_only = replace(state, outputs=(output,))
        edge_only = replace(state, edges=(EdgeSpec("unconnected", "missing-source", "missing-output", "on_success", None),))
        blob_state = replace(state, sources={"uploaded": SourceSpec("csv", "unconnected", {"blob_ref": str(blob.id)}, "discard")})
        assert not output_only.validate().is_valid and not edge_only.validate().is_valid
        assert not state_is_structurally_empty(output_only) and not state_is_structurally_empty(edge_only)
        assert _state_contains_blob_ref(blob_state)
        for changed in (replace(state, version=state.version + 1), output_only, edge_only, blob_state):
            live.state = changed
            try:
                with pytest.raises(AuditIntegrityError, match="cold END Review needs an effectful preflight or interpretation consequence"):
                    await project_first_cold_end_review(gate=gate)
            finally:
                live.state = state
        blocked_prior = advisor_signoff_unverified_validation(
            reason="flagged_final_pass", findings="Earlier review did not clear.", category="other", step_ids=(), note=None
        )
        assert type(blocked_prior) is ValidationResult and blocked_prior.is_valid is False
        assert blocked_prior.readiness.authoring_valid is False
        assert blocked_prior.readiness.execution_ready is False
        assert blocked_prior.readiness.completion_ready is False
        live.last_runtime_preflight = blocked_prior
        try:
            with pytest.raises(AuditIntegrityError, match="cold END Review needs an effectful preflight or interpretation consequence"):
                await project_first_cold_end_review(gate=gate)
        finally:
            live.last_runtime_preflight = None
        live.advisor_repair_context_introduced = True
        try:
            with pytest.raises(AuditIntegrityError, match="cold END Review needs an effectful preflight or interpretation consequence"):
                await project_first_cold_end_review(gate=gate)
        finally:
            live.advisor_repair_context_introduced = False

        cold = await project_first_cold_end_review(gate=gate)
        assert type(cold) is ColdProjectedEndReview
        warm = await actual_project(physical=physical, returned=returned, computation=computation)
        assert cold.execution.disposition == warm.disposition
        assert cold.execution.publication_binding == computation.publication_review.binding
        assert cold.execution.public_result.state.to_dict() == warm.result.state.to_dict() == state.to_dict()
        assert cold.execution.public_result.graph_review_identity == warm.result.graph_review_identity
        for result in (cold.execution.public_result, warm.result):
            assert result.runtime_preflight is None and result.advisor_gate_decision is None
            assert result.pipeline_commit_intent is None and result.graph_recovery_intent is None and not result.recovery_audit_drafts
            assert result.graph_review_identity is not None and result.graph_review_identity.source == "model"
            assert result.graph_review_identity.binding == computation.publication_review.binding
            assert result.llm_calls[-1].call_id == returned.audit_call.call_id == source.owed.attempt_id
        projections.append((cold, warm))
        return warm

    monkeypatch.setattr(review_projection, "project_end_complete_clean", paired_project)
    async with shared_fixture.actual_shared_end_case(store, tmp_path, monkeypatch, timed_read=True) as case:
        assert type(case["execution"]) is EndCheckpointCompleteReview and len(projections) == 1
        review = case["execution"].review
        assert review.outcome == "clean" and review.original_source == case["admission"].paid.source
        assert review.result.runtime_preflight is None and review.result.advisor_gate_decision is None
        assert review.result.pipeline_commit_intent is None and review.result.graph_recovery_intent is None
        assert tuple(name for name, _ in case["sdk"]) == ("semantic", "end")
        running = case["running"]
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        with locked_session_transaction(store[0], sid) as conn:
            job = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
            assert job.status == "running" and job.result_json is None
            assert tuple(conn.execute(select(messages.c.id).where(messages.c.role == "assistant"))) == case["before_assistants"]
            assert (
                read_original_semantic_review_result_on_connection(
                    conn, session_id=running.claim.session_id, operation_id=operation, review_effect_key=review.owner_effect_key
                )
                == review
            )
