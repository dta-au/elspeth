"""Raw semantic source DATA; no invocation, novelty or current SQL grant."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, fields, replace

import litellm
import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer import provider_gateway
from elspeth.web.composer.advisor_output import ContinuationEvidenceRef
from elspeth.web.composer.continuation_advisor import nearest_compose_frame
from elspeth.web.composer.continuation_codec import (
    decode_frontier_chunk,
    encode_continuation_snapshot,
    encode_frontier_chunk,
)
from elspeth.web.composer.continuation_progress import ActionExecutionReceipt, ContinuationObligation
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.turn_effects import authored_state_hash
from elspeth.web.sessions.continuation_runtime import runtime_chunk
from tests.unit.web.composer.test_continuation_semantic_advisor import _case
from tests.unit.web.composer.test_continuation_semantic_advisor import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows

operation_store = _operation_store


def _with_frontier(runtime, frontier):
    selected = runtime.source.selected
    old = selected.stack.semantic_frontier
    assert old is not None
    chunk = encode_frontier_chunk(frontier)
    changed = SelectedContinuationState(
        selected.checkpoint_id,
        selected.stack.model_copy(update={"semantic_frontier": chunk.reference}),
        tuple(chunk if item.reference == old else item for item in selected.chunks),
    )
    return replace(runtime, source=replace(runtime.source, selected=changed))


@pytest.mark.asyncio
async def test_raw_capture_has_exact_preimages_without_provider_or_sql_writes(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_semantic_source import RawSemanticAdvisorSource, capture_semantic_advisor_source

    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, provider, _context, coordinator, _baseline, custodies):
        runtime = inputs["runtime"]
        selected = runtime.source.selected
        frame = nearest_compose_frame(selected.stack)
        before = stored_rows(operation_store[0])
        original_budget = provider.cost_budget.snapshot()

        async def forbidden_sdk(**kwargs):
            raise AssertionError("raw capture cannot call a provider")

        def forbidden_mint(*args, **kwargs):
            raise AssertionError("raw capture cannot mint provider custody")

        monkeypatch.setattr(litellm, "acompletion", forbidden_sdk)
        monkeypatch.setattr(provider, "mint", forbidden_mint)
        capture = capture_semantic_advisor_source(runtime)
        assert type(capture) is RawSemanticAdvisorSource
        assert capture.checkpoint_id == selected.checkpoint_id and capture.cursor == selected.stack.cursor
        assert capture.selected_snapshot_json == encode_continuation_snapshot(selected.stack)
        assert capture.logical_json == canonical_json(selected.stack.logical.model_dump(mode="json"))
        assert capture.bootstrap == runtime_chunk(selected, frame.bootstrap)
        assert capture.original_request_json == runtime.source.bootstrap.request_json
        assert capture.original_user_message == runtime.source.original.message == runtime.bindings.message
        assert capture.actor_user_id == runtime.source.bootstrap.actor_user_id == runtime.bindings.user_id
        for actual, reference in (
            (capture.state, frame.state),
            (capture.evidence, frame.evidence),
            (capture.transcript, frame.transcript),
            (capture.iteration, frame.iteration),
            (capture.tools, frame.tools),
        ):
            assert actual == runtime_chunk(selected, reference)
        assert capture.candidate == capture.state.canonical == canonical_json(runtime.live.state.to_dict())
        assert capture.tool_manifest_json == capture.tools.canonical == canonical_json(runtime.live.tools)
        assert capture.admission_policy_json == selected.stack.admission_policy_json
        assert capture.admission_policy_hash == selected.stack.policy_hash
        # The real admission producer retains Pydantic's original JSON text,
        # not RFC8785 key order/number spelling. Preserve either exact body
        # while comparing the complete owned policy values and original hash.
        canonical_policy = canonical_json(runtime.source.policy.model_dump(mode="json"))
        assert selected.stack.admission_policy_json == runtime.source.policy.model_dump_json()
        assert selected.stack.admission_policy_json != canonical_policy
        canonical_selected = replace(selected, stack=selected.stack.model_copy(update={"admission_policy_json": canonical_policy}))
        canonical_runtime = replace(runtime, source=replace(runtime.source, selected=canonical_selected))
        canonical_capture = capture_semantic_advisor_source(canonical_runtime)
        assert canonical_capture.admission_policy_json == canonical_policy
        assert canonical_capture.admission_policy_hash == capture.admission_policy_hash
        assert json.loads(canonical_capture.admission_policy_json) == json.loads(capture.admission_policy_json)
        assert capture.plugin_policy_hash == runtime.bindings.plugin_snapshot.policy_hash
        assert capture.catalog_snapshot_hash == runtime.bindings.plugin_snapshot.snapshot_hash
        assert capture.plugin_policy_hash != capture.admission_policy_hash
        snapshot = json.loads(capture.plugin_snapshot_json)
        assert snapshot["principal_scope"] == runtime.bindings.plugin_snapshot.principal_scope
        assert snapshot["available"] == sorted(map(str, runtime.bindings.plugin_snapshot.available))
        assert snapshot["policy_hash"] == capture.plugin_policy_hash
        assert snapshot["snapshot_hash"] == capture.catalog_snapshot_hash
        assert capture.predecessor_frontier == runtime_chunk(selected, selected.stack.semantic_frontier)
        assert capture.proposed_frontier is None
        # Match the real first-Compose semantic producer, rather than the
        # separate physical content-scope hash projection.
        assert decode_frontier_chunk(capture.predecessor_frontier).graph_hash == authored_state_hash(runtime.live.state)
        assert authored_state_hash(runtime.live.state) != composition_content_hash(runtime.live.state)
        unchanged = capture_semantic_advisor_source(runtime, propose_current_graph=True)
        assert unchanged.predecessor_frontier == capture.predecessor_frontier
        assert unchanged.proposed_frontier is None
        body = json.loads(capture.source_body_json)
        assert body["original_request_json"] == runtime.source.bootstrap.request_json
        assert body["original_user_message"] == runtime.bindings.message
        assert body["bootstrap_json"] == capture.bootstrap.canonical
        assert body["selected_snapshot_json"] == capture.selected_snapshot_json
        assert body["plugin_snapshot_json"] == capture.plugin_snapshot_json
        assert body["admission_policy_json"] == capture.admission_policy_json
        assert body["candidate"] == capture.candidate
        assert body["source_chunks"] == [
            {"reference": item.reference.model_dump(mode="json"), "canonical": item.canonical}
            for item in (
                capture.state,
                capture.evidence,
                capture.transcript,
                capture.iteration,
                capture.tools,
                capture.predecessor_frontier,
            )
        ]
        names = {item.name for item in fields(capture)}
        assert names.isdisjoint({"input_hash", "premise", "invocation", "decision", "authority", "custody"})
        with pytest.raises(FrozenInstanceError):
            capture.actor_user_id = "changed-after-capture"
        assert stored_rows(operation_store[0]) == before
        assert provider.cost_budget.snapshot() == original_budget and custodies == []
        coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "request",
        "request_body",
        "original_values",
        "actor",
        "session",
        "user_message",
        "state",
        "transcript",
        "evidence",
        "iteration",
        "policy",
        "env_inventory",
    ],
)
async def test_raw_capture_refuses_drift_after_actual_restoration(operation_store, tmp_path, monkeypatch, change):
    from elspeth.web.composer.continuation_semantic_source import capture_semantic_advisor_source

    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, _context, coordinator, _baseline, _custodies):
        runtime = inputs["runtime"]
        positive = capture_semantic_advisor_source(runtime)
        assert positive.candidate == canonical_json(runtime.live.state.to_dict())
        changed = runtime
        restore = None
        if change in ("request", "actor", "session", "user_message"):
            field = {"request": "message", "actor": "user_id", "session": "session_id", "user_message": "user_message_id"}[change]
            changed = replace(runtime, bindings=replace(runtime.bindings, **{field: "different original binding"}))
        elif change == "request_body":
            original = runtime.source.bootstrap
            request = json.loads(original.request_json)
            request["content"] = "Different complete originating request"
            changed = replace(
                runtime, source=replace(runtime.source, bootstrap=original.model_copy(update={"request_json": canonical_json(request)}))
            )
        elif change == "original_values":
            changed = replace(
                runtime, source=replace(runtime.source, original=replace(runtime.source.original, message="Different decoded request"))
            )
        elif change == "state":
            changed = replace(
                runtime, live=replace(runtime.live, state=replace(runtime.live.state, version=runtime.live.state.version + 1))
            )
        elif change == "policy":
            current = runtime.source.policy
            altered = current.model_copy(update={"segment": current.segment.model_copy(update={"reserve": current.segment.reserve + 0.25})})
            changed = replace(runtime, source=replace(runtime.source, policy=altered))
        elif change == "env_inventory":
            changed = replace(runtime, env_ref_names=runtime.env_ref_names | {"UNSELECTED_ENVIRONMENT_REF"})
        else:
            if change == "transcript":
                owner, field = runtime.source.evidence.transcript, "messages_json"
                old = owner.messages_json
                value = (*old, canonical_json({"role": "user", "content": "unselected transcript"}))
            elif change == "evidence":
                owner, field = runtime.source.evidence, "request_content_hash"
                old, value = owner.request_content_hash, stable_hash("unselected evidence")
            else:
                owner, field = runtime.source.iteration, "advisor_review_json"
                old = owner.advisor_review_json
                payload = json.loads(old)
                payload["completed_passes"] += 1
                value = canonical_json(payload)
            # Deliberately corrupt an existing owned value after restoration;
            # its original retained SQL chunk remains unchanged.
            object.__setattr__(owner, field, value)
            restore = (owner, field, old)
        try:
            with pytest.raises(AuditIntegrityError):
                capture_semantic_advisor_source(changed)
        finally:
            if restore is not None:
                object.__setattr__(*restore)
        coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["plugin_policy", "snapshot_body", "catalog_snapshot", "ordered_tools", "catalog_authored_state"])
async def test_raw_capture_reacquires_real_policy_catalog_and_tool_owners(operation_store, tmp_path, monkeypatch, change):
    from elspeth.web.composer.continuation_semantic_source import capture_semantic_advisor_source

    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, _context, coordinator, _baseline, _custodies):
        runtime = inputs["runtime"]
        assert capture_semantic_advisor_source(runtime).plugin_policy_hash == runtime.bindings.plugin_snapshot.policy_hash
        factory = runtime.materialization.service._policy_context
        if change in ("plugin_policy", "snapshot_body", "catalog_snapshot"):
            actual_factory = factory._snapshot_factory

            def changed_snapshot(actor):
                original = actual_factory(actor)
                if change == "plugin_policy":
                    return replace(original, policy_hash=stable_hash("changed current plugin policy"))
                if change == "snapshot_body":
                    # Keeping the opaque hashes is an explicit opponent: a
                    # whole owned body comparison must still notice drift.
                    return replace(original, principal_scope="changed-current-principal")
                return replace(original, snapshot_hash=stable_hash("changed current catalog snapshot"))

            monkeypatch.setattr(factory, "_snapshot_factory", changed_snapshot)
        elif change == "ordered_tools":
            actual_tools = provider_gateway.composer_loop_tool_definitions

            def changed_tools(dialect):
                values = actual_tools(dialect)
                assert len(values) > 1
                return list(reversed(values))

            monkeypatch.setattr(provider_gateway, "composer_loop_tool_definitions", changed_tools)
        else:
            actual_validate = PolicyCatalogView.validate_composition_state

            def changed_validation(owner, state):
                value = actual_validate(owner, state)
                return replace(value, authored_state=replace(value.authored_state, version=value.authored_state.version + 1))

            monkeypatch.setattr(PolicyCatalogView, "validate_composition_state", changed_validation)
        with pytest.raises(AuditIntegrityError):
            capture_semantic_advisor_source(runtime)
        coordinator.assert_completed()


@pytest.mark.asyncio
async def test_raw_graph_proposal_preserves_exact_unselected_predecessor_and_all_obligations(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_semantic_source import UnselectedSemanticFrontierProposal, capture_semantic_advisor_source

    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, _context, coordinator, _baseline, _custodies):
        runtime = inputs["runtime"]
        selected = runtime.source.selected
        retained = decode_frontier_chunk(runtime_chunk(selected, selected.stack.semantic_frontier))
        evidence = ContinuationEvidenceRef(
            receipt_id="retained-read:1",
            content_hash=stable_hash("complete retained evidence"),
            original_attempt_id=None,
            coverage_binding="retained-input",
        )
        predecessor = replace(
            retained,
            obligations=(
                *retained.obligations,
                ContinuationObligation("request/blocked", "request", "Blocked subset", runtime.bindings.user_message_id, 0, 1, "blocked"),
            ),
            evidence=(*retained.evidence, evidence),
            pending_review_bindings=(*retained.pending_review_bindings, "unresolved-review:1"),
            action_receipts=(
                ActionExecutionReceipt("unresolved-action", "retained-owner:1", "unknown"),
                ActionExecutionReceipt("closed-action", "retained-owner:2", "refused"),
            ),
            graph_hash=stable_hash("previous selected graph"),
        )
        runtime = _with_frontier(runtime, predecessor)
        original_json = encode_continuation_snapshot(runtime.source.selected.stack)
        assert capture_semantic_advisor_source(runtime).proposed_frontier is None
        capture = capture_semantic_advisor_source(runtime, propose_current_graph=True)
        proposal = capture.proposed_frontier
        assert type(proposal) is UnselectedSemanticFrontierProposal
        assert proposal.predecessor == capture.predecessor_frontier == encode_frontier_chunk(predecessor)
        projected = decode_frontier_chunk(proposal.proposed)
        assert projected == replace(predecessor, graph_hash=authored_state_hash(runtime.live.state))
        assert projected.obligations == predecessor.obligations and projected.evidence == predecessor.evidence
        assert (
            projected.pending_review_bindings == predecessor.pending_review_bindings
            and projected.action_receipts == predecessor.action_receipts
        )
        assert proposal.proposed.reference != proposal.predecessor.reference
        assert runtime.source.selected.stack.semantic_frontier == proposal.predecessor.reference
        assert encode_continuation_snapshot(runtime.source.selected.stack) == original_json
        coordinator.assert_completed()
