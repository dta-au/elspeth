"""Private semantic invocation DATA and the existing normalized action key.

Authenticated request identity is stable across receipts and provider history.
Exact evolving source identity has its own coverage binding. Neither result
grants current SQL authority, selects a graph proposal, invokes a provider or
qualifies a retained physical result; those live joins remain separate.
"""

from __future__ import annotations

from dataclasses import dataclass

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.advisor_output import ContinuationAdvisorDecision
from elspeth.web.composer.continuation_advisor import ContinuationAdvisorInvocation, ContinuationAdvisorPremise, nearest_compose_frame
from elspeth.web.composer.continuation_bootstrap import bootstrap_values, decode_continuation_bootstrap
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_snapshot,
    decode_frontier_chunk,
    encode_continuation_snapshot,
    encode_frontier_chunk,
    verify_private_chunk,
)
from elspeth.web.composer.continuation_evidence import TurnEvidenceSnapshot, state_from_evidence_json
from elspeth.web.composer.continuation_iteration import decode_continuation_iteration, encode_continuation_iteration
from elspeth.web.composer.continuation_message_shelf import encode_message_shelf
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_progress import ContinuationFrontier, continuation_action_key, require_bound_continuation_decision
from elspeth.web.composer.continuation_semantic_source import RawSemanticAdvisorSource
from elspeth.web.composer.turn_effects import authored_state_hash


def _derive(
    source: RawSemanticAdvisorSource,
) -> tuple[ContinuationAdvisorInvocation, ContinuationFrontier, str, str]:
    if type(source) is not RawSemanticAdvisorSource:
        raise TypeError("semantic invocation requires its nominal original raw capture")
    if source.proposed_frontier is not None:
        raise AuditIntegrityError("semantic invocation requires a selected frontier, never an unselected graph proposal")
    root = decode_continuation_snapshot(source.selected_snapshot_json)
    frame = nearest_compose_frame(root)
    if (
        type(source.checkpoint_id) is not str
        or not source.checkpoint_id
        or type(source.cursor) is not int
        or source.cursor != root.cursor
        or encode_continuation_snapshot(root) != source.selected_snapshot_json
        or source.logical_json != canonical_json(root.logical.model_dump(mode="json"))
        or source.admission_policy_json != root.admission_policy_json
        or source.admission_policy_hash != root.policy_hash
        or type(source.env_ref_names) is not frozenset
        or any(type(name) is not str for name in source.env_ref_names)
    ):
        raise AuditIntegrityError("semantic invocation changed its original selected root or policy")
    for chunk, reference in (
        (source.bootstrap, frame.bootstrap),
        (source.state, frame.state),
        (source.evidence, frame.evidence),
        (source.transcript, frame.transcript),
        (source.iteration, frame.iteration),
        (source.tools, frame.tools),
        (source.predecessor_frontier, root.semantic_frontier),
    ):
        if type(chunk) is not PrivateChunk or chunk.reference != reference:
            raise AuditIntegrityError("semantic invocation substituted an original complete source reference")
        verify_private_chunk(chunk.reference, chunk.canonical)
    bootstrap = decode_continuation_bootstrap(source.bootstrap)
    original = bootstrap_values(bootstrap)
    if (
        bootstrap.logical != root.logical
        or source.actor_user_id != bootstrap.actor_user_id
        or source.original_request_json != bootstrap.request_json
        or source.original_user_message != original.message
        or source.candidate != source.state.canonical
        or source.tool_manifest_json != source.tools.canonical
    ):
        raise AuditIntegrityError("semantic invocation changed its original authenticated request or candidate")
    decode_continuation_policy(source.admission_policy_json, source.admission_policy_hash)
    evidence = TurnEvidenceSnapshot.model_validate_json(source.evidence.canonical)
    if (
        canonical_json(evidence.model_dump(mode="json")) != source.evidence.canonical
        or evidence.logical != root.logical
        or (evidence.actor_user_id, evidence.session_id, evidence.request_id, evidence.request_content_hash)
        != (source.actor_user_id, root.logical.session_id, root.logical.original_user_message_id, stable_hash(original.message))
        or encode_message_shelf(evidence.transcript, env_ref_names=source.env_ref_names) != source.transcript
    ):
        raise AuditIntegrityError("semantic invocation changed its original complete evidence/transcript")
    iteration = decode_continuation_iteration(source.iteration, env_ref_names=source.env_ref_names)
    if encode_continuation_iteration(iteration, env_ref_names=source.env_ref_names) != source.iteration:
        raise AuditIntegrityError("semantic invocation changed its complete original iteration")
    state = state_from_evidence_json(source.state.canonical)
    frontier = decode_frontier_chunk(source.predecessor_frontier)
    # The actual first-Compose producer binds semantic frontier.graph_hash
    # with authored_state_hash. Physical content-scope/ordered-authority
    # hashes use a separate projection and cannot substitute for that owner.
    if encode_frontier_chunk(frontier) != source.predecessor_frontier or frontier.graph_hash != authored_state_hash(state):
        raise AuditIntegrityError("semantic invocation requires its already selected current graph binding")
    plugin_snapshot, plugin_error = parse_json_strict(source.plugin_snapshot_json)
    tools, tools_error = parse_json_strict(source.tool_manifest_json)
    body, body_error = parse_json_strict(source.source_body_json)
    if (
        plugin_error is not None
        or type(plugin_snapshot) is not dict
        or canonical_json(plugin_snapshot) != source.plugin_snapshot_json
        or plugin_snapshot.get("policy_hash") != source.plugin_policy_hash
        or plugin_snapshot.get("snapshot_hash") != source.catalog_snapshot_hash
        or tools_error is not None
        or type(tools) is not list
        or canonical_json(tools) != source.tool_manifest_json
        or body_error is not None
        or type(body) is not dict
    ):
        raise AuditIntegrityError("semantic invocation changed its complete raw policy/tools/source bodies")
    expected_body = canonical_json(
        {
            "checkpoint_id": source.checkpoint_id,
            "cursor": source.cursor,
            "selected_snapshot_json": source.selected_snapshot_json,
            "logical_json": source.logical_json,
            "bootstrap_json": source.bootstrap.canonical,
            "actor_user_id": source.actor_user_id,
            "original_request_json": source.original_request_json,
            "original_user_message": source.original_user_message,
            "candidate": source.candidate,
            "admission_policy_json": source.admission_policy_json,
            "admission_policy_hash": source.admission_policy_hash,
            "plugin_policy_hash": source.plugin_policy_hash,
            "catalog_snapshot_hash": source.catalog_snapshot_hash,
            "plugin_snapshot_json": source.plugin_snapshot_json,
            "tool_manifest_json": source.tool_manifest_json,
            "env_ref_names": sorted(source.env_ref_names),
            "source_chunks": [
                {"reference": chunk.reference.model_dump(mode="json"), "canonical": chunk.canonical}
                for chunk in (source.state, source.evidence, source.transcript, source.iteration, source.tools, source.predecessor_frontier)
            ],
            "unselected_graph_proposal": None,
        }
    )
    if source.source_body_json != expected_body:
        raise AuditIntegrityError("semantic invocation changed its original complete raw capture body")
    request_input = {
        "domain": "composer.semantic-request-input.v1",
        "logical": root.logical.model_dump(mode="json"),
        "actor_user_id": source.actor_user_id,
        "original_request_json": source.original_request_json,
        "original_user_message": original.message,
    }
    policy = {
        "domain": "composer.semantic-policy.v1",
        "actor_user_id": source.actor_user_id,
        "catalog_snapshot_hash": source.catalog_snapshot_hash,
        "plugin_policy_hash": source.plugin_policy_hash,
        "continuation_policy_hash": source.admission_policy_hash,
    }
    invocation = ContinuationAdvisorInvocation(
        kind="semantic",
        state=source.state.reference,
        evidence=source.evidence.reference,
        transcript=source.transcript.reference,
        iteration=source.iteration.reference,
        coverage_binding=stable_hash({"domain": "composer.semantic-complete-source.v1", "capture": body}),
        manual_arguments_json=None,
        manual_tool_call_id=None,
        candidate=source.candidate,
        premise=ContinuationAdvisorPremise(
            input_hash=stable_hash(request_input),
            policy_hash=stable_hash(policy),
            tool_manifest_hash=stable_hash(tools),
            frontier=source.predecessor_frontier.reference,
        ),
    )
    return invocation, frontier, canonical_json(request_input), canonical_json(policy)


