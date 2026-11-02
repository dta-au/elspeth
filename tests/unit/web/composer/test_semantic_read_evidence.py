"""Pure DATA controls for semantic Read novelty, separate from SQL admission."""

from uuid import UUID

from elspeth.web.catalog.schemas import PluginSummary
from elspeth.web.composer.advisor_output import ContinuationEvidenceRef, ReadContinuationAction, ReviewContinuationAction
from elspeth.web.composer.continuation import LogicalRequestBinding
from elspeth.web.composer.continuation_progress import continuation_action_key
from elspeth.web.composer.continuation_semantic_advisor_evidence import semantic_read_scope_binding, semantic_read_value_data
from elspeth.web.composer.state import CompositionState, PipelineMetadata, ValidationSummary
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.sessions.continuation_owner_results import (
    ContinuationReadObservations,
    ContinuationReadOwnerResult,
    build_read_owner_result,
)


def _read_with_prescriber(call_id: str, *, success: bool = True) -> ContinuationReadOwnerResult:
    state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(name="Read evidence"), version=3)
    result = ToolResult(
        success=success,
        updated_state=state,
        validation=ValidationSummary(True, ()),
        affected_nodes=(),
        data={"available": [PluginSummary(name="csv", description="CSV source", plugin_type="source", config_fields=[])], "prohibited": []}
        if success
        else None,
        post_call_hints=("original read hint",),
        _validation_snapshot_hash="a" * 64,
        _state_validation_withheld=True,
    )
    retained = build_read_owner_result(
        tool_name="list_sources",
        arguments={},
        result=result,
        pre_state=state,
        observations=ContinuationReadObservations(),
    )
    # The owned result captures a genuine ToolResult through its normal codec.
    # This copy varies only the paid prescriber for the DATA projection control;
    # it does not claim a SQL-paid decision, receipt, or live execution grant.
    return retained.model_copy(update={"prescribing_provider_call_id": call_id})


def _scope() -> tuple[LogicalRequestBinding, ReadContinuationAction]:
    logical = LogicalRequestBinding(
        session_id=str(UUID(int=1)),
        operation_id=str(UUID(int=2)),
        original_user_message_id=str(UUID(int=3)),
        request_hash="a" * 64,
        kind="compose_message",
        base_state_id=None,
    )
    action = ReadContinuationAction(
        kind="read_tool",
        obligation_ids=("request",),
        evidence_ids=(),
        input_hash="b" * 64,
        graph_hash="c" * 64,
        policy_hash="d" * 64,
        tool_manifest_hash="e" * 64,
        tool_name="list_sources",
        arguments_json="{}",
    )
    return logical, action


def test_identical_owned_read_values_ignore_a_new_paid_prescriber_but_keep_custody_distinct():
    first = semantic_read_value_data(_read_with_prescriber("paid-call-one"))
    second = semantic_read_value_data(_read_with_prescriber("paid-call-two"))
    assert first.semantic_result_json == second.semantic_result_json
    assert first.content_hash == second.content_hash
    assert first.original_result_sha256 != second.original_result_sha256

    changed = semantic_read_value_data(_read_with_prescriber("paid-call-two", success=False))
    assert changed.semantic_result_json != first.semantic_result_json
    assert changed.content_hash != first.content_hash
    assert changed.original_result_sha256 != second.original_result_sha256


def test_review_action_key_uses_stable_read_values_and_scope_not_receipt_or_paid_call():
    logical, read_action = _scope()
    scope = semantic_read_scope_binding(logical, "original-actor", read_action)
    assert scope == semantic_read_scope_binding(logical, "original-actor", read_action)
    assert scope != semantic_read_scope_binding(logical, "another-actor", read_action)
    assert scope != semantic_read_scope_binding(logical, "original-actor", read_action.model_copy(update={"graph_hash": "f" * 64}))

    first = semantic_read_value_data(_read_with_prescriber("paid-call-one"))
    repeated = semantic_read_value_data(_read_with_prescriber("paid-call-two"))
    changed = semantic_read_value_data(_read_with_prescriber("paid-call-two", success=False))
    reference_one = ContinuationEvidenceRef(
        receipt_id="read:physical-one",
        source_kind="read",
        content_hash=first.content_hash,
        original_attempt_id=None,
        coverage_binding=scope,
    )
    reference_two = reference_one.model_copy(update={"receipt_id": "read:physical-two", "content_hash": repeated.content_hash})
    reference_changed = reference_one.model_copy(update={"receipt_id": "read:physical-three", "content_hash": changed.content_hash})
    review = ReviewContinuationAction(
        kind="review",
        obligation_ids=("request",),
        evidence_ids=(reference_one.receipt_id,),
        input_hash=read_action.input_hash,
        graph_hash=read_action.graph_hash,
        policy_hash=read_action.policy_hash,
        tool_manifest_hash=read_action.tool_manifest_hash,
        candidate_ref="candidate:" + "1" * 64,
        evidence_binding="paid-source-one",
    )
    first_key = continuation_action_key(review, retained_evidence={reference_one.receipt_id: reference_one})
    repeated_review = review.model_copy(update={"evidence_ids": (reference_two.receipt_id,), "evidence_binding": "paid-source-two"})
    assert continuation_action_key(repeated_review, retained_evidence={reference_two.receipt_id: reference_two}) == first_key
    changed_review = review.model_copy(update={"evidence_ids": (reference_changed.receipt_id,)})
    assert continuation_action_key(changed_review, retained_evidence={reference_changed.receipt_id: reference_changed}) != first_key
