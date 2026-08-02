"""Canonical pending-interpretation policy and validation-only capability.

This module owns no engine, connection, transaction, or session-service handle.
The repository supplies locked immutable state and remains the sole DML owner.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime
from enum import Enum
from inspect import isfunction, ismethod
from types import MappingProxyType, MemberDescriptorType
from typing import TYPE_CHECKING, Any, TypedDict, cast, final
from uuid import UUID

from elspeth.contracts.composer_interpretation import (
    INTERPRETATION_HASH_DOMAIN_V2,
    InterpretationChoice,
    InterpretationKind,
    InterpretationSource,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    PENDING_INTERPRETATION_AUTHORING_TEXT,
    PROMPT_TEMPLATE_PARTS_KEY,
    SOURCE_AUTHORING_KEY,
    SOURCE_COMPONENT_ID,
    model_choice_artifact_hash,
    pipeline_decision_artifact_hash,
    prompt_structure_hash_from_options,
    source_name_from_component_id,
    validate_pipeline_decision_node_semantics,
)
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.guided_replay import validation_errors_for_composer_surface
from elspeth.web.sessions.protocol import (
    CompositionStateData,
    CompositionStateRecord,
    InterpretationNodeMissingError,
    InterpretationNodePluginMutatedError,
    InterpretationPlaceholderConsumedError,
    InterpretationResolveError,
    SessionCompositionStateCreation,
    SessionPendingInterpretationCommand,
    SessionPendingInterpretationDecision,
    SessionPendingInterpretationSnapshot,
    SessionPendingInterpretationValidationCandidate,
    SessionPendingInterpretationValidationResult,
    SessionPendingInterpretationValidator,
)
from elspeth.web.validation import INTERPRETATION_PLACEHOLDER_RE

if TYPE_CHECKING:
    from elspeth.web.catalog.protocol import CatalogService
    from elspeth.web.composer.state import CompositionState, ValidationSummary
    from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
    from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry


class _InterpretationHashDomainV2Payload(TypedDict):
    """Closed hash-domain payload for interpretation review events."""

    session_id: str
    composition_state_id: str | None
    affected_node_id: str
    tool_call_id: str
    user_term: str
    kind: str
    llm_draft: str
    accepted_value: str
    actor: str
    model_identifier: str
    model_version: str
    provider: str
    composer_skill_hash: str


def _interpretation_hash_domain_v2(
    *,
    session_id: str,
    composition_state_id: str | None,
    affected_node_id: str,
    tool_call_id: str,
    user_term: str,
    kind: str,
    llm_draft: str,
    accepted_value: str,
    actor: str,
    model_identifier: str,
    model_version: str,
    provider: str,
    composer_skill_hash: str,
    context: str,
) -> _InterpretationHashDomainV2Payload:
    domain_dict: _InterpretationHashDomainV2Payload = {
        "session_id": session_id,
        "composition_state_id": composition_state_id,
        "affected_node_id": affected_node_id,
        "tool_call_id": tool_call_id,
        "user_term": user_term,
        "kind": kind,
        "llm_draft": llm_draft,
        "accepted_value": accepted_value,
        "actor": actor,
        "model_identifier": model_identifier,
        "model_version": model_version,
        "provider": provider,
        "composer_skill_hash": composer_skill_hash,
    }
    if set(domain_dict.keys()) != INTERPRETATION_HASH_DOMAIN_V2:
        raise AssertionError(
            f"{context}: domain dict keys {set(domain_dict.keys())!r} drifted from "
            f"INTERPRETATION_HASH_DOMAIN_V2 {INTERPRETATION_HASH_DOMAIN_V2!r}"
        )
    return domain_dict


def _require_mapping(value: object, *, message: str) -> Mapping[str, Any]:
    if type(value) not in (dict, MappingProxyType):
        raise InterpretationPlaceholderConsumedError(message)
    return cast(Mapping[str, Any], value)


def _find_llm_transform_node(
    state: CompositionStateRecord,
    *,
    affected_node_id: str,
    context: str,
) -> Mapping[str, Any]:
    if state.nodes is None:
        raise InterpretationNodeMissingError(f"{context}: composition state has no nodes; node {affected_node_id!r} is not present")
    for node in state.nodes:
        if node["id"] != affected_node_id:
            continue
        node_type = node["node_type"] if "node_type" in node else None
        if node_type != "transform" or "plugin" not in node:
            raise InterpretationNodePluginMutatedError(
                f"{context}: node {affected_node_id!r} has no LLM discriminator; expected node_type='transform' with plugin='llm'"
            )
        node_plugin = node["plugin"]
        if node_plugin != "llm":
            raise InterpretationNodePluginMutatedError(
                f"{context}: node {affected_node_id!r} has plugin {node_plugin!r}; only llm nodes carry interpretation review state"
            )
        options = _require_mapping(
            node["options"] if "options" in node else None,
            message=f"{context}: node {affected_node_id!r} has no options mapping",
        )
        if "prompt_template" not in options or type(options["prompt_template"]) is not str:
            raise InterpretationPlaceholderConsumedError(f"{context}: node {affected_node_id!r} options.prompt_template is not a string")
        prompt_template = options["prompt_template"]
        if not prompt_template:
            raise InterpretationPlaceholderConsumedError(
                f"{context}: node {affected_node_id!r} must declare non-empty options.prompt_template"
            )
        return node
    raise InterpretationNodeMissingError(f"{context}: node {affected_node_id!r} is not present in the composition state's nodes")


def _find_interpretation_review_node(
    state: CompositionStateRecord,
    *,
    affected_node_id: str,
    context: str,
) -> Mapping[str, Any]:
    if state.nodes is None:
        raise InterpretationNodeMissingError(f"{context}: composition state has no nodes; node {affected_node_id!r} is not present")
    for node in state.nodes:
        if node["id"] == affected_node_id:
            return node
    raise InterpretationNodeMissingError(f"{context}: node {affected_node_id!r} is not present in the composition state's nodes")


def _node_specs_from_state_record(state_record: CompositionStateRecord) -> tuple[Any, ...]:
    from elspeth.web.composer.state import NodeSpec

    return tuple(NodeSpec.from_dict(dict(n)) for n in state_record.nodes or ())


def _find_node_spec_from_state_record(
    state_record: CompositionStateRecord,
    *,
    affected_node_id: str,
    context: str,
) -> tuple[Any, tuple[Any, ...]]:
    all_nodes_spec = _node_specs_from_state_record(state_record)
    target = next((n for n in all_nodes_spec if n.id == affected_node_id), None)
    if target is None:
        raise InterpretationNodeMissingError(f"{context}: node {affected_node_id!r} is not present in the composition state's nodes")
    return target, all_nodes_spec


def _pipeline_decision_artifact_hash_from_state_record(
    state_record: CompositionStateRecord,
    *,
    affected_node_id: str,
    user_term: str,
) -> str:
    """Compute the canonical pipeline-decision artifact hash from a record DTO.

    Bridge between the persistence layer (dict-shaped nodes on
    :class:`CompositionStateRecord`) and the canonical hash function on
    :class:`NodeSpec`. Both write and read paths use the same projection
    helpers under the hood, so an interpretation_resolve event stores
    exactly the hash that preflight will recompute later.
    """

    target, all_nodes_spec = _find_node_spec_from_state_record(
        state_record,
        affected_node_id=affected_node_id,
        context="_pipeline_decision_artifact_hash_from_state_record",
    )
    return pipeline_decision_artifact_hash(target, all_nodes_spec, user_term=user_term)


def _validate_pipeline_decision_semantics_from_state_record(
    state_record: CompositionStateRecord,
    *,
    affected_node_id: str,
    user_term: str,
    draft: str | None,
    context: str,
) -> None:
    target, all_nodes_spec = _find_node_spec_from_state_record(
        state_record,
        affected_node_id=affected_node_id,
        context=context,
    )
    validate_pipeline_decision_node_semantics(
        node=target,
        all_nodes=all_nodes_spec,
        user_term=user_term,
        draft=draft,
        context=context,
    )


def _matching_pending_requirement_index(
    requirements_value: object,
    *,
    kind: InterpretationKind,
    user_term: str,
    context: str,
) -> tuple[list[dict[str, Any]], int]:
    if type(requirements_value) not in (list, tuple):
        raise InterpretationPlaceholderConsumedError(f"{context}: options.interpretation_requirements is not a list")
    normalized_user_term = user_term.strip()
    requirements: list[dict[str, Any]] = []
    matching_indexes: list[int] = []
    for index, requirement_value in enumerate(cast(Sequence[Any], requirements_value)):
        if type(requirement_value) not in (dict, MappingProxyType):
            raise InterpretationPlaceholderConsumedError(f"{context}: interpretation requirement entry is not a mapping")
        requirement = dict(requirement_value)
        requirement_kind = requirement["kind"] if "kind" in requirement else InterpretationKind.VAGUE_TERM.value
        requirement_term = requirement["user_term"]
        if type(requirement_term) is not str:
            raise InterpretationPlaceholderConsumedError(f"{context}: interpretation requirement user_term is invalid")
        requirement_status = requirement["status"] if "status" in requirement else None
        if requirement_term.strip() == normalized_user_term and requirement_status == "pending" and requirement_kind == kind.value:
            matching_indexes.append(index)
        requirements.append(requirement)
    if len(matching_indexes) != 1:
        raise InterpretationPlaceholderConsumedError(
            f"{context}: does not contain exactly one pending {kind.value!r} requirement for {user_term!r}; found {len(matching_indexes)}"
        )
    return requirements, matching_indexes[0]


def _review_requirement_identity(
    options: Mapping[str, Any],
    *,
    kind: InterpretationKind,
    user_term: str,
    context: str,
) -> Mapping[str, str]:
    requirements, matching_index = _matching_pending_requirement_index(
        options[INTERPRETATION_REQUIREMENTS_KEY] if INTERPRETATION_REQUIREMENTS_KEY in options else None,
        kind=kind,
        user_term=user_term,
        context=context,
    )
    requirement = requirements[matching_index]
    requirement_id = requirement["id"] if "id" in requirement else None
    draft = requirement["draft"] if "draft" in requirement else None
    if type(requirement_id) is not str or not requirement_id:
        raise InterpretationPlaceholderConsumedError(f"{context}: review requirement id is missing or invalid")
    if type(draft) is not str:
        raise InterpretationPlaceholderConsumedError(f"{context}: review requirement draft is missing or invalid")
    return {
        "id": requirement_id,
        "kind": kind.value,
        "user_term": user_term.strip(),
        "draft": draft,
    }


def _reviewed_content_identity(
    state_record: CompositionStateRecord,
    *,
    kind: InterpretationKind,
    affected_node_id: str,
    user_term: str,
    context: str,
) -> str:
    """Canonical identity of the exact content one interpretation event reviews.

    The event row's immutable ``composition_state_id`` is the storage anchor;
    this projection derives the kind-specific reviewed artifact from that
    state. It deliberately excludes unrelated composition fields so a later
    state version can reuse an event only while the reviewed content is
    unchanged.
    """

    domain: dict[str, Any] = {
        "version": 1,
        "kind": kind.value,
        "affected_node_id": affected_node_id,
        "user_term": user_term.strip(),
    }
    if kind is InterpretationKind.INVENTED_SOURCE:
        source_name = source_name_from_component_id(affected_node_id)
        if source_name is None:
            raise InterpretationNodeMissingError(
                f"{context}: invented_source must target a source component ({SOURCE_COMPONENT_ID!r} or {SOURCE_COMPONENT_ID!r}:<name>)"
            )
        sources = _require_mapping(
            state_record.sources,
            message=f"{context}: invented_source requires a persisted sources mapping",
        )
        source = _require_mapping(
            sources[source_name] if source_name in sources else None,
            message=f"{context}: invented_source requires persisted source {source_name!r}",
        )
        options = _require_mapping(
            source["options"] if "options" in source else None,
            message=f"{context}: invented_source requires source.options",
        )
        authoring = _require_mapping(
            options[SOURCE_AUTHORING_KEY] if SOURCE_AUTHORING_KEY in options else None,
            message=f"{context}: invented_source requires source.options.{SOURCE_AUTHORING_KEY}",
        )
        content_hash = authoring["content_hash"] if "content_hash" in authoring else None
        if type(content_hash) is not str or not content_hash:
            raise InterpretationPlaceholderConsumedError(f"{context}: source.options.{SOURCE_AUTHORING_KEY}.content_hash must be populated")
        domain["requirement"] = _review_requirement_identity(
            options,
            kind=kind,
            user_term=user_term,
            context=context,
        )
        domain["artifact_hash"] = content_hash
        return stable_hash(domain)

    if kind is InterpretationKind.PIPELINE_DECISION:
        node = _find_interpretation_review_node(
            state_record,
            affected_node_id=affected_node_id,
            context=context,
        )
    else:
        node = _find_llm_transform_node(
            state_record,
            affected_node_id=affected_node_id,
            context=context,
        )
    options = _require_mapping(
        node["options"] if "options" in node else None,
        message=f"{context}: node {affected_node_id!r} options is not a mapping",
    )

    if kind is InterpretationKind.VAGUE_TERM:
        raw_requirements = options[INTERPRETATION_REQUIREMENTS_KEY] if INTERPRETATION_REQUIREMENTS_KEY in options else None
        structured_requirements = cast(Sequence[Any], raw_requirements) if type(raw_requirements) in (list, tuple) else ()
        structured_match = any(
            type(requirement) in (dict, MappingProxyType)
            and (requirement["kind"] if "kind" in requirement else InterpretationKind.VAGUE_TERM.value)
            == InterpretationKind.VAGUE_TERM.value
            and type(requirement["user_term"] if "user_term" in requirement else None) is str
            and requirement["user_term"].strip() == user_term.strip()
            for requirement in structured_requirements
        )
        if structured_match:
            requirement_identity = _review_requirement_identity(
                options,
                kind=kind,
                user_term=user_term,
                context=context,
            )
            structure_hash = prompt_structure_hash_from_options(options)
            if structure_hash is None:
                raise InterpretationPlaceholderConsumedError(
                    f"{context}: structured vague-term review requires options.{PROMPT_TEMPLATE_PARTS_KEY}"
                )
            parts = options[PROMPT_TEMPLATE_PARTS_KEY]
            if not any(
                type(part) in (dict, MappingProxyType)
                and (part["kind"] if "kind" in part else None) == "interpretation_ref"
                and (part["requirement_id"] if "requirement_id" in part else None) == requirement_identity["id"]
                for part in parts
            ):
                raise InterpretationPlaceholderConsumedError(
                    f"{context}: structured vague-term review has no prompt part for its requirement"
                )
            domain["requirement"] = requirement_identity
            domain["prompt_structure_hash"] = structure_hash
        else:
            prompt_template = options["prompt_template"] if "prompt_template" in options else None
            if type(prompt_template) is not str:
                raise InterpretationPlaceholderConsumedError(f"{context}: legacy vague-term review requires options.prompt_template")
            domain["legacy_prompt_hash"] = stable_hash(prompt_template)
        return stable_hash(domain)

    domain["requirement"] = _review_requirement_identity(
        options,
        kind=kind,
        user_term=user_term,
        context=context,
    )
    if kind is InterpretationKind.LLM_PROMPT_TEMPLATE:
        structure_hash = prompt_structure_hash_from_options(options)
        if structure_hash is None:
            prompt_template = options["prompt_template"] if "prompt_template" in options else None
            if type(prompt_template) is not str:
                raise InterpretationPlaceholderConsumedError(f"{context}: llm_prompt_template review requires options.prompt_template")
            structure_hash = stable_hash(prompt_template)
        domain["artifact_hash"] = structure_hash
    elif kind is InterpretationKind.LLM_MODEL_CHOICE:
        model = options["model"] if "model" in options else None
        if type(model) is not str or not model:
            raise InterpretationPlaceholderConsumedError(f"{context}: llm_model_choice review requires a non-empty options.model")
        domain["artifact_hash"] = model_choice_artifact_hash(model)
    elif kind is InterpretationKind.PIPELINE_DECISION:
        domain["artifact_hash"] = _pipeline_decision_artifact_hash_from_state_record(
            state_record,
            affected_node_id=affected_node_id,
            user_term=user_term,
        )
    else:
        raise AssertionError(f"unhandled InterpretationKind {kind!r}")
    return stable_hash(domain)


# Prefixes (case-insensitive) that, when they appear immediately before the
# placeholder, indicate the LLM placed the placeholder inside a structural
# directive rather than in the prompt body. Substituting the user's
# accepted_value into a structural-directive position would produce a
# broken prompt at runtime — fail closed.
#
# CLOSED LIST — extending this set is a governance action for the
# prompt-template patch helper. Any new prefix must be paired with a
# direct-helper unit test and a writer-path audit.
_STRUCTURAL_DIRECTIVE_PREFIXES: tuple[str, ...] = (
    "system:",
    "role:",
    "instructions:",
)


def _patch_structured_interpretation_prompt(
    *,
    options: Mapping[str, Any],
    affected_node_id: str,
    user_term: str,
    accepted_value: str,
    event_id: str | None = None,
    llm_draft: str | None = None,
) -> dict[str, Any] | None:
    """Resolve structured interpretation metadata, returning patched options.

    ``None`` means the node does not carry structured interpretation state and
    the caller should fall back to the legacy sentinel-string path.
    """

    if INTERPRETATION_REQUIREMENTS_KEY not in options:
        return None
    requirements_value = options[INTERPRETATION_REQUIREMENTS_KEY]
    if type(requirements_value) not in (list, tuple):
        raise InterpretationPlaceholderConsumedError(
            f"_patch_llm_transform_prompt: node {affected_node_id!r} options.interpretation_requirements is not a list"
        )

    matching_indexes: list[int] = []
    normalized_user_term = user_term.strip()
    requirements: list[dict[str, Any]] = []
    requirements_by_id: dict[str, Mapping[str, Any]] = {}
    for index, requirement_value in enumerate(requirements_value):
        if type(requirement_value) not in (dict, MappingProxyType):
            raise InterpretationPlaceholderConsumedError(
                f"_patch_llm_transform_prompt: node {affected_node_id!r} interpretation requirement entry is not a mapping"
            )
        requirement = dict(requirement_value)
        requirement_id = requirement["id"]
        requirement_term = requirement["user_term"]
        if type(requirement_id) is not str or not requirement_id:
            raise InterpretationPlaceholderConsumedError(
                f"_patch_llm_transform_prompt: node {affected_node_id!r} interpretation requirement id is invalid"
            )
        if type(requirement_term) is not str:
            raise InterpretationPlaceholderConsumedError(
                f"_patch_llm_transform_prompt: node {affected_node_id!r} interpretation requirement user_term is invalid"
            )
        if requirement_id in requirements_by_id:
            raise InterpretationPlaceholderConsumedError(
                f"_patch_llm_transform_prompt: duplicate interpretation requirement id {requirement_id!r}"
            )
        requirements_by_id[requirement_id] = requirement
        requirement_kind = requirement["kind"] if "kind" in requirement else InterpretationKind.VAGUE_TERM.value
        requirement_status = requirement["status"] if "status" in requirement else None
        if (
            requirement_term.strip() == normalized_user_term
            and requirement_status == "pending"
            and requirement_kind == InterpretationKind.VAGUE_TERM.value
        ):
            matching_indexes.append(index)
        requirements.append(requirement)

    # A node can legitimately carry interpretation_requirements for OTHER kinds
    # (the prompt-template and model-choice auto-stagers add one each to every
    # LLM node) while its vague term is wired by a legacy
    # ``{{interpretation:<term>}}`` placeholder. When no pending vague_term
    # requirement matches this user_term, the structured path does not apply —
    # fall back to the legacy placeholder path rather than demanding
    # prompt_template_parts the node never needed.
    if not matching_indexes:
        return None
    if len(matching_indexes) != 1:
        raise InterpretationPlaceholderConsumedError(
            f"_patch_llm_transform_prompt: node {affected_node_id!r} does not contain exactly one pending "
            f"interpretation requirement for {user_term!r}; found {len(matching_indexes)}"
        )

    # The vague term is structured (a matching requirement exists); the prompt
    # parts that carry its substitution are now required.
    parts_value = options[PROMPT_TEMPLATE_PARTS_KEY] if PROMPT_TEMPLATE_PARTS_KEY in options else None
    if type(parts_value) not in (list, tuple):
        raise InterpretationPlaceholderConsumedError(
            f"_patch_llm_transform_prompt: node {affected_node_id!r} options.prompt_template_parts is required for structured interpretation resolution"
        )

    matching_index = matching_indexes[0]
    matching_requirement = requirements[matching_index]
    matching_requirement_id = matching_requirement["id"]
    if llm_draft is not None:
        current_draft = matching_requirement["draft"] if "draft" in matching_requirement else None
        if type(current_draft) is not str or current_draft != llm_draft:
            raise InterpretationPlaceholderConsumedError(
                f"_patch_llm_transform_prompt: vague_term event draft no longer matches "
                f"the current review requirement on node {affected_node_id!r}"
            )

    rendered: list[str] = []
    matched_ref_count = 0
    for part_value in cast(Sequence[Any], parts_value):
        if type(part_value) not in (dict, MappingProxyType):
            raise InterpretationPlaceholderConsumedError(
                f"_patch_llm_transform_prompt: node {affected_node_id!r} prompt_template_parts entry is not a mapping"
            )
        kind = part_value["kind"]
        if kind == "text":
            text = part_value["text"]
            if type(text) is not str:
                raise InterpretationPlaceholderConsumedError(
                    f"_patch_llm_transform_prompt: node {affected_node_id!r} text prompt part is not a string"
                )
            rendered.append(text)
            continue
        if kind != "interpretation_ref":
            raise InterpretationPlaceholderConsumedError(
                f"_patch_llm_transform_prompt: node {affected_node_id!r} unknown prompt part kind {kind!r}"
            )
        requirement_id = part_value["requirement_id"]
        if type(requirement_id) is not str or requirement_id not in requirements_by_id:
            raise InterpretationPlaceholderConsumedError(
                f"_patch_llm_transform_prompt: node {affected_node_id!r} prompt part references unknown interpretation requirement"
            )
        stored_requirement = requirements_by_id[requirement_id]
        if requirement_id == matching_requirement_id:
            prefix_lower = "".join(rendered).rstrip().lower()
            for directive in _STRUCTURAL_DIRECTIVE_PREFIXES:
                if prefix_lower.endswith(directive):
                    raise InterpretationPlaceholderConsumedError(
                        f"_patch_llm_transform_prompt: interpretation requirement {requirement_id!r} in node "
                        f"{affected_node_id!r} is immediately preceded by structural directive {directive!r}; "
                        f"substituting into a directive position would produce a broken prompt"
                    )
            matched_ref_count += 1
            rendered.append(accepted_value)
            continue
        stored_status = stored_requirement["status"] if "status" in stored_requirement else None
        if stored_status == "resolved":
            accepted = stored_requirement["accepted_value"] if "accepted_value" in stored_requirement else None
            if type(accepted) is not str:
                raise InterpretationPlaceholderConsumedError(
                    f"_patch_llm_transform_prompt: resolved interpretation requirement {requirement_id!r} has no accepted value"
                )
            rendered.append(accepted)
            continue
        rendered.append(PENDING_INTERPRETATION_AUTHORING_TEXT)

    # Defense-in-depth backstop: the matched requirement must be referenced by
    # at least one ``interpretation_ref`` part, or the accepted value never
    # lands in the rendered prompt — a "resolved" review whose decision silently
    # never reaches the runtime, i.e. the exact audit divergence CLAUDE.md
    # forbids. Unreachable once the staging gate (vague_term_wiring_count) holds;
    # present so a bypass crashes loudly instead of corrupting the prompt.
    if matched_ref_count == 0:
        raise InterpretationPlaceholderConsumedError(
            f"_patch_llm_transform_prompt: node {affected_node_id!r} prompt_template_parts contains no "
            f"interpretation_ref part referencing the resolved requirement {matching_requirement_id!r}; "
            "the accepted interpretation value would be silently dropped from the prompt"
        )

    new_template = "".join(rendered)
    resolved_prompt_template_hash = stable_hash(new_template)
    updated_requirement = dict(matching_requirement)
    updated_requirement["status"] = "resolved"
    if event_id is not None:
        updated_requirement["event_id"] = event_id
    updated_requirement["accepted_value"] = accepted_value
    # The requirement-level hash attests THIS requirement's accepted value, not
    # the full render: the render changes again when a sibling vague term
    # resolves, and reconciliation must be able to re-verify every resolved
    # requirement against state that survives those later resolutions. The
    # full-render hash lives at node level (options.resolved_prompt_template_hash).
    updated_requirement["resolved_prompt_template_hash"] = stable_hash(accepted_value)
    requirements[matching_index] = updated_requirement

    patched_options = dict(options)
    patched_options["prompt_template"] = new_template
    patched_options["resolved_prompt_template_hash"] = resolved_prompt_template_hash
    patched_options[INTERPRETATION_REQUIREMENTS_KEY] = requirements
    return patched_options


def _patch_llm_transform_prompt(
    state: CompositionStateRecord,
    *,
    affected_node_id: str,
    user_term: str,
    accepted_value: str,
    event_id: str | None = None,
    llm_draft: str | None = None,
) -> Sequence[Mapping[str, Any]]:
    """Return a new ``nodes`` JSON sequence with the LLM transform's prompt
    template patched to embed ``accepted_value`` for ``user_term``.

    The prompt-template patch convention: the LLM transform's
    ``options.prompt_template`` field contains exactly one
    ``{{interpretation:<term>}}`` placeholder that the LLM writes when it
    first stages the LLM transform. This helper substitutes the placeholder
    with the user's ``accepted_value`` and writes the result back into
    ``options.prompt_template``.

    The ``prompt_template`` field lives **inside the node's ``options``
    mapping** because that is the shape ``CompositionState.NodeSpec``
    consumes (``node.options["prompt_template"]``) and the shape
    ``yaml_generator.generate_pipeline_dict`` emits to the runtime engine.
    The LLM discriminator is the production ``CompositionState.to_dict()``
    shape: ``node_type == "transform"`` and ``plugin == "llm"``.

    Raises typed :class:`InterpretationResolveError` subclasses when:

    * the affected node is not present in ``state.nodes``;
    * the affected node is not a transform node with ``plugin == 'llm'``;
    * the affected node has no ``options`` mapping;
    * ``options.prompt_template`` is missing or not a string;
    * the prompt template does not contain the expected placeholder;
    * the placeholder appears more than once in the template;
    * the prefix immediately before the placeholder matches (case-insensitive)
      any of :data:`_STRUCTURAL_DIRECTIVE_PREFIXES`.

    The helper is pure (no DB IO). It is called from
    :meth:`SessionServiceImpl.resolve_interpretation_event` BEFORE the
    composition-state UPDATE so any raise short-circuits the resolve
    transaction cleanly.
    """
    if state.nodes is None:
        raise InterpretationNodeMissingError(
            f"_patch_llm_transform_prompt: composition state has no nodes; node {affected_node_id!r} is not present"
        )

    patched_nodes: list[Mapping[str, Any]] = []
    found = False
    for node in state.nodes:
        if node["id"] != affected_node_id:
            patched_nodes.append(node)
            continue

        found = True

        node_type = node["node_type"] if "node_type" in node else None
        if node_type != "transform" or "plugin" not in node:
            raise InterpretationNodePluginMutatedError(
                f"_patch_llm_transform_prompt: node {affected_node_id!r} has no LLM discriminator; "
                "expected node_type='transform' with plugin='llm'"
            )

        node_plugin = node["plugin"]
        if node_plugin != "llm":
            raise InterpretationNodePluginMutatedError(
                f"_patch_llm_transform_prompt: node {affected_node_id!r} has plugin "
                f"{node_plugin!r}; only llm nodes carry interpretation placeholders"
            )

        # ``composition_states.nodes`` is Tier-1 (our own audit data) but
        # stored as schemaless JSON. Membership checks here are an
        # offensive pattern: assert the invariant, raise a structured
        # ValueError with a precise message. Direct indexing on the
        # ``Mapping[str, Any]`` annotation lets a wrong type surface as a
        # KeyError/TypeError at the operation site — informative crash
        # rather than fabricated default, per CLAUDE.md offensive
        # programming rules.
        if "options" not in node:
            raise InterpretationPlaceholderConsumedError(
                f"_patch_llm_transform_prompt: node {affected_node_id!r} has no options "
                f"mapping; expected options.prompt_template carrying the placeholder"
            )
        options_value = node["options"]
        if not isinstance(options_value, Mapping):
            raise InterpretationPlaceholderConsumedError(
                f"_patch_llm_transform_prompt: node {affected_node_id!r} options is not a mapping; "
                "expected options.prompt_template carrying the placeholder"
            )
        options: Mapping[str, Any] = options_value

        if "prompt_template" not in options:
            raise InterpretationPlaceholderConsumedError(
                f"_patch_llm_transform_prompt: node {affected_node_id!r} has no options.prompt_template field"
            )

        template_value = options["prompt_template"]
        if not isinstance(template_value, str):
            raise InterpretationPlaceholderConsumedError(
                f"_patch_llm_transform_prompt: node {affected_node_id!r} options.prompt_template is not a string"
            )
        template = template_value

        structured_options = _patch_structured_interpretation_prompt(
            options=options,
            affected_node_id=affected_node_id,
            user_term=user_term,
            accepted_value=accepted_value,
            event_id=event_id,
            llm_draft=llm_draft,
        )
        if structured_options is not None:
            patched_node = dict(node)
            patched_node["options"] = structured_options
            patched_nodes.append(patched_node)
            continue

        placeholder_matches = [match for match in INTERPRETATION_PLACEHOLDER_RE.finditer(template) if match.group(1).strip() == user_term]
        placeholder = f"{{{{interpretation:{user_term}}}}}"
        if not placeholder_matches:
            raise InterpretationPlaceholderConsumedError(
                f"_patch_llm_transform_prompt: node {affected_node_id!r} options.prompt_template does not contain placeholder {placeholder!r}"
            )

        # Count occurrences. Exactly one is required; more is a structural
        # error (the LLM emitted an ambiguous template; we cannot know which
        # site the user resolution should bind to).
        if len(placeholder_matches) != 1:
            raise InterpretationPlaceholderConsumedError(
                f"_patch_llm_transform_prompt: placeholder {placeholder!r} appears "
                f"{len(placeholder_matches)} times in node {affected_node_id!r}'s options.prompt_template; "
                f"the placeholder must appear exactly once"
            )
        placeholder_match = placeholder_matches[0]

        # Structural-directive guard: the substring ending at the placeholder
        # is the "prefix immediately before". We strip trailing whitespace
        # (so "System: {{...}}" is caught even though there's a space before
        # the placeholder) and compare case-insensitively against the closed
        # prefix list.
        prefix = template[: placeholder_match.start()].rstrip()
        prefix_lower = prefix.lower()
        for directive in _STRUCTURAL_DIRECTIVE_PREFIXES:
            if prefix_lower.endswith(directive):
                raise InterpretationPlaceholderConsumedError(
                    f"_patch_llm_transform_prompt: placeholder {placeholder!r} in node "
                    f"{affected_node_id!r} is immediately preceded by structural "
                    f"directive {directive!r}; substituting into a directive position "
                    f"would produce a broken prompt"
                )

        # Patch is a single span replacement; we already verified exactly one
        # matching placeholder so this is unambiguous. The resolved string is written
        # back into ``options.prompt_template`` so it lands on
        # ``NodeSpec.options`` after ``state_from_record`` and flows into the
        # runtime YAML emitted by ``generate_pipeline_dict``. The same helper
        # also writes the resolved-prompt-template hash into
        # ``options.resolved_prompt_template_hash`` (the cross-DB anchor
        # the LLM transform plugin reads at execution time to populate the
        # Landscape ``calls.resolved_prompt_template_hash`` column).
        new_template = f"{template[: placeholder_match.start()]}{accepted_value}{template[placeholder_match.end() :]}"
        patched_node = dict(node)
        patched_options = dict(options)
        patched_options["prompt_template"] = new_template
        patched_node["options"] = patched_options
        patched_nodes.append(patched_node)

    if not found:
        raise InterpretationNodeMissingError(
            f"_patch_llm_transform_prompt: node {affected_node_id!r} is not present in the composition state's nodes"
        )

    return patched_nodes


def _resolve_vague_term(
    state_record: CompositionStateRecord,
    *,
    surfacing_state_record: CompositionStateRecord | None,
    event_id: str,
    affected_node_id: str,
    user_term: str,
    llm_draft: str,
    accepted_value: str,
) -> tuple[Mapping[str, Mapping[str, Any]] | None, list[Mapping[str, Any]], str]:
    live_node = _find_llm_transform_node(
        state_record,
        affected_node_id=affected_node_id,
        context="resolve_interpretation_event",
    )
    live_options = _require_mapping(
        live_node["options"],
        message=f"resolve_interpretation_event: node {affected_node_id!r} options is not a mapping",
    )
    requirements_value = live_options[INTERPRETATION_REQUIREMENTS_KEY] if INTERPRETATION_REQUIREMENTS_KEY in live_options else None
    structured_requirements = cast(Sequence[Any], requirements_value) if type(requirements_value) in (list, tuple) else ()
    has_structured_site = any(
        type(requirement) in (dict, MappingProxyType)
        and (requirement["kind"] if "kind" in requirement else InterpretationKind.VAGUE_TERM.value) == InterpretationKind.VAGUE_TERM.value
        and type(requirement["user_term"] if "user_term" in requirement else None) is str
        and requirement["user_term"].strip() == user_term.strip()
        and (requirement["status"] if "status" in requirement else None) == "pending"
        for requirement in structured_requirements
    )
    if not has_structured_site:
        if surfacing_state_record is None:
            raise InterpretationPlaceholderConsumedError("resolve_interpretation_event: legacy vague-term review has no surfacing state")
        surfacing_node = _find_llm_transform_node(
            surfacing_state_record,
            affected_node_id=affected_node_id,
            context="resolve_interpretation_event",
        )
        surfacing_options = _require_mapping(
            surfacing_node["options"],
            message=f"resolve_interpretation_event: surfacing node {affected_node_id!r} options is not a mapping",
        )
        surfacing_prompt = surfacing_options["prompt_template"] if "prompt_template" in surfacing_options else None
        live_prompt = live_options["prompt_template"] if "prompt_template" in live_options else None
        if surfacing_prompt != live_prompt:
            raise InterpretationPlaceholderConsumedError(
                "resolve_interpretation_event: legacy vague-term prompt no longer matches the review surface"
            )
    patched_nodes = _patch_llm_transform_prompt(
        state_record,
        affected_node_id=affected_node_id,
        user_term=user_term,
        accepted_value=accepted_value,
        event_id=event_id,
        llm_draft=llm_draft,
    )
    patched_node = next(n for n in patched_nodes if n["id"] == affected_node_id)
    resolved_template: str = patched_node["options"]["prompt_template"]
    resolved_prompt_template_hash = stable_hash(resolved_template)

    final_nodes: list[Mapping[str, Any]] = []
    for n in patched_nodes:
        if n["id"] == affected_node_id:
            node_with_hash = dict(n)
            options_with_hash = dict(n["options"])
            options_with_hash["resolved_prompt_template_hash"] = resolved_prompt_template_hash
            node_with_hash["options"] = options_with_hash
            final_nodes.append(node_with_hash)
        else:
            final_nodes.append(n)
    # Vague-term review patches only nodes; the sources map is carried forward
    # unchanged. The legacy singular ``source`` column is dead.
    return state_record.sources, final_nodes, resolved_prompt_template_hash


def _surfacing_prompt_structure_hash(
    surfacing_state_record: CompositionStateRecord | None,
    *,
    affected_node_id: str,
) -> str | None:
    """Skeleton hash of the prompt the user reviewed at surfacing time.

    The ``llm_prompt_template`` review approves the LLM-authored prompt
    *skeleton* (text segments + the requirement each slot references), which
    :func:`prompt_structure_hash` deliberately makes invariant under
    interpretation resolution. Comparing this surfacing skeleton to the live
    skeleton at resolve time distinguishes a benign vague-term bake (skeleton
    unchanged → accept) from a genuine prompt edit (skeleton changed → reject as
    stale).

    Returns ``None`` when the surfacing state, the affected node, or its prompt
    parts are unavailable (legacy no-parts nodes) — the caller then falls back
    to rendered-text equality.
    """
    if surfacing_state_record is None:
        return None
    for node in surfacing_state_record.nodes or ():
        if node["id"] == affected_node_id:
            options = node["options"] if "options" in node else None
            if type(options) in (dict, MappingProxyType):
                return prompt_structure_hash_from_options(cast(Mapping[str, Any], options))
            return None
    return None


def _resolve_prompt_template_review(
    state_record: CompositionStateRecord,
    *,
    event_id: str,
    affected_node_id: str,
    user_term: str,
    accepted_value: str,
    surfacing_structure_hash: str | None,
) -> tuple[Mapping[str, Mapping[str, Any]] | None, list[Mapping[str, Any]], str]:
    node = _find_llm_transform_node(
        state_record,
        affected_node_id=affected_node_id,
        context="resolve_interpretation_event",
    )
    options = _require_mapping(
        node["options"],
        message=f"resolve_interpretation_event: node {affected_node_id!r} options is not a mapping",
    )
    prompt_template = options["prompt_template"]
    if type(prompt_template) is not str:
        raise InterpretationPlaceholderConsumedError(
            f"resolve_interpretation_event: node {affected_node_id!r} options.prompt_template is not a string"
        )
    # Acceptance gate: the user is approving the prompt SKELETON the LLM authored
    # (text segments + the requirement each slot references). For a structured
    # node (prompt_template_parts present) the skeleton hash is invariant under
    # vague-term resolution — resolving a sibling vague_term first rewrites the
    # rendered options.prompt_template but PRESERVES the parts — so we gate on
    # skeleton equality, NOT rendered-text equality. Gating on rendered text
    # permanently bricked this review whenever a sibling vague_term was resolved
    # first (elspeth-e51216d305: the frozen surfacing draft could never again
    # equal the post-bake template). A genuine prompt edit changes the skeleton
    # and is still rejected as stale. Legacy no-parts nodes have no skeleton;
    # they fall back to the original rendered-text equality.
    live_structure_hash = prompt_structure_hash_from_options(options)
    if live_structure_hash is not None or surfacing_structure_hash is not None:
        if live_structure_hash != surfacing_structure_hash:
            raise InterpretationPlaceholderConsumedError(
                "resolve_interpretation_event: llm_prompt_template prompt skeleton no longer matches the structure the review approved"
            )
    elif accepted_value != prompt_template:
        raise InterpretationPlaceholderConsumedError(
            "resolve_interpretation_event: llm_prompt_template accepted value must equal current options.prompt_template"
        )
    requirements, matching_index = _matching_pending_requirement_index(
        options[INTERPRETATION_REQUIREMENTS_KEY] if INTERPRETATION_REQUIREMENTS_KEY in options else None,
        kind=InterpretationKind.LLM_PROMPT_TEMPLATE,
        user_term=user_term,
        context="resolve_interpretation_event",
    )
    # Node-level / returned hash stays the final-prompt-string hash (the runtime
    # LLM plugin reads options.resolved_prompt_template_hash to populate
    # calls.resolved_prompt_template_hash). The REQUIREMENT-level attestation
    # anchor, by contrast, is the prompt *skeleton* for structured nodes: the
    # prompt-template review approves the LLM-authored structure, while the
    # vague-term reviews approve the slot values. Anchoring the requirement to
    # the skeleton keeps it invariant under vague-term resolution (which rewrites
    # the rendered prompt) — see interpretation_state.prompt_structure_hash.
    resolved_prompt_template_hash = stable_hash(prompt_template)
    structure_hash = prompt_structure_hash_from_options(options)
    requirement_anchor_hash = structure_hash if structure_hash is not None else resolved_prompt_template_hash
    requirement = dict(requirements[matching_index])
    requirement["status"] = "resolved"
    requirement["event_id"] = event_id
    requirement["accepted_value"] = accepted_value
    requirement["resolved_prompt_template_hash"] = requirement_anchor_hash
    requirements[matching_index] = requirement

    final_nodes: list[Mapping[str, Any]] = []
    for current_node in state_record.nodes or ():
        if current_node["id"] == affected_node_id:
            patched_node = dict(current_node)
            patched_options = dict(options)
            patched_options["resolved_prompt_template_hash"] = resolved_prompt_template_hash
            patched_options[INTERPRETATION_REQUIREMENTS_KEY] = requirements
            patched_node["options"] = patched_options
            final_nodes.append(patched_node)
        else:
            final_nodes.append(current_node)
    # Prompt-template review patches only node review metadata; the sources map
    # is carried forward unchanged. The legacy singular ``source`` column is dead.
    return state_record.sources, final_nodes, resolved_prompt_template_hash


def _resolve_invented_source(
    state_record: CompositionStateRecord,
    *,
    event_id: str,
    affected_node_id: str,
    user_term: str,
    llm_draft: str,
    accepted_value: str,
) -> tuple[Mapping[str, Mapping[str, Any]], list[Mapping[str, Any]], None]:
    source_name = source_name_from_component_id(affected_node_id)
    if source_name is None:
        raise InterpretationNodeMissingError(
            "resolve_interpretation_event: invented_source must target a source component "
            f"({SOURCE_COMPONENT_ID!r} or {SOURCE_COMPONENT_ID!r}:<name>)"
        )
    sources_map = _require_mapping(
        state_record.sources,
        message="resolve_interpretation_event: invented_source requires a persisted sources mapping",
    )
    source = _require_mapping(
        sources_map[source_name] if source_name in sources_map else None,
        message=f"resolve_interpretation_event: invented_source requires persisted source {source_name!r}",
    )
    options = _require_mapping(
        source["options"] if "options" in source else None,
        message="resolve_interpretation_event: invented_source requires source.options",
    )
    source_authoring = _require_mapping(
        options[SOURCE_AUTHORING_KEY] if SOURCE_AUTHORING_KEY in options else None,
        message=f"resolve_interpretation_event: invented_source requires source.options.{SOURCE_AUTHORING_KEY}",
    )
    content_hash = source_authoring["content_hash"] if "content_hash" in source_authoring else None
    if type(content_hash) is not str or not content_hash:
        raise InterpretationPlaceholderConsumedError(
            f"resolve_interpretation_event: source.options.{SOURCE_AUTHORING_KEY}.content_hash must be populated"
        )
    requirements, matching_index = _matching_pending_requirement_index(
        options[INTERPRETATION_REQUIREMENTS_KEY] if INTERPRETATION_REQUIREMENTS_KEY in options else None,
        kind=InterpretationKind.INVENTED_SOURCE,
        user_term=user_term,
        context="resolve_interpretation_event",
    )
    requirement = dict(requirements[matching_index])
    draft = requirement["draft"] if "draft" in requirement else None
    if type(draft) is not str or draft != llm_draft:
        raise InterpretationPlaceholderConsumedError(
            "resolve_interpretation_event: invented_source event draft does not match the source review requirement draft"
        )
    requirement["status"] = "resolved"
    requirement["event_id"] = event_id
    requirement["accepted_value"] = accepted_value
    requirement["accepted_artifact_hash"] = content_hash
    requirements[matching_index] = requirement

    patched_authoring = dict(source_authoring)
    patched_authoring["review_event_id"] = event_id
    patched_authoring["resolved_kind"] = InterpretationKind.INVENTED_SOURCE.value
    patched_options = dict(options)
    patched_options[SOURCE_AUTHORING_KEY] = patched_authoring
    patched_options[INTERPRETATION_REQUIREMENTS_KEY] = requirements
    patched_source = dict(source)
    patched_source["options"] = patched_options
    # Splice only the reviewed source back into the sources map. Every sibling
    # source carries its own independent review authority and is left untouched.
    patched_sources = dict(sources_map)
    patched_sources[source_name] = patched_source
    return patched_sources, list(state_record.nodes or ()), None


def _resolve_pipeline_decision_review(
    state_record: CompositionStateRecord,
    *,
    event_id: str,
    affected_node_id: str,
    user_term: str,
    llm_draft: str,
    accepted_value: str,
) -> tuple[Mapping[str, Mapping[str, Any]] | None, list[Mapping[str, Any]], None]:
    node = _find_interpretation_review_node(
        state_record,
        affected_node_id=affected_node_id,
        context="resolve_interpretation_event",
    )
    options = _require_mapping(
        node["options"] if "options" in node else None,
        message=f"resolve_interpretation_event: node {affected_node_id!r} options is not a mapping",
    )
    requirements, matching_index = _matching_pending_requirement_index(
        options[INTERPRETATION_REQUIREMENTS_KEY] if INTERPRETATION_REQUIREMENTS_KEY in options else None,
        kind=InterpretationKind.PIPELINE_DECISION,
        user_term=user_term,
        context="resolve_interpretation_event",
    )
    requirement = dict(requirements[matching_index])
    draft = requirement["draft"] if "draft" in requirement else None
    if type(draft) is not str or draft != llm_draft:
        raise InterpretationPlaceholderConsumedError(
            "resolve_interpretation_event: pipeline_decision event draft does not match the node review requirement draft"
        )
    _validate_pipeline_decision_semantics_from_state_record(
        state_record,
        affected_node_id=affected_node_id,
        user_term=user_term,
        draft=draft,
        context="resolve_interpretation_event",
    )
    decision_hash = _pipeline_decision_artifact_hash_from_state_record(
        state_record,
        affected_node_id=affected_node_id,
        user_term=user_term,
    )
    requirement["status"] = "resolved"
    requirement["event_id"] = event_id
    requirement["accepted_value"] = accepted_value
    requirement["accepted_artifact_hash"] = decision_hash
    requirements[matching_index] = requirement

    final_nodes: list[Mapping[str, Any]] = []
    for current_node in state_record.nodes or ():
        if current_node["id"] == affected_node_id:
            patched_node = dict(current_node)
            patched_options = dict(options)
            patched_options[INTERPRETATION_REQUIREMENTS_KEY] = requirements
            patched_node["options"] = patched_options
            final_nodes.append(patched_node)
        else:
            final_nodes.append(current_node)
    # Pipeline-decision review patches only node review metadata; the sources map
    # is carried forward unchanged. The legacy singular ``source`` column is dead.
    return state_record.sources, final_nodes, None


def _resolve_model_choice_review(
    state_record: CompositionStateRecord,
    *,
    event_id: str,
    affected_node_id: str,
    user_term: str,
    llm_draft: str,
    accepted_value: str,
) -> tuple[Mapping[str, Mapping[str, Any]] | None, list[Mapping[str, Any]], None]:
    """Resolve an ``llm_model_choice`` review on an LLM node.

    Parallel to :func:`_resolve_pipeline_decision_review`. The reviewed
    artifact is the LLM node's ``options.model`` identifier, which the
    composer authored and the mutation-time auto-stager
    (:func:`elspeth.web.interpretation_state._options_with_default_model_choice_review`)
    surfaced for review. Resolving stamps the requirement and writes
    ``accepted_value`` into ``options.model`` so the audit record (what the
    operator approved) and the runnable pipeline (what executes) cannot
    diverge:

    * ``accepted_as_drafted`` — ``accepted_value`` equals the existing
      ``options.model`` (the drafted identifier); the write is idempotent.
    * ``amended`` — ``accepted_value`` is the operator's substituted model
      identifier; the write applies it so the node runs the approved model.

    No prompt-template patch occurs (model choice is a different field than
    the prompt), so the resolved-prompt-template hash is ``None``.
    """
    node = _find_interpretation_review_node(
        state_record,
        affected_node_id=affected_node_id,
        context="resolve_interpretation_event",
    )
    plugin = node["plugin"] if "plugin" in node else None
    if plugin != "llm":
        # Tier 1 invariant: an llm_model_choice requirement is only ever
        # auto-staged on an llm node. A resolve targeting any other plugin
        # means our own state is corrupt — fail loud, do not coerce.
        raise AuditIntegrityError(
            f"resolve_interpretation_event: llm_model_choice review targets node "
            f"{affected_node_id!r} with plugin {plugin!r}; expected 'llm'"
        )
    options = _require_mapping(
        node["options"] if "options" in node else None,
        message=f"resolve_interpretation_event: node {affected_node_id!r} options is not a mapping",
    )
    requirements, matching_index = _matching_pending_requirement_index(
        options[INTERPRETATION_REQUIREMENTS_KEY] if INTERPRETATION_REQUIREMENTS_KEY in options else None,
        kind=InterpretationKind.LLM_MODEL_CHOICE,
        user_term=user_term,
        context="resolve_interpretation_event",
    )
    requirement = dict(requirements[matching_index])
    draft = requirement["draft"] if "draft" in requirement else None
    if type(draft) is not str or draft != llm_draft:
        raise InterpretationPlaceholderConsumedError(
            "resolve_interpretation_event: llm_model_choice event draft does not match the node review requirement draft"
        )
    requirement["status"] = "resolved"
    requirement["event_id"] = event_id
    requirement["accepted_value"] = accepted_value
    # Read-side drift guard (_validate_model_choice_review) recomputes
    # stable_hash(options.model) and compares it to this field, so the
    # resolved requirement must carry the hash of the accepted model. The
    # field is named for the prompt-template case but is reused here as the
    # model-choice review's anchor hash (mirroring _resolve_prompt_template_review).
    requirement["resolved_prompt_template_hash"] = model_choice_artifact_hash(accepted_value)
    requirements[matching_index] = requirement

    final_nodes: list[Mapping[str, Any]] = []
    for current_node in state_record.nodes or ():
        if current_node["id"] == affected_node_id:
            patched_node = dict(current_node)
            patched_options = dict(options)
            patched_options[INTERPRETATION_REQUIREMENTS_KEY] = requirements
            patched_options["model"] = accepted_value
            patched_node["options"] = patched_options
            final_nodes.append(patched_node)
        else:
            final_nodes.append(current_node)
    # Model-choice review patches only node options; the sources map is
    # carried forward unchanged. The legacy singular ``source`` column is dead.
    return state_record.sources, final_nodes, None


def _pending_interpretation_validation_candidate_digest(
    base_state: CompositionStateRecord,
    data: CompositionStateData,
) -> str:
    """Bind a validation result to the exact repository-created candidate."""
    return stable_hash(
        {
            "base_state_id": str(base_state.id),
            "sources": deep_thaw(data.sources),
            "nodes": deep_thaw(data.nodes),
            "edges": deep_thaw(data.edges),
            "outputs": deep_thaw(data.outputs),
            "metadata": deep_thaw(data.metadata_),
            "composer_meta": deep_thaw(data.composer_meta),
        }
    )


def _forbidden_validation_dependency(value: object) -> str | None:
    """Find runtime/authority handles hidden in a validation dependency graph."""
    seen: set[int] = set()

    def _walk(current: object) -> str | None:
        current_id = id(current)
        if current_id in seen:
            return None
        seen.add(current_id)
        current_type = type(current)
        lineage = {(base.__module__, base.__name__) for base in current_type.__mro__}
        for module_name, class_name in lineage:
            if module_name.startswith("sqlalchemy.engine") and class_name in {"Connection", "Engine"}:
                return f"{module_name}.{class_name}"
            if module_name == "elspeth.web.sessions.service" and class_name == "SessionServiceImpl":
                return f"{module_name}.{class_name}"
        if current_type.__module__.startswith("elspeth.web.coordination") and all(
            callable(getattr(current, method_name, None)) for method_name in ("acquire", "mutate", "release")
        ):
            return f"{current_type.__module__}.{current_type.__name__}"
        if current is None or current_type in {bool, bytes, datetime, float, int, str, UUID} or isinstance(current, Enum):
            return None
        if isinstance(current, type):
            return None
        nested_values: list[object] = []
        if isinstance(current, Mapping):
            nested_values.extend(current.keys())
            nested_values.extend(current.values())
        if isinstance(current, (tuple, list, set, frozenset)):
            nested_values.extend(current)
        if ismethod(current):
            nested_values.extend((current.__self__, current.__func__))
        if isfunction(current):
            for cell in current.__closure__ or ():
                with suppress(ValueError):
                    nested_values.append(cell.cell_contents)
            nested_values.extend(current.__defaults__ or ())
            nested_values.extend((current.__kwdefaults__ or {}).values())
        if is_dataclass(current):
            nested_values.extend(getattr(current, field.name) for field in fields(current))
        with suppress(TypeError):
            nested_values.extend(vars(current).values())
        for base in current_type.__mro__:
            for descriptor in vars(base).values():
                if not isinstance(descriptor, MemberDescriptorType):
                    continue
                try:
                    nested_values.append(descriptor.__get__(current, current_type))
                except (AttributeError, TypeError):
                    continue
        for nested in nested_values:
            forbidden = _walk(nested)
            if forbidden is not None:
                return forbidden
        return None

    return _walk(value)


def _validate_patched_composition_state_for_policy(
    state: CompositionState,
    *,
    profile_aware: bool,
    plugin_snapshot: PluginAvailabilitySnapshot | None,
    profile_registry: OperatorProfileRegistry | None,
    catalog: CatalogService | None,
) -> ValidationSummary:
    """Validate a candidate without retaining its originating session service."""
    if not profile_aware:
        return state.validate()
    if plugin_snapshot is None:
        raise AuditIntegrityError("Profile-aware composition validation has no principal snapshot")
    if profile_registry is None or catalog is None:
        raise AuditIntegrityError("Profile-aware composition validation dependencies are unavailable")

    from elspeth.web.plugin_policy.validation import validate_authored_composition_state

    result = validate_authored_composition_state(
        state,
        snapshot=plugin_snapshot,
        profile_registry=profile_registry,
        catalog=catalog,
    )
    return result.validation


@final
class _SessionPendingInterpretationValidator:
    """Exact validation-only capability for process-local, synchronous, handle-free dependencies."""

    __slots__ = ("__catalog", "__plugin_snapshot", "__profile_aware", "__profile_registry")

    def __init__(
        self,
        *,
        profile_aware: bool,
        plugin_snapshot: PluginAvailabilitySnapshot | None,
        profile_registry: OperatorProfileRegistry | None,
        catalog: CatalogService | None,
    ) -> None:
        from elspeth.web.catalog.service import CatalogServiceImpl
        from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
        from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry

        if type(profile_aware) is not bool:
            raise TypeError("profile_aware must be an exact boolean")
        for field_name, dependency, allowed_type in (
            ("plugin_snapshot", plugin_snapshot, PluginAvailabilitySnapshot),
            ("profile_registry", profile_registry, OperatorProfileRegistry),
            ("catalog", catalog, CatalogServiceImpl),
        ):
            if dependency is None or type(dependency) is allowed_type:
                continue
            forbidden = _forbidden_validation_dependency(dependency)
            if forbidden is not None:
                raise TypeError(f"pending interpretation {field_name} retains forbidden runtime handle {forbidden}")
            raise TypeError(f"pending interpretation {field_name} must be the exact process-local {allowed_type.__name__}")
        self.__profile_aware = profile_aware
        self.__plugin_snapshot = plugin_snapshot
        self.__profile_registry = profile_registry
        self.__catalog = catalog

    def __call__(
        self,
        candidate: SessionPendingInterpretationValidationCandidate,
    ) -> SessionPendingInterpretationValidationResult:
        if type(candidate) is not SessionPendingInterpretationValidationCandidate:
            raise TypeError("pending interpretation validation candidate must be exact")
        data = candidate.data
        validation = _validate_patched_composition_state_for_policy(
            state_from_record(
                replace(
                    candidate.base_state,
                    source=None,
                    sources=data.sources,
                    nodes=data.nodes,
                    edges=data.edges,
                    outputs=data.outputs,
                    metadata_=data.metadata_,
                    composer_meta=data.composer_meta,
                    is_valid=False,
                    validation_errors=None,
                )
            ),
            profile_aware=self.__profile_aware,
            plugin_snapshot=self.__plugin_snapshot,
            profile_registry=self.__profile_registry,
            catalog=self.__catalog,
        )
        return SessionPendingInterpretationValidationResult(
            candidate_digest=candidate.digest,
            is_valid=validation.is_valid,
            validation_errors=tuple(error.message for error in validation.errors) or None,
        )


@final
@dataclass(frozen=True, slots=True)
class _PreparedPendingInterpretation:
    command: SessionPendingInterpretationCommand
    validator: _SessionPendingInterpretationValidator

    def __post_init__(self) -> None:
        if type(self.command) is not SessionPendingInterpretationCommand:
            raise TypeError("prepared pending interpretation command must be exact")
        if type(self.validator) is not _SessionPendingInterpretationValidator:
            raise TypeError("prepared pending interpretation validator must be exact")


@final
class _SessionPendingInterpretationPlanner:
    """Canonical reconciliation planner; it can request validation but cannot perform DML."""

    @staticmethod
    def plan(
        command: SessionPendingInterpretationCommand,
        snapshot: SessionPendingInterpretationSnapshot,
        validator: SessionPendingInterpretationValidator,
    ) -> SessionPendingInterpretationDecision:
        event_id = command.event_id
        composition_state_id = command.composition_state_id
        affected_node_id = command.affected_node_id
        tool_call_id = command.tool_call_id
        user_term = command.user_term
        kind = command.kind
        llm_draft = command.llm_draft
        model_identifier = command.model_identifier
        model_version = command.model_version
        provider = command.provider
        composer_skill_hash = command.composer_skill_hash
        now = command.created_at
        sid = str(snapshot.anchor_state.session_id)
        state_record = snapshot.anchor_state
        nodes = state_record.nodes
        sources = state_record.sources
        if kind is InterpretationKind.INVENTED_SOURCE:
            source_name = source_name_from_component_id(affected_node_id)
            if source_name is None:
                raise ValueError(
                    "create_pending_interpretation_event: invented_source must target a source component "
                    f"({SOURCE_COMPONENT_ID!r} or {SOURCE_COMPONENT_ID!r}:<name>), got {affected_node_id!r}"
                )
            source = sources[source_name] if sources is not None and source_name in sources else None
            if type(source) not in (dict, MappingProxyType):
                raise ValueError(f"create_pending_interpretation_event: invented_source requires persisted source {source_name!r}")
            source = cast("Mapping[str, Any]", source)
            source_options = source["options"] if "options" in source else None
            if type(source_options) not in (dict, MappingProxyType):
                raise ValueError(f"create_pending_interpretation_event: invented_source requires source.options.{SOURCE_AUTHORING_KEY}")
            source_options = cast("Mapping[str, Any]", source_options)
            if SOURCE_AUTHORING_KEY not in source_options:
                raise ValueError(f"create_pending_interpretation_event: invented_source requires source.options.{SOURCE_AUTHORING_KEY}")
            source_authoring = source_options[SOURCE_AUTHORING_KEY]
            if type(source_authoring) not in (dict, MappingProxyType):
                raise ValueError(f"create_pending_interpretation_event: source.options.{SOURCE_AUTHORING_KEY} must be a mapping")
            source_authoring = cast("Mapping[str, Any]", source_authoring)
            content_hash = source_authoring["content_hash"] if "content_hash" in source_authoring else None
            if type(content_hash) is not str or not content_hash:
                raise ValueError(
                    f"create_pending_interpretation_event: source.options.{SOURCE_AUTHORING_KEY}.content_hash must be populated"
                )
            try:
                requirements, matching_index = _matching_pending_requirement_index(
                    source_options[INTERPRETATION_REQUIREMENTS_KEY] if INTERPRETATION_REQUIREMENTS_KEY in source_options else None,
                    kind=kind,
                    user_term=user_term,
                    context="create_pending_interpretation_event",
                )
            except InterpretationPlaceholderConsumedError as exc:
                raise ValueError(
                    "create_pending_interpretation_event: source.options.interpretation_requirements "
                    f"must contain exactly one pending {kind.value!r} requirement for {user_term!r}"
                ) from exc
            draft = requirements[matching_index]["draft"] if "draft" in requirements[matching_index] else None
            if type(draft) is not str or draft != llm_draft:
                raise ValueError(
                    "create_pending_interpretation_event: invented_source event draft does not match the source review requirement draft"
                )
        elif kind is InterpretationKind.PIPELINE_DECISION:
            if nodes is None:
                raise ValueError(
                    f"create_pending_interpretation_event: composition state {composition_state_id!s} has no nodes; "
                    f"affected_node_id {affected_node_id!r} is not present"
                )
            node = _find_interpretation_review_node(
                state_record,
                affected_node_id=affected_node_id,
                context="create_pending_interpretation_event",
            )
            options = _require_mapping(
                node["options"] if "options" in node else None,
                message=f"create_pending_interpretation_event: node {affected_node_id!r} options is not a mapping",
            )
            try:
                requirements, matching_index = _matching_pending_requirement_index(
                    options[INTERPRETATION_REQUIREMENTS_KEY] if INTERPRETATION_REQUIREMENTS_KEY in options else None,
                    kind=kind,
                    user_term=user_term,
                    context="create_pending_interpretation_event",
                )
            except InterpretationPlaceholderConsumedError as exc:
                raise ValueError(
                    "create_pending_interpretation_event: node options.interpretation_requirements "
                    f"must contain exactly one pending {kind.value!r} requirement for {user_term!r}"
                ) from exc
            draft = requirements[matching_index]["draft"] if "draft" in requirements[matching_index] else None
            if type(draft) is not str or draft != llm_draft:
                raise ValueError(
                    "create_pending_interpretation_event: pipeline_decision event draft does not match the node review requirement draft"
                )
            _validate_pipeline_decision_semantics_from_state_record(
                state_record,
                affected_node_id=affected_node_id,
                user_term=user_term,
                draft=draft,
                context="create_pending_interpretation_event",
            )
        else:
            if nodes is None:
                raise ValueError(
                    f"create_pending_interpretation_event: composition state {composition_state_id!s} has no nodes; "
                    f"affected_node_id {affected_node_id!r} is not present"
                )
            node = _find_llm_transform_node(
                state_record,
                affected_node_id=affected_node_id,
                context="create_pending_interpretation_event",
            )
            options = _require_mapping(
                node["options"],
                message=f"create_pending_interpretation_event: node {affected_node_id!r} options is not a mapping",
            )
            if kind is InterpretationKind.VAGUE_TERM:
                requirements_value = options.get(INTERPRETATION_REQUIREMENTS_KEY)
                has_structured_match = type(requirements_value) in (list, tuple) and any(
                    type(requirement) in (dict, MappingProxyType)
                    and requirement.get("kind", InterpretationKind.VAGUE_TERM.value) == InterpretationKind.VAGUE_TERM.value
                    and type(requirement.get("user_term")) is str
                    and requirement["user_term"].strip() == user_term.strip()
                    and requirement.get("status") == "pending"
                    for requirement in cast("list[Any] | tuple[Any, ...]", requirements_value)
                )
                if has_structured_match:
                    requirements, matching_index = _matching_pending_requirement_index(
                        requirements_value,
                        kind=kind,
                        user_term=user_term,
                        context="create_pending_interpretation_event",
                    )
                    current_draft = requirements[matching_index].get("draft")
                    if type(current_draft) is not str or current_draft != llm_draft:
                        raise ValueError(
                            "create_pending_interpretation_event: vague_term event draft does not match "
                            "the current review requirement draft"
                        )
            elif kind is InterpretationKind.LLM_PROMPT_TEMPLATE:
                prompt_template = options.get("prompt_template")
                if type(prompt_template) is not str:
                    raise ValueError(
                        f"create_pending_interpretation_event: node {affected_node_id!r} options.prompt_template is not a string"
                    )
                if llm_draft != prompt_template:
                    raise ValueError(
                        "create_pending_interpretation_event: llm_prompt_template event draft must match current options.prompt_template"
                    )
                try:
                    _matching_pending_requirement_index(
                        options.get(INTERPRETATION_REQUIREMENTS_KEY),
                        kind=kind,
                        user_term=user_term,
                        context="create_pending_interpretation_event",
                    )
                except InterpretationPlaceholderConsumedError as exc:
                    raise ValueError(
                        "create_pending_interpretation_event: node options.interpretation_requirements "
                        f"must contain exactly one pending {kind.value!r} requirement for {user_term!r}"
                    ) from exc

        surfacing_identity = _reviewed_content_identity(
            state_record,
            kind=kind,
            affected_node_id=affected_node_id,
            user_term=user_term,
            context="create_pending_interpretation_event",
        )
        live_state = snapshot.live_state
        try:
            current_identity = _reviewed_content_identity(
                live_state,
                kind=kind,
                affected_node_id=affected_node_id,
                user_term=user_term,
                context="create_pending_interpretation_event",
            )
        except InterpretationResolveError:
            stale = tuple(
                site.event.id
                for site in snapshot.pending_sites
                if type(site.event.user_term) is str and site.event.user_term.strip() == user_term.strip()
            )
            if not stale:
                raise
            return SessionPendingInterpretationDecision(
                result_event_id=stale[0],
                abandoned_event_ids=stale,
            )

        matching_event_id: UUID | None = None
        rows_to_abandon: list[UUID] = []
        for site in snapshot.pending_sites:
            pending_user_term = site.event.user_term
            if type(pending_user_term) is not str:
                raise AuditIntegrityError("create_pending_interpretation_event: pending review has malformed user_term")
            if pending_user_term.strip() != user_term.strip():
                continue
            if site.surfacing_state is None:
                raise AuditIntegrityError("create_pending_interpretation_event: pending review has no same-session surfacing state")
            pending_identity = _reviewed_content_identity(
                site.surfacing_state,
                kind=kind,
                affected_node_id=affected_node_id,
                user_term=pending_user_term,
                context="create_pending_interpretation_event",
            )
            if not snapshot.review_disabled and matching_event_id is None and pending_identity == current_identity:
                matching_event_id = site.event.id
            else:
                rows_to_abandon.append(site.event.id)
        if matching_event_id is None and surfacing_identity != current_identity:
            raise InterpretationPlaceholderConsumedError(
                "create_pending_interpretation_event: reviewed content no longer matches the current composition state"
            )
        if matching_event_id is not None:
            return SessionPendingInterpretationDecision(
                result_event_id=matching_event_id,
                abandoned_event_ids=tuple(rows_to_abandon),
            )

        if not snapshot.review_disabled:
            return SessionPendingInterpretationDecision(
                result_event_id=event_id,
                abandoned_event_ids=tuple(rows_to_abandon),
                insert_event=True,
                choice=InterpretationChoice.PENDING,
                interpretation_source=InterpretationSource.USER_APPROVED,
            )

        domain_dict = _interpretation_hash_domain_v2(
            session_id=sid,
            composition_state_id=str(composition_state_id),
            affected_node_id=affected_node_id,
            tool_call_id=tool_call_id,
            user_term=user_term,
            kind=kind.value,
            llm_draft=llm_draft,
            accepted_value=llm_draft,
            actor="composer-llm",
            model_identifier=model_identifier,
            model_version=model_version,
            provider=provider,
            composer_skill_hash=composer_skill_hash,
            context="create_pending_interpretation_event",
        )
        resolved_hash: str | None
        if kind is InterpretationKind.VAGUE_TERM:
            final_sources, final_nodes, resolved_hash = _resolve_vague_term(
                live_state,
                surfacing_state_record=live_state,
                event_id=str(event_id),
                affected_node_id=affected_node_id,
                user_term=user_term,
                llm_draft=llm_draft,
                accepted_value=llm_draft,
            )
        elif kind is InterpretationKind.LLM_PROMPT_TEMPLATE:
            final_sources, final_nodes, resolved_hash = _resolve_prompt_template_review(
                live_state,
                event_id=str(event_id),
                affected_node_id=affected_node_id,
                user_term=user_term,
                accepted_value=llm_draft,
                surfacing_structure_hash=_surfacing_prompt_structure_hash(
                    live_state,
                    affected_node_id=affected_node_id,
                ),
            )
        elif kind is InterpretationKind.INVENTED_SOURCE:
            final_sources, final_nodes, resolved_hash = _resolve_invented_source(
                live_state,
                event_id=str(event_id),
                affected_node_id=affected_node_id,
                user_term=user_term,
                llm_draft=llm_draft,
                accepted_value=llm_draft,
            )
        elif kind is InterpretationKind.PIPELINE_DECISION:
            final_sources, final_nodes, resolved_hash = _resolve_pipeline_decision_review(
                live_state,
                event_id=str(event_id),
                affected_node_id=affected_node_id,
                user_term=user_term,
                llm_draft=llm_draft,
                accepted_value=llm_draft,
            )
        elif kind is InterpretationKind.LLM_MODEL_CHOICE:
            final_sources, final_nodes, resolved_hash = _resolve_model_choice_review(
                live_state,
                event_id=str(event_id),
                affected_node_id=affected_node_id,
                user_term=user_term,
                llm_draft=llm_draft,
                accepted_value=llm_draft,
            )
        else:  # pragma: no cover - closed enum
            raise AssertionError(f"unhandled InterpretationKind {kind!r}")
        patched_state = CompositionStateData(
            sources=final_sources,
            nodes=final_nodes,
            edges=live_state.edges,
            outputs=live_state.outputs,
            metadata_=live_state.metadata_,
            is_valid=False,
            validation_errors=None,
            composer_meta=live_state.composer_meta,
        )
        candidate = SessionPendingInterpretationValidationCandidate(
            digest=_pending_interpretation_validation_candidate_digest(live_state, patched_state),
            base_state=live_state,
            data=patched_state,
        )
        validation = validator(candidate)
        if type(validation) is not SessionPendingInterpretationValidationResult or validation.candidate_digest != candidate.digest:
            raise AuditIntegrityError("pending interpretation validation result is not bound to its candidate")
        patched_state = replace(
            patched_state,
            is_valid=validation.is_valid,
            validation_errors=validation_errors_for_composer_surface(
                composer_meta=live_state.composer_meta,
                is_valid=validation.is_valid,
                validation_errors=list(validation.validation_errors) if validation.validation_errors is not None else None,
            ),
        )
        return SessionPendingInterpretationDecision(
            result_event_id=event_id,
            abandoned_event_ids=tuple(rows_to_abandon),
            insert_event=True,
            choice=InterpretationChoice.OPTED_OUT,
            accepted_value=llm_draft,
            resolved_at=now,
            arguments_hash=stable_hash(domain_dict),
            hash_domain_version="v2",
            interpretation_source=InterpretationSource.AUTO_INTERPRETED_OPT_OUT,
            resolved_prompt_template_hash=(resolved_hash if kind is InterpretationKind.LLM_PROMPT_TEMPLATE else None),
            ensure_opt_out_marker=True,
            appended_state=SessionCompositionStateCreation(
                id=uuid.uuid4(),
                data=patched_state,
                provenance="interpretation_resolve",
                created_at=now,
                derived_from_state_id=live_state.id,
            ),
        )
