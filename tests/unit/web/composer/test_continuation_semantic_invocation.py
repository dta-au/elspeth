"""Private source-to-action identity; coherent DATA variants grant no SQL use."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import UUID

import pytest

from elspeth.contracts.composer_content import ComposerContentScope
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.advisor_output import ContinuationAdvisorDecision, ContinuationEvidenceRef, ReadContinuationAction
from elspeth.web.composer.continuation_advisor import nearest_compose_frame
from elspeth.web.composer.continuation_codec import (
    decode_continuation_snapshot,
    decode_frontier_chunk,
    encode_continuation_snapshot,
    encode_private_chunk,
)
from elspeth.web.composer.continuation_iteration import decode_continuation_iteration, encode_continuation_iteration
from elspeth.web.composer.continuation_message_shelf import MessageShelfSnapshot, encode_message_shelf
from elspeth.web.composer.continuation_progress import ActionExecutionReceipt, ContinuationProtocolError, continuation_action_key
from elspeth.web.composer.continuation_semantic_advisor import prepare_semantic_advisor_request
from elspeth.web.composer.continuation_semantic_source import capture_semantic_advisor_source
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.turn_effects import authored_state_hash
from tests.unit.web.composer.test_continuation_semantic_source import _with_frontier
from tests.unit.web.sessions.test_continuation_composition_initialization import construct, ready_case
from tests.unit.web.sessions.test_continuation_composition_initialization import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows

operation_store = _operation_store


@asynccontextmanager
async def _initialized_invocation_data(store, tmp_path, monkeypatch):
    """Use actual first-Compose DATA without entering a physical semantic action."""
    running, service, work, ready, sdk = await ready_case(store, tmp_path, monkeypatch)
    try:
        runtime = await construct(store, running, service, work, ready)
        assert not sdk and runtime.source.selected.stack.semantic_frontier is not None
        provider = runtime.bindings.provider_owner
        assert runtime.bindings.session_operation_context is running.session_operation_context
        inputs = {
            "owner": service._advisor_checkpoint,
            "provider_owner": provider,
            "state": runtime.live.state,
            "content_scope": ComposerContentScope(
                UUID(running.session_operation_context.fence.session_id), composition_content_hash(runtime.live.state)
            ),
            "user_message": runtime.bindings.message,
            "runtime": runtime,
            "env_ref_names": runtime.env_ref_names,
        }
        custodies = []
        actual_mint = provider.mint

        def mint(family):
            actual = actual_mint(family)
            custodies.append(actual)
            return actual

        with monkeypatch.context() as observe:
            observe.setattr(provider, "mint", mint)
            yield inputs, work.coordinator, custodies
    finally:
        store[1].release(running.session_operation_context)


def _evidence(content, *, provider=False, receipt="retained-read:1"):
    return ContinuationEvidenceRef(
        receipt_id=receipt,
        source_kind="provider" if provider else "read",
        content_hash=stable_hash(content),
        original_attempt_id="physical:1" if provider else None,
        coverage_binding="same-authoritative-read-scope",
    )


def _decision(data, *, call_id="actual-private-advisor:1"):
    premise = data.invocation.premise
    assert premise is not None
    return ContinuationAdvisorDecision(
        schema_version="composer.continuation-decision.v1",
        decision_id=call_id,
        decision="CONTINUE",
        scope="whole_request",
        obligation_ids=("request",),
        obligations=(),
        evidence=data.frontier.evidence,
        reason="Inspect the same authoritative source scope.",
        remaining_actionable_ids=("request",),
        prescribed_action=ReadContinuationAction(
            kind="read_tool",
            obligation_ids=("request",),
            evidence_ids=tuple(item.receipt_id for item in data.frontier.evidence),
            input_hash=premise.input_hash,
            graph_hash=data.frontier.graph_hash,
            policy_hash=premise.policy_hash,
            tool_manifest_hash=premise.tool_manifest_hash,
            tool_name="get_plugin_schema",
            arguments_json=canonical_json({"plugin_type": "source", "name": "csv"}),
        ),
    )


def _capture_with_history(capture):
    """A closed retained-source DATA variant, not a current SQL installation."""
    shelf = MessageShelfSnapshot.model_validate_json(capture.transcript.canonical)
    message = canonical_json({"role": "assistant", "content": "Additional provider history without a new source fact."})
    changed_shelf = shelf.model_copy(
        update={
            "messages_json": (*shelf.messages_json, message),
            "producer_recipes": (*shelf.producer_recipes, None) if shelf.producer_recipes else (),
        }
    )
    transcript = encode_message_shelf(changed_shelf, env_ref_names=capture.env_ref_names)
    evidence_body = json.loads(capture.evidence.canonical)
    evidence_body["transcript"] = changed_shelf.model_dump(mode="json")
    evidence = encode_private_chunk(
        codec="composer.evidence.v2", canonical=canonical_json(evidence_body), env_ref_names=capture.env_ref_names
    )
    iteration = decode_continuation_iteration(capture.iteration, env_ref_names=capture.env_ref_names)
    review = json.loads(iteration.advisor_review_json)
    review["completed_passes"] += 1
    iteration_chunk = encode_continuation_iteration(
        iteration.model_copy(update={"advisor_review_json": canonical_json(review)}), env_ref_names=capture.env_ref_names
    )
    root = decode_continuation_snapshot(capture.selected_snapshot_json)
    frame = nearest_compose_frame(root)
    messages = encode_private_chunk(
        codec="composer.messages.v1",
        canonical=canonical_json({"messages_json": changed_shelf.messages_json, "current_indexes": changed_shelf.current_indexes}),
        env_ref_names=capture.env_ref_names,
    )
    changed_frame = frame.model_copy(
        update={
            "transcript": transcript.reference,
            "evidence": evidence.reference,
            "iteration": iteration_chunk.reference,
            "messages": messages.reference,
        }
    )
    selected_json = encode_continuation_snapshot(
        root.model_copy(update={"frames": tuple(changed_frame if item is frame else item for item in root.frames)})
    )
    changed = replace(capture, transcript=transcript, evidence=evidence, iteration=iteration_chunk, selected_snapshot_json=selected_json)
    body = json.loads(capture.source_body_json)
    body["selected_snapshot_json"] = selected_json
    body["source_chunks"] = [
        {"reference": item.reference.model_dump(mode="json"), "canonical": item.canonical}
        for item in (changed.state, changed.evidence, changed.transcript, changed.iteration, changed.tools, changed.predecessor_frontier)
    ]
    return replace(changed, source_body_json=canonical_json(body))


@pytest.mark.asyncio
async def test_final_invocation_binds_exact_stable_request_and_existing_private_preparation(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_semantic_invocation import SemanticAdvisorInvocationData, make_semantic_advisor_invocation

    async with _initialized_invocation_data(operation_store, tmp_path, monkeypatch) as (inputs, coordinator, custodies):
        raw = capture_semantic_advisor_source(inputs["runtime"])
        before = stored_rows(operation_store[0])
        data = make_semantic_advisor_invocation(raw)
        assert type(data) is SemanticAdvisorInvocationData and data.source is raw
        data.verify(raw)
        expected = {
            "domain": "composer.semantic-request-input.v1",
            "logical": json.loads(raw.logical_json),
            "actor_user_id": raw.actor_user_id,
            "original_request_json": raw.original_request_json,
            "original_user_message": raw.original_user_message,
        }
        assert data.request_input_json == canonical_json(expected)
        assert data.invocation.premise.input_hash == stable_hash(expected)
        assert data.invocation.premise.frontier == raw.predecessor_frontier.reference
        assert data.frontier.graph_hash == authored_state_hash(inputs["runtime"].live.state)
        assert data.frontier.graph_hash != composition_content_hash(inputs["runtime"].live.state)
        assert data.invocation.candidate == raw.state.canonical
        assert data.invocation.state == raw.state.reference and data.invocation.evidence == raw.evidence.reference
        assert data.invocation.transcript == raw.transcript.reference and data.invocation.iteration == raw.iteration.reference
        policy = json.loads(data.policy_preimage_json)
        assert policy["actor_user_id"] == raw.actor_user_id
        assert policy["plugin_policy_hash"] == raw.plugin_policy_hash
        assert policy["catalog_snapshot_hash"] == raw.catalog_snapshot_hash
        assert policy["continuation_policy_hash"] == raw.admission_policy_hash
        assert data.invocation.premise.policy_hash == stable_hash(policy)
        assert data.invocation.premise.tool_manifest_hash == stable_hash(json.loads(raw.tool_manifest_json))
        prepared = prepare_semantic_advisor_request(**(inputs | {"source": data.invocation, "frontier": data.frontier}))
        assert prepared.source is data.invocation and prepared.request_sha256 != data.invocation.premise.input_hash
        assert stored_rows(operation_store[0]) == before and custodies == []
        coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ["committed", "refused"])
async def test_receipt_and_provider_history_cannot_reopen_unchanged_action(operation_store, tmp_path, monkeypatch, disposition):
    from elspeth.web.composer.continuation_semantic_invocation import make_semantic_advisor_invocation, semantic_advisor_action_key

    async with _initialized_invocation_data(operation_store, tmp_path, monkeypatch) as (inputs, coordinator, _custodies):
        runtime = inputs["runtime"]
        frontier = decode_frontier_chunk(capture_semantic_advisor_source(runtime).predecessor_frontier)
        fact = _evidence({"authoritative_field": "original value"})
        provider_fact = _evidence("original provider response", provider=True, receipt="provider-receipt:1")
        frontier = replace(frontier, evidence=(fact, provider_fact))
        first = make_semantic_advisor_invocation(capture_semantic_advisor_source(_with_frontier(runtime, frontier)))
        decision = _decision(first)
        first_key = semantic_advisor_action_key(
            first,
            decision,
            actual_advisor_call_id=decision.decision_id,
            permitted_read_tools=frozenset({"get_plugin_schema"}),
            complete_required_coverage=False,
        )
        receipt = ActionExecutionReceipt(first_key, "actual-terminal-owner-result", disposition)
        receipt_frontier = replace(frontier, action_receipts=(*frontier.action_receipts, receipt))
        receipt_only = make_semantic_advisor_invocation(capture_semantic_advisor_source(_with_frontier(runtime, receipt_frontier)))
        assert receipt_only.invocation.premise.input_hash == first.invocation.premise.input_hash
        assert receipt_only.invocation.coverage_binding != first.invocation.coverage_binding
        with pytest.raises(ContinuationProtocolError, match="already been prepared"):
            semantic_advisor_action_key(
                receipt_only,
                _decision(receipt_only),
                actual_advisor_call_id=decision.decision_id,
                permitted_read_tools=frozenset({"get_plugin_schema"}),
                complete_required_coverage=False,
            )
        newer_provider = _evidence("new provider-only response", provider=True, receipt="provider-receipt:2")
        evolved = replace(receipt_frontier, evidence=(fact, newer_provider))
        raw = capture_semantic_advisor_source(_with_frontier(runtime, evolved))
        history = make_semantic_advisor_invocation(_capture_with_history(raw))
        repeated = _decision(history)
        assert history.invocation.premise.input_hash == first.invocation.premise.input_hash
        assert history.invocation.coverage_binding != first.invocation.coverage_binding
        assert history.invocation.coverage_binding != receipt_only.invocation.coverage_binding
        assert (
            continuation_action_key(
                repeated.prescribed_action, retained_evidence={item.receipt_id: item for item in history.frontier.evidence}
            )
            == first_key
        )
        with pytest.raises(ContinuationProtocolError, match="already been prepared"):
            semantic_advisor_action_key(
                history,
                repeated,
                actual_advisor_call_id=repeated.decision_id,
                permitted_read_tools=frozenset({"get_plugin_schema"}),
                complete_required_coverage=False,
            )
        coordinator.assert_completed()


@pytest.mark.asyncio
async def test_changed_nonprovider_content_changes_action_key_without_relabeling_request(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_semantic_invocation import make_semantic_advisor_invocation, semantic_advisor_action_key

    async with _initialized_invocation_data(operation_store, tmp_path, monkeypatch) as (inputs, coordinator, _custodies):
        runtime = inputs["runtime"]
        frontier = decode_frontier_chunk(capture_semantic_advisor_source(runtime).predecessor_frontier)
        first_fact = _evidence({"authoritative_field": "original value"})
        frontier = replace(frontier, evidence=(first_fact,))
        first = make_semantic_advisor_invocation(capture_semantic_advisor_source(_with_frontier(runtime, frontier)))
        first_decision = _decision(first)
        key = semantic_advisor_action_key(
            first,
            first_decision,
            actual_advisor_call_id=first_decision.decision_id,
            permitted_read_tools=frozenset({"get_plugin_schema"}),
            complete_required_coverage=False,
        )
        changed_fact = _evidence({"authoritative_field": "materially changed value"})
        assert changed_fact.receipt_id == first_fact.receipt_id and changed_fact.coverage_binding == first_fact.coverage_binding
        assert changed_fact.content_hash != first_fact.content_hash
        updated = replace(
            frontier, evidence=(changed_fact,), action_receipts=(ActionExecutionReceipt(key, "prior-owner-result", "committed"),)
        )
        second = make_semantic_advisor_invocation(capture_semantic_advisor_source(_with_frontier(runtime, updated)))
        second_decision = _decision(second)
        second_key = semantic_advisor_action_key(
            second,
            second_decision,
            actual_advisor_call_id=second_decision.decision_id,
            permitted_read_tools=frozenset({"get_plugin_schema"}),
            complete_required_coverage=False,
        )
        assert second.invocation.premise.input_hash == first.invocation.premise.input_hash
        assert second_key != key and updated.action_receipts[0].action_key == key
        coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["candidate", "original_message", "request_json", "source_body", "foreign_capture", "invocation"])
async def test_final_invocation_refuses_original_source_tampering(operation_store, tmp_path, monkeypatch, change):
    from elspeth.web.composer.continuation_semantic_invocation import make_semantic_advisor_invocation

    async with _initialized_invocation_data(operation_store, tmp_path, monkeypatch) as (inputs, coordinator, _custodies):
        raw = capture_semantic_advisor_source(inputs["runtime"])
        data = make_semantic_advisor_invocation(raw)
        data.verify(raw)
        if change == "foreign_capture":
            with pytest.raises(AuditIntegrityError, match="original"):
                data.verify(replace(raw))
        elif change == "invocation":
            changed = replace(data, invocation=data.invocation.model_copy(update={"coverage_binding": stable_hash("foreign source")}))
            with pytest.raises(AuditIntegrityError):
                changed.verify(raw)
        else:
            updates = {
                "candidate": {"candidate": "a substituted candidate"},
                "original_message": {"original_user_message": "a substituted authenticated message"},
                "request_json": {"original_request_json": "{}"},
                "source_body": {"source_body_json": "{}"},
            }[change]
            with pytest.raises(AuditIntegrityError):
                make_semantic_advisor_invocation(replace(raw, **updates))
        coordinator.assert_completed()


@pytest.mark.asyncio
async def test_unselected_graph_proposal_cannot_become_final_invocation(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_semantic_invocation import make_semantic_advisor_invocation

    async with _initialized_invocation_data(operation_store, tmp_path, monkeypatch) as (inputs, coordinator, _custodies):
        runtime = inputs["runtime"]
        retained = decode_frontier_chunk(capture_semantic_advisor_source(runtime).predecessor_frontier)
        old_graph = _with_frontier(runtime, replace(retained, graph_hash=stable_hash("actual predecessor graph")))
        raw = capture_semantic_advisor_source(old_graph, propose_current_graph=True)
        assert raw.proposed_frontier is not None
        with pytest.raises(AuditIntegrityError, match="select"):
            make_semantic_advisor_invocation(raw)
        assert old_graph.source.selected.stack.semantic_frontier == raw.predecessor_frontier.reference
        coordinator.assert_completed()


@pytest.mark.asyncio
async def test_selected_frontier_refuses_a_different_graph_hash_domain(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_semantic_invocation import make_semantic_advisor_invocation

    async with _initialized_invocation_data(operation_store, tmp_path, monkeypatch) as (inputs, coordinator, _custodies):
        runtime = inputs["runtime"]
        raw = capture_semantic_advisor_source(runtime)
        actual = make_semantic_advisor_invocation(raw)
        assert actual.frontier.graph_hash == authored_state_hash(runtime.live.state)
        other_domain = composition_content_hash(runtime.live.state)
        assert other_domain != actual.frontier.graph_hash
        changed = _with_frontier(runtime, replace(actual.frontier, graph_hash=other_domain))
        coherent_other = capture_semantic_advisor_source(changed)
        assert coherent_other.proposed_frontier is None
        with pytest.raises(AuditIntegrityError, match="selected current graph binding"):
            make_semantic_advisor_invocation(coherent_other)
        coordinator.assert_completed()
