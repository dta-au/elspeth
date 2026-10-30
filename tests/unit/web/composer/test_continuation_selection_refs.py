"""Closed frame reference positions, independently of payload-domain decoders."""

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.advisor_output import ReadContinuationAction
from elspeth.web.composer.continuation import (
    AdvisorFrame,
    ComposeFrame,
    ContinuationActionSource,
    EffectFrontierEntry,
    EndFrame,
    InitializeFrame,
    PrivateChunkRef,
    RetainedOwnerResultRef,
    ToolBatchFrame,
)
from elspeth.web.sessions.continuation_selection import required_private_chunks
from tests.unit.web.composer.test_continuation_frames import initial_stack


def reference(codec, number):
    return PrivateChunkRef(codec=codec, digest=f"{number:064x}", byte_length=number)


def test_each_actual_frame_reference_is_retained_in_first_occurrence_order():
    bootstrap = reference("composer.bootstrap.v1", 1)
    response = reference("composer.response.v1", 2)
    iteration = reference("composer.iteration.v1", 3)
    messages = reference("composer.messages.v1", 4)
    initial = reference("composer.state.v1", 5)
    state = reference("composer.state.v1", 6)
    evidence = reference("composer.evidence.v2", 7)
    audit = reference("composer.audit-buffer.v1", 8)
    transcript = reference("composer.transcript.v1", 9)
    caches = reference("composer.caches.v1", 10)
    attachments = reference("composer.attachment-admissions.v1", 11)
    tools = reference("composer.tools.v1", 12)
    accumulator = reference("composer.tool-batch.v1", 13)
    advisor = reference("composer.advisor.v1", 14)
    candidate = reference("composer.end.v1", 15)
    owner = reference("composer.owner-result.v1", 16)
    frontier = reference("composer.semantic-frontier.v1", 17)
    compose = ComposeFrame(
        phase="response_retained",
        bootstrap=bootstrap,
        iteration=iteration,
        messages=messages,
        tools=tools,
        initial_state=initial,
        state=state,
        evidence=evidence,
        audit_buffer=audit,
        transcript=transcript,
        caches=caches,
        attachment_admissions=attachments,
        initial_version=1,
        current_state_id=None,
        slice_sequence=1,
        composition_slice=0,
        discovery_slice=0,
        composition_total=0,
        discovery_total=0,
        advisor_calls=0,
        repair_turns=0,
        advisor_checkpoint_passes=0,
        mutation_success_seen=False,
        persisted_assistant_message_id=None,
        persisted_assistant_content=None,
        persisted_tool_call_turn=False,
        advisor_repair_context_introduced=False,
        pending_advisor_elision_indices=(),
        pending_response=response,
    )
    root = initial_stack().model_copy(
        update={
            "semantic_frontier": frontier,
            "pending_owner_results": (RetainedOwnerResultRef(effect_key="session:read", kind="read", result=owner),),
            "effect_frontier": (EffectFrontierEntry(effect_id="session:read", disposition="committed", receipt_id="receipt"),),
            "frames": (
                InitializeFrame(
                    request_json='{"content":"café"}',
                    ingress_phase="ready",
                    title_attempt_id=None,
                    bootstrap=bootstrap,
                    title_response=response,
                ),
                compose,
                ToolBatchFrame(phase="tool_ready", response=response, accumulator=accumulator, next_tool_index=0, settled_effect_ids=()),
                AdvisorFrame(phase="input_ready", review=advisor, response=response, coverage_binding="coverage"),
                EndFrame(phase="review_needed", state=state, candidate=candidate, publication_binding=None),
            ),
        }
    )
    assert required_private_chunks(root) == (
        owner,
        frontier,
        bootstrap,
        response,
        iteration,
        messages,
        initial,
        state,
        evidence,
        audit,
        transcript,
        caches,
        attachments,
        tools,
        accumulator,
        advisor,
        candidate,
    )


def test_same_identity_conflicting_bounds_refuses_after_positive():
    root = initial_stack()
    original = root.frames[0]
    assert type(original) is InitializeFrame and original.bootstrap is not None
    assert required_private_chunks(root) == (original.bootstrap,)
    changed = original.bootstrap.model_copy(update={"byte_length": original.bootstrap.byte_length + 1})
    root = root.model_copy(update={"frames": (original.model_copy(update={"title_response": changed}),)})
    with pytest.raises(AuditIntegrityError, match="conflicting byte bounds"):
        required_private_chunks(root)


def test_actual_prescribed_action_source_is_retained_even_without_frame_response():
    root = initial_stack()
    response = reference("composer.response.v1", 99)
    action = ReadContinuationAction(
        kind="read_tool",
        obligation_ids=("request",),
        evidence_ids=(),
        input_hash="input",
        graph_hash="graph",
        policy_hash="policy",
        tool_manifest_hash="tools",
        tool_name="get_plugin_schema",
        arguments_json='{"name":"csv","type":"source"}',
    )
    root = root.model_copy(
        update={
            "active_action_json": action.model_dump_json(),
            "active_action_source": ContinuationActionSource(effect_key="provider:1:0", input_sha256="a" * 64, response=response),
        }
    )
    assert required_private_chunks(root) == (response, root.frames[0].bootstrap)
