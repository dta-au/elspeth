"""Exact raw semantic source DATA, without invocation or action novelty.

These immutable values describe one already selected runtime cohort. They
grant no current SQL custody, projection selection, provider dispatch, review
acceptance or retry. Complete source history is deliberately not assigned to
ContinuationAdvisorInvocation.premise.input_hash or any action novelty key.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer import provider_gateway
from elspeth.web.composer._compose_loop_carriers import _ComposeIterationBindings, _ComposeIterationState
from elspeth.web.composer.application_policy import PluginPolicyContextFactory
from elspeth.web.composer.continuation_advisor import nearest_compose_frame
from elspeth.web.composer.continuation_bootstrap import bootstrap_values
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_frontier_chunk,
    encode_continuation_snapshot,
    encode_frontier_chunk,
)
from elspeth.web.composer.continuation_evidence import encode_turn_evidence
from elspeth.web.composer.continuation_iteration import encode_continuation_iteration
from elspeth.web.composer.continuation_message_shelf import encode_message_shelf
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers
from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.turn_effects import authored_state_hash
from elspeth.web.credential_guard import require_no_credential_material_in_state
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.continuation_runtime import ContinuationRuntimeSource, runtime_chunk


@dataclass(frozen=True, slots=True)
class UnselectedSemanticFrontierProposal:
    """Only a prospective graph binding; the predecessor remains selected."""

    predecessor: PrivateChunk
    proposed: PrivateChunk


@dataclass(frozen=True, slots=True)
class RawSemanticAdvisorSource:
    checkpoint_id: str
    cursor: int
    selected_snapshot_json: str
    logical_json: str
    bootstrap: PrivateChunk
    actor_user_id: str
    original_request_json: str
    original_user_message: str
    state: PrivateChunk
    evidence: PrivateChunk
    transcript: PrivateChunk
    iteration: PrivateChunk
    tools: PrivateChunk
    predecessor_frontier: PrivateChunk
    proposed_frontier: UnselectedSemanticFrontierProposal | None
    candidate: str
    admission_policy_json: str
    admission_policy_hash: str
    plugin_policy_hash: str
    catalog_snapshot_hash: str
    plugin_snapshot_json: str
    tool_manifest_json: str
    env_ref_names: frozenset[str]
    # Exact evolving history, not a semantic recurrence/novelty preimage.
    source_body_json: str


def _plugin_snapshot_json(snapshot: PluginAvailabilitySnapshot) -> str:
    """Retain the complete effective policy snapshot exposed by its owner."""
    return canonical_json(
        {
            "policy_hash": snapshot.policy_hash,
            "principal_scope": snapshot.principal_scope,
            "available": sorted(map(str, snapshot.available)),
            "unavailable": [(str(item.plugin_id), item.reason.value) for item in snapshot.unavailable],
            "selected": [(capability.value, None if plugin is None else str(plugin)) for capability, plugin in snapshot.selected],
            "usable_profile_aliases": [(str(plugin), aliases) for plugin, aliases in snapshot.usable_profile_aliases],
            "selected_profile_aliases": [(str(plugin), alias) for plugin, alias in snapshot.selected_profile_aliases],
            "control_modes": [(capability.value, mode.value) for capability, mode in snapshot.control_modes],
            "power_automate_allowed_origins": snapshot.power_automate_allowed_origins,
            "binding_generation_fingerprint": snapshot.binding_generation_fingerprint,
            "authority": snapshot.authority.value,
            "snapshot_hash": snapshot.snapshot_hash,
        }
    )


def capture_semantic_advisor_source(
    runtime: RestoredContinuationRuntime, *, propose_current_graph: bool = False
) -> RawSemanticAdvisorSource:
    """Recheck restored DATA and current policy owners without selecting work.

    This does not re-enter a current SQL transaction. A future current owner
    must compare the exact cohort again before persisting or dispatching it.
    Optional graph projection preserves every other retained frontier field;
    it neither marks progress nor installs a frame or prescribed action.
    """
    if (
        type(runtime) is not RestoredContinuationRuntime
        or type(runtime.live) is not _ComposeIterationState
        or type(runtime.bindings) is not _ComposeIterationBindings
        or type(runtime.source) is not ContinuationRuntimeSource
        or type(runtime.source.selected) is not SelectedContinuationState
        or type(runtime.materialization) is not CurrentContinuationMessageProducers
    ):
        raise TypeError("raw semantic capture requires its actual restored runtime DATA")
    if type(propose_current_graph) is not bool:
        raise TypeError("raw graph proposal requires an explicit boolean")
    source, live, bindings = runtime.source, runtime.live, runtime.bindings
    selected = source.selected
    selected.__post_init__()
    root = selected.stack
    frame = nearest_compose_frame(root)
    original = bootstrap_values(source.bootstrap)
    bootstrap = runtime_chunk(selected, frame.bootstrap)
    if (
        source.bootstrap.logical != root.logical
        or bootstrap.canonical != canonical_json(source.bootstrap.model_dump(mode="json"))
        or source.original != original
        or source.evidence.logical != root.logical
        or (bindings.session_id, bindings.user_id, bindings.user_message_id, bindings.message)
        != (root.logical.session_id, source.bootstrap.actor_user_id, root.logical.original_user_message_id, original.message)
        or (source.evidence.session_id, source.evidence.actor_user_id, source.evidence.request_id)
        != (root.logical.session_id, source.bootstrap.actor_user_id, root.logical.original_user_message_id)
        or source.evidence.request_content_hash != stable_hash(original.message)
    ):
        raise AuditIntegrityError("raw semantic capture changed its complete original request/bootstrap/actor")
    names = runtime.env_ref_names
    if type(names) is not frozenset or names != source.evidence_env_ref_names:
        raise AuditIntegrityError("raw semantic capture changed its admitted credential environment")
    state = runtime_chunk(selected, frame.state)
    if type(live.state) is not CompositionState or canonical_json(live.state.to_dict()) != state.canonical:
        raise AuditIntegrityError("raw semantic candidate differs from its complete selected state")
    require_no_credential_material_in_state(live.state, surface="raw semantic source state", env_ref_names=names)
    evidence = runtime_chunk(selected, frame.evidence)
    transcript = runtime_chunk(selected, frame.transcript)
    iteration = runtime_chunk(selected, frame.iteration)
    if (
        source.admitted_evidence.snapshot is not source.evidence
        or canonical_json(source.evidence.model_dump(mode="json")) != evidence.canonical
        or encode_turn_evidence(source.admitted_evidence, env_ref_names=names) != evidence
        or encode_message_shelf(source.evidence.transcript, env_ref_names=names) != transcript
        or encode_continuation_iteration(source.iteration, env_ref_names=names) != iteration
    ):
        raise AuditIntegrityError("raw semantic capture changed its selected evidence/transcript/iteration")
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    retained_policy_body, policy_error = parse_json_strict(root.admission_policy_json)
    # Admission retains model_dump_json(), whose field order and numeric
    # spelling need not be RFC8785 canonical. Compare every body field in
    # canonical form while retaining the exact original text below. The
    # existing decoder still proves the closed version and original hash.
    if (
        policy_error is not None
        or source.policy != policy
        or canonical_json(source.policy.model_dump(mode="json")) != canonical_json(retained_policy_body)
    ):
        raise AuditIntegrityError("raw semantic capture changed its complete admitted policy body")
    service = runtime.materialization.service
    if type(service._policy_context) is not PluginPolicyContextFactory:
        raise AuditIntegrityError("raw semantic capture lost its actual plugin policy context owner")
    snapshot, catalog = service._policy_context.build(source.bootstrap.actor_user_id)
    if (
        type(snapshot) is not PluginAvailabilitySnapshot
        or type(bindings.plugin_snapshot) is not PluginAvailabilitySnapshot
        or type(catalog) is not PolicyCatalogView
        or type(bindings.policy_catalog) is not PolicyCatalogView
        or snapshot != bindings.plugin_snapshot
        or catalog.snapshot != snapshot
        or bindings.policy_catalog.snapshot != snapshot
    ):
        raise AuditIntegrityError("raw semantic capture requires current actor policy/catalog reacquisition")
    authored = catalog.validate_composition_state(live.state).authored_state
    if canonical_json(authored.to_dict()) != state.canonical:
        raise AuditIntegrityError("raw semantic capture requires current catalog-authored state reacquisition")
    if frame.tools is None:
        raise AuditIntegrityError("raw semantic capture lost its selected complete tool manifest")
    tools = runtime_chunk(selected, frame.tools)
    if tools.canonical != canonical_json(live.tools) or tools.canonical != canonical_json(
        provider_gateway.composer_loop_tool_definitions(service._planner_dialect)
    ):
        raise AuditIntegrityError("raw semantic capture changed its current ordered tool manifest")
    if root.semantic_frontier is None:
        raise AuditIntegrityError("raw semantic capture lost its retained frontier predecessor")
    predecessor = runtime_chunk(selected, root.semantic_frontier)
    retained = decode_frontier_chunk(predecessor)
    proposed: UnselectedSemanticFrontierProposal | None = None
    # Match the retained semantic frontier's real first-Compose producer.
    # Physical content-scope hashes use a different authority projection.
    graph_hash = authored_state_hash(live.state)
    if propose_current_graph and retained.graph_hash != graph_hash:
        proposed = UnselectedSemanticFrontierProposal(predecessor, encode_frontier_chunk(replace(retained, graph_hash=graph_hash)))
    selected_json = encode_continuation_snapshot(root)
    logical_json = canonical_json(root.logical.model_dump(mode="json"))
    snapshot_json = _plugin_snapshot_json(snapshot)
    body = canonical_json(
        {
            "checkpoint_id": selected.checkpoint_id,
            "cursor": root.cursor,
            "selected_snapshot_json": selected_json,
            "logical_json": logical_json,
            "bootstrap_json": bootstrap.canonical,
            "actor_user_id": source.bootstrap.actor_user_id,
            "original_request_json": source.bootstrap.request_json,
            "original_user_message": original.message,
            "candidate": state.canonical,
            "admission_policy_json": root.admission_policy_json,
            "admission_policy_hash": root.policy_hash,
            "plugin_policy_hash": snapshot.policy_hash,
            "catalog_snapshot_hash": snapshot.snapshot_hash,
            "plugin_snapshot_json": snapshot_json,
            "tool_manifest_json": tools.canonical,
            "env_ref_names": sorted(names),
            "source_chunks": [
                {"reference": chunk.reference.model_dump(mode="json"), "canonical": chunk.canonical}
                for chunk in (state, evidence, transcript, iteration, tools, predecessor)
            ],
            "unselected_graph_proposal": (
                None
                if proposed is None
                else {"reference": proposed.proposed.reference.model_dump(mode="json"), "canonical": proposed.proposed.canonical}
            ),
        }
    )
    return RawSemanticAdvisorSource(
        checkpoint_id=selected.checkpoint_id,
        cursor=root.cursor,
        selected_snapshot_json=selected_json,
        logical_json=logical_json,
        bootstrap=bootstrap,
        actor_user_id=source.bootstrap.actor_user_id,
        original_request_json=source.bootstrap.request_json,
        original_user_message=original.message,
        state=state,
        evidence=evidence,
        transcript=transcript,
        iteration=iteration,
        tools=tools,
        predecessor_frontier=predecessor,
        proposed_frontier=proposed,
        candidate=state.canonical,
        admission_policy_json=root.admission_policy_json,
        admission_policy_hash=root.policy_hash,
        plugin_policy_hash=snapshot.policy_hash,
        catalog_snapshot_hash=snapshot.snapshot_hash,
        plugin_snapshot_json=snapshot_json,
        tool_manifest_json=tools.canonical,
        env_ref_names=names,
        source_body_json=body,
    )
