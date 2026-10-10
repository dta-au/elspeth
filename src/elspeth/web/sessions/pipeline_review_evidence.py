"""Pure semantic cohort evidence and deterministic opt-out replay."""

from __future__ import annotations

from typing import Literal

from elspeth.contracts.composer_interpretation import (
    InterpretationChoice,
    InterpretationEventRecord,
    InterpretationKind,
    InterpretationSource,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.web.sessions.pending_interpretation import (
    _interpretation_hash_domain_v2,
    _resolve_invented_source,
    _resolve_model_choice_review,
    _resolve_pipeline_decision_review,
    _resolve_prompt_template_review,
    _resolve_source_data_contract,
    _resolve_vague_term,
    _surfacing_prompt_structure_hash,
)
from elspeth.web.sessions.pipeline_settlement_payloads import ReviewCohortMember, ReviewInitialResolution, ReviewSemanticMaterial
from elspeth.web.sessions.protocol import CompositionStateRecord, PreparedInterpretationEventDraft


def review_semantic_material(draft: PreparedInterpretationEventDraft, *, candidate_id: str, ordinal: int) -> ReviewSemanticMaterial:
    return ReviewSemanticMaterial(
        ordinal=ordinal,
        candidate_state_id=candidate_id,
        affected_node_id=draft.affected_node_id,
        kind=draft.kind.value,
        user_term=draft.user_term.strip(),
        llm_draft=draft.llm_draft,
        surface_origin=draft.surface_origin.value,
        model_identifier=draft.model_identifier,
        model_version=draft.model_version,
        provider=draft.provider,
        composer_skill_hash=draft.composer_skill_hash,
    )


def review_initial_resolution(event: InterpretationEventRecord) -> ReviewInitialResolution:
    if event.hash_domain_version not in (None, "v2"):
        raise AuditIntegrityError("review initial hash domain is not current")
    if event.actor != "composer-llm":
        raise AuditIntegrityError("review initial actor is not the owned surface writer")
    source: Literal["user_approved", "auto_interpreted_opt_out"]
    if event.interpretation_source is InterpretationSource.USER_APPROVED:
        source = "user_approved"
    elif event.interpretation_source is InterpretationSource.AUTO_INTERPRETED_OPT_OUT:
        source = "auto_interpreted_opt_out"
    else:
        raise AuditIntegrityError("review initial source is not a surface resolution")
    return ReviewInitialResolution(
        accepted_value=event.accepted_value,
        arguments_hash=event.arguments_hash,
        hash_domain_version="v2" if event.hash_domain_version == "v2" else None,
        approved_prompt_artifact_hash=event.approved_prompt_artifact_hash,
        actor="composer-llm",
        interpretation_source=source,
    )


def review_cohort_member(
    draft: PreparedInterpretationEventDraft,
    *,
    candidate_id: str,
    ordinal: int,
    event: InterpretationEventRecord,
    produced_state: CompositionStateRecord | None,
    produced_hash: str | None,
) -> ReviewCohortMember:
    material = review_semantic_material(draft, candidate_id=candidate_id, ordinal=ordinal)
    resolution = review_initial_resolution(event)
    if event.tool_call_id is None or event.choice not in (InterpretationChoice.PENDING, InterpretationChoice.OPTED_OUT):
        raise AuditIntegrityError("new review member is not a pending/opted-out bound event")
    return ReviewCohortMember(
        ordinal=ordinal,
        candidate_state_id=candidate_id,
        event_id=str(event.id),
        tool_call_id=event.tool_call_id,
        semantic_material=material,
        semantic_hash=material.content_hash(),
        initial_disposition="pending" if event.choice is InterpretationChoice.PENDING else "opted_out",
        initial_resolution=resolution,
        initial_resolution_hash=stable_hash(resolution.model_dump(mode="json")),
        produced_state_id=str(produced_state.id) if produced_state is not None else None,
        produced_state_content_hash=produced_hash,
    )


def verify_review_event_material(member: ReviewCohortMember, event: InterpretationEventRecord) -> None:
    material = member.semantic_material
    if (
        str(event.id) != member.event_id
        or str(event.composition_state_id) != member.candidate_state_id
        or event.tool_call_id != member.tool_call_id
        or event.affected_node_id != material.affected_node_id
        or event.kind is None
        or event.kind.value != material.kind
        or event.user_term is None
        or event.user_term.strip() != material.user_term
        or event.llm_draft != material.llm_draft
        or event.surface_origin is None
        or event.surface_origin.value != material.surface_origin
        or event.model_identifier != material.model_identifier
        or event.model_version != material.model_version
        or event.provider != material.provider
        or event.composer_skill_hash != material.composer_skill_hash
    ):
        raise AuditIntegrityError("accepted review immutable material mismatch")
    if member.initial_disposition == "opted_out" and (
        event.choice.value != "opted_out" or review_initial_resolution(event) != member.initial_resolution
    ):
        raise AuditIntegrityError("accepted opt-out initial resolution mismatch")
    if member.initial_disposition == "opted_out":
        if event.accepted_value is None or event.tool_call_id is None or event.kind is None or event.user_term is None:
            raise AuditIntegrityError("accepted opt-out arguments domain is incomplete")
        domain = _interpretation_hash_domain_v2(
            session_id=str(event.session_id),
            composition_state_id=str(event.composition_state_id),
            affected_node_id=material.affected_node_id,
            tool_call_id=event.tool_call_id,
            user_term=event.user_term,
            kind=event.kind.value,
            llm_draft=material.llm_draft,
            accepted_value=event.accepted_value,
            actor=event.actor,
            model_identifier=event.model_identifier,
            model_version=event.model_version,
            provider=event.provider,
            composer_skill_hash=event.composer_skill_hash,
            context="accepted opt-out replay",
        )
        if stable_hash(domain) != member.initial_resolution.arguments_hash:
            raise AuditIntegrityError("accepted opt-out arguments domain hash mismatch")


def verify_opt_out_transformation(
    member: ReviewCohortMember,
    *,
    previous: CompositionStateRecord,
    produced: CompositionStateRecord,
) -> None:
    """Verify every supported canonical transformation without SQL or provider calls."""
    material = member.semantic_material
    kind = InterpretationKind(material.kind)
    event_id = member.event_id
    node = material.affected_node_id
    term = material.user_term
    draft = material.llm_draft
    accepted = member.initial_resolution.accepted_value
    if accepted is None:
        raise AuditIntegrityError("opt-out replay has no initial accepted material")
    if kind is InterpretationKind.VAGUE_TERM:
        sources, nodes, resolved_hash = _resolve_vague_term(
            previous,
            surfacing_state_record=previous,
            event_id=event_id,
            affected_node_id=node,
            user_term=term,
            llm_draft=draft,
            accepted_value=accepted,
        )
    elif kind is InterpretationKind.LLM_PROMPT_TEMPLATE:
        sources, nodes, resolved_hash = _resolve_prompt_template_review(
            previous,
            event_id=event_id,
            affected_node_id=node,
            user_term=term,
            accepted_value=accepted,
            surfacing_structure_hash=_surfacing_prompt_structure_hash(previous, affected_node_id=node),
        )
    elif kind is InterpretationKind.INVENTED_SOURCE:
        sources, nodes, resolved_hash = _resolve_invented_source(
            previous, event_id=event_id, affected_node_id=node, user_term=term, llm_draft=draft, accepted_value=accepted
        )
    elif kind is InterpretationKind.SOURCE_DATA_CONTRACT:
        sources, nodes, resolved_hash = _resolve_source_data_contract(
            previous, event_id=event_id, affected_node_id=node, user_term=term, llm_draft=draft, accepted_value=accepted
        )
    elif kind is InterpretationKind.PIPELINE_DECISION:
        sources, nodes, resolved_hash = _resolve_pipeline_decision_review(
            previous, event_id=event_id, affected_node_id=node, user_term=term, llm_draft=draft, accepted_value=accepted
        )
    elif kind is InterpretationKind.LLM_MODEL_CHOICE:
        sources, nodes, resolved_hash = _resolve_model_choice_review(
            previous, event_id=event_id, affected_node_id=node, user_term=term, llm_draft=draft, accepted_value=accepted
        )
    else:
        raise AuditIntegrityError("unsupported opt-out replay transformation")
    if (
        produced.derived_from_state_id != previous.id
        or produced.session_id != previous.session_id
        or deep_thaw(produced.sources) != deep_thaw(sources)
        or deep_thaw(produced.nodes) != deep_thaw(nodes)
        or produced.edges != previous.edges
        or produced.outputs != previous.outputs
        or produced.metadata_ != previous.metadata_
        or produced.composer_meta != previous.composer_meta
    ):
        raise AuditIntegrityError("accepted opt-out derived state transformation mismatch")
    if (
        kind in (InterpretationKind.VAGUE_TERM, InterpretationKind.LLM_PROMPT_TEMPLATE)
        and resolved_hash != member.initial_resolution.approved_prompt_artifact_hash
    ):
        raise AuditIntegrityError("accepted opt-out artifact hash mismatch")