@dataclass(frozen=True, slots=True)
class SemanticAdvisorInvocationData:
    """One exact private source, pending fresh live SQL/CAS proof."""

    source: RawSemanticAdvisorSource
    invocation: ContinuationAdvisorInvocation
    frontier: ContinuationFrontier
    request_input_json: str
    policy_preimage_json: str

    def verify(self, original_source: RawSemanticAdvisorSource | None = None) -> None:
        if original_source is not None and self.source is not original_source:
            raise AuditIntegrityError("semantic invocation changed its one original raw capture")
        expected = _derive(self.source)
        if (
            type(self.invocation) is not ContinuationAdvisorInvocation
            or (self.invocation, self.frontier, self.request_input_json, self.policy_preimage_json) != expected
        ):
            raise AuditIntegrityError("semantic invocation changed its original source or derived identities")


def make_semantic_advisor_invocation(source: RawSemanticAdvisorSource) -> SemanticAdvisorInvocationData:
    """Derive historical invocation DATA; no provider, SQL or graph selection."""
    invocation, frontier, request_input, policy = _derive(source)
    return SemanticAdvisorInvocationData(source, invocation, frontier, request_input, policy)


def semantic_advisor_action_key(
    data: SemanticAdvisorInvocationData,
    decision: ContinuationAdvisorDecision,
    *,
    actual_advisor_call_id: str,
    permitted_read_tools: frozenset[str],
    complete_required_coverage: bool,
) -> str | None:
    """Apply the existing complete decision/recurrence contract to owned DATA.

    Current SQL must still prove real physical result, evidence completeness,
    actual permissions, coverage and unresolved owner results before use.
    This function never substitutes complete source history for the existing
    action-specific normalized non-provider evidence or records execution.
    """
    if type(data) is not SemanticAdvisorInvocationData or type(decision) is not ContinuationAdvisorDecision:
        raise TypeError("semantic action identity requires its nominal original DATA")
    data.verify()
    premise = data.invocation.premise
    if premise is None:
        raise AuditIntegrityError("semantic action identity lost its original premise")
    retained = {item.receipt_id: item for item in data.frontier.evidence}
    if len(retained) != len(data.frontier.evidence):
        raise AuditIntegrityError("semantic action identity has conflicting retained evidence receipts")
    require_bound_continuation_decision(
        decision,
        actual_advisor_call_id=actual_advisor_call_id,
        frontier=data.frontier,
        retained_evidence=retained,
        input_hash=premise.input_hash,
        policy_hash=premise.policy_hash,
        tool_manifest_hash=premise.tool_manifest_hash,
        permitted_read_tools=permitted_read_tools,
        complete_required_coverage=complete_required_coverage,
    )
    action = decision.prescribed_action
    return None if action is None else continuation_action_key(action, retained_evidence=retained)
