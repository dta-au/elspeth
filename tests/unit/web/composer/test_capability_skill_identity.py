"""Capability-core identity, coverage, and planner-manifest contracts."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.canonical import stable_hash
from elspeth.web.composer._producer_resolver import _IMPLICIT_SELF_PUBLISHING_NODE_TYPES
from elspeth.web.composer.capability_skill import (
    CANONICAL_CAPABILITY_FIELDS,
    CAPABILITY_CORE_NODE_GUIDANCE,
    PlannerCapabilityManifest,
    build_planner_capability_manifest,
    canonical_capability_fields,
    documented_capability_fields,
    load_pipeline_capability_core,
    validate_capability_field_contract,
)
from elspeth.web.composer.pipeline_planner import PLANNER_DISCOVERY_TOOL_NAMES, planner_tool_definitions
from elspeth.web.composer.prompts import build_system_prompt
from elspeth.web.composer.skills import load_skill
from elspeth.web.composer.state import COMPOSER_NODE_TYPES
from elspeth.web.composer.tools.schema_contract import canonical_set_pipeline_schema


def _messages(rendered_skill: str, *, sensitive_user_text: str = "build it") -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": rendered_skill},
        {"role": "user", "content": sensitive_user_text},
    ]


def _manifest(
    rendered_skill: str,
    *,
    sensitive_user_text: str = "build it",
) -> PlannerCapabilityManifest:
    return build_planner_capability_manifest(
        messages=_messages(rendered_skill, sensitive_user_text=sensitive_user_text),
        tools=planner_tool_definitions(dialect=ToolContractDialect.NONE),
        canonical_schema=canonical_set_pipeline_schema(),
    )


def test_freeform_planner_prepends_capability_core_once() -> None:
    core = load_pipeline_capability_core()
    rendered = build_system_prompt(None)
    assert rendered.startswith(core)
    assert rendered.count(core) == 1


def test_capability_core_names_no_planner_exclusive_terminal_tool() -> None:
    # The core is shared by the freeform tool loop and proposal planner.
    # The latter's terminal contract is delivered per request, while both
    # author the canonical `set_pipeline` document shape.
    core = load_pipeline_capability_core()

    assert "emit_pipeline_proposal" not in core
    assert "emit_pipeline_proposal" not in build_system_prompt(None)
    assert "`set_pipeline`" in core


def test_planner_request_instruction_carries_the_terminal_contract() -> None:
    # Split-the-core counterpart: with the shared bytes surface-neutral, the
    # planner's terminal-alone and bounded-repair contract must ride the per-request
    # instruction channel every plan_pipeline call sends.
    from elspeth.web.composer.pipeline_planner import PLANNER_TERMINAL_INSTRUCTION

    assert "emit_pipeline_proposal" in PLANNER_TERMINAL_INSTRUCTION
    assert "alone in its turn" in PLANNER_TERMINAL_INSTRUCTION
    assert "After a rejection, repair and resubmit the complete proposal" in PLANNER_TERMINAL_INSTRUCTION
    assert "handoff_to_user or restore_parked_graph alone" in PLANNER_TERMINAL_INSTRUCTION
    assert "set_pipeline" in PLANNER_TERMINAL_INSTRUCTION


def test_planner_request_instruction_orients_the_planner_on_its_own_surface() -> None:
    # The shared skill is written for the incremental tool loop and cannot name
    # the planner's terminal tool, so the per-request instruction is the only
    # place the planner learns which described tools it lacks. Each claim is
    # checked against the live palettes so the sentence cannot drift from them.
    from elspeth.web.composer.pipeline_planner import PLANNER_TERMINAL_INSTRUCTION
    from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions

    planner_palette = {tool["function"]["name"] for tool in planner_tool_definitions(dialect=ToolContractDialect.NONE)}
    loop_palette = {tool["function"]["name"] for tool in composer_loop_tool_definitions(ToolContractDialect.NONE)}

    assert "This is the proposal-only surface" in PLANNER_TERMINAL_INSTRUCTION
    assert "apply them to emit_pipeline_proposal" in PLANNER_TERMINAL_INSTRUCTION
    named_as_absent = (
        "set_source",
        "upsert_node",
        "patch_node_options",
        "create_blob",
        "request_interpretation_review",
        "request_advisor_hint",
    )
    for tool_name in named_as_absent:
        assert tool_name in PLANNER_TERMINAL_INSTRUCTION
        assert tool_name not in planner_palette
        assert tool_name in loop_palette
    assert {"emit_pipeline_proposal", "handoff_to_user", "restore_parked_graph"} <= planner_palette

    assert "Author vague_term and registered pipeline_decision rows inside the proposal's node options" in PLANNER_TERMINAL_INSTRUCTION
    assert (
        "never author invented_source, llm_prompt_template, llm_model_choice or source_data_contract rows" in PLANNER_TERMINAL_INSTRUCTION
    )
    # The rejection shape named here is the planner's own feedback wire, not
    # the incremental loop's envelope the skill teaches.
    from elspeth.web.composer.pipeline_planner import _PlannerFeedbackError, _PlannerFeedbackWire

    assert {"success", "validation"} <= set(_PlannerFeedbackWire.__annotations__)
    for key in ("component", "error_code", "explanation", "suggested_fix", "detail", "plugin_contracts"):
        assert key in PLANNER_TERMINAL_INSTRUCTION
        assert key in _PlannerFeedbackError.__annotations__


def test_interaction_skill_scopes_review_calls_to_surfaces_that_offer_the_tool() -> None:
    interaction = " ".join(load_skill("pipeline_composer").split())

    assert interaction.startswith("# Pipeline Composer Interaction Policy")
    assert "A rule that names a tool your palette does not offer does not apply to you as written." in interaction
    assert "there is no call to make and nothing to look for" in interaction
    assert "then surface it where your palette offers the review tool" in interaction
    assert "Surface every staged assumption with `request_interpretation_review` (where your palette offers it)" in interaction
    assert 'every "YOU" in the matrix\'s "Review surfaced by" column, applies where your palette offers that tool' in interaction
    assert "On a proposal-only surface, states 1 and 2 are replaced by one" in interaction


def test_capability_core_explains_digest_budget_omissions() -> None:
    core = load_pipeline_capability_core()

    assert "omitted_public_text_count" in core
    assert "details_via" in core


def test_capability_core_treats_successful_mutation_echo_as_state_authority() -> None:
    """A mutation echo replaces confirmation reads; it is not optional telemetry."""
    discovery = load_pipeline_capability_core().split("[capability:discovery-order]", 1)[1].split("## Complete topology", 1)[0]
    prose = " ".join(discovery.split())

    assert "`applied_component`" in discovery
    assert "authoritative post-change state" in prose
    assert "do not call `get_pipeline_state` to confirm" in prose
    assert "a mutation and the read that confirms it may share one turn" not in prose


def test_capability_facts_have_one_document_owner() -> None:
    core = load_skill("pipeline_capabilities")
    interaction = load_skill("pipeline_composer")
    anchors = (
        "[capability:discovery-order]",
        "[capability:topology]",
        "[capability:canonical-fields]",
        "[capability:field-contracts]",
        "[capability:structured-output-repair]",
        "[capability:plugin-assistance]",
    )

    for anchor in anchors:
        assert core.count(anchor) == 1
        assert anchor not in interaction

    assert "## Discovery And Credentials" not in interaction
    assert "### Multi-source Pipelines" not in interaction
    assert "### Field Wiring" not in interaction
    assert "### LLM Nodes" not in interaction
    assert "For `batch_stats`" not in interaction
    assert "For `batch_stats`" not in core
    assert "get_plugin_assistance" in core


def test_capability_core_names_every_implicit_self_publisher_from_runtime_authority() -> None:
    core = load_pipeline_capability_core()
    normalized_core = " ".join(core.split())
    prefix = "The only implicit self-publishing node kinds are "

    assert normalized_core.count(prefix) == 1, (
        "The topology guidance must state the bounded node-id exception once; "
        "a blanket claim that node ids are never connections contradicts the runtime."
    )
    documented_clause = normalized_core.split(prefix, 1)[1].split(":", 1)[0]
    documented_kinds = frozenset(re.findall(r"`([a-z_]+)`", documented_clause))

    assert documented_kinds == _IMPLICIT_SELF_PUBLISHING_NODE_TYPES
    assert "`row_union` requires an explicit `on_success` connection" in normalized_core


def test_static_planner_guidance_contains_no_deployment_plugin_facts() -> None:
    rendered_prompts = [build_system_prompt(None)]
    forbidden_facts = (
        "web_scrape",
        "field_mapper",
        "azure_prompt_shield",
        '"provider": "openrouter"',
        "OPENROUTER_API_KEY",
        "collision_policy",
        "auto_increment",
        "url_field",
        "response_field",
        "llm_response",
        "CSV source means CSV sinks",
        "Author free-text generated sources as JSON",
        "Headered mode (no `columns`)",
        "`text` source treats every non-blank line",
        "Azure-blob",
        "Dataverse",
        "dataverse",
        "null-source placeholder",
        "csv_source_blob_header_mismatch",
    )

    for rendered in rendered_prompts:
        assert all(fact not in rendered for fact in forbidden_facts)

    core = load_pipeline_capability_core()
    assert "get_plugin_assistance" in core
    assert "policy-visible" in core
    assert "prompt-injection" in core
    assert "cleanup" in core
    assert "An option key such as" not in core
    assert "Single-query LLM output remains one response column" not in core


def test_interpretation_requirement_guidance_uses_exact_public_shell() -> None:
    interaction = load_skill("pipeline_composer")

    assert "You author ONLY `kind`, `user_term`, `draft`, and optional `display_title`." in interaction
    assert "plus `id`" not in interaction
    assert "`status` defaults to `pending`" not in interaction


def test_invented_source_authorship_has_one_answer_across_skill_aids_and_schema_note() -> None:
    # The server stages the invented_source row when it binds generated source
    # rows (tools/sources.py::_options_with_source_blob_review, with
    # replace_kind=INVENTED_SOURCE). The skill, the authoring aids and the tool
    # schema note once gave three different answers on who authors that row.
    from elspeth.web.composer import planner_authoring_aids as aids
    from elspeth.web.composer.tools import _common

    interaction = " ".join(load_skill("pipeline_composer").split())
    assert "never author that row yourself on any surface" in interaction
    assert "NEVER author this row on any surface" in interaction

    rules = " ".join((*aids._SOURCE_CUSTODY_RULES, *aids._FORK_COALESCE_RULES, *aids._REVIEW_REGISTRY_RULES))
    owned_kind_sentences = re.findall(r"planner-owned kinds[^.]*\.", rules)
    assert len(owned_kind_sentences) == 2
    assert all("invented_source" not in sentence for sentence in owned_kind_sentences)
    assert rules.count("Never author invented_source: the server stages") == 2
    assert "stage the invented_source interpretation review" not in rules

    for note in (_common._INTERPRETATION_REVIEW_FOLLOWUP, _common._INTERPRETATION_REQUIREMENTS_OWNERSHIP_SCHEMA_NOTE):
        assert "never author invented_source" in note
        assert "the rows you author ride in the proposal" in note
        assert "pending requirements" not in note


def test_capability_coverage_is_exactly_derived_from_canonical_authorities() -> None:
    actual_fields = canonical_capability_fields(canonical_set_pipeline_schema())

    assert actual_fields == CANONICAL_CAPABILITY_FIELDS
    assert documented_capability_fields(load_pipeline_capability_core()) == actual_fields
    assert set(CAPABILITY_CORE_NODE_GUIDANCE) == set(COMPOSER_NODE_TYPES)
    core = load_pipeline_capability_core()
    assert all(core.count(anchor) == 1 for anchor in CAPABILITY_CORE_NODE_GUIDANCE.values())
    assert "timeout_seconds" in actual_fields["node"]


def test_gate_capability_documents_node_level_error_policy_and_fail_fast_omission() -> None:
    core = load_pipeline_capability_core()
    gate_section = core.split("[capability-node:gate]", 1)[1].split("[capability-node:aggregation]", 1)[0]

    assert "node-level" in gate_section
    assert "on_error" in gate_section
    assert "discard" in gate_section
    assert "sink" in gate_section
    assert "fail-fast" in gate_section


def test_capability_field_extraction_detects_new_structural_field() -> None:
    schema = canonical_set_pipeline_schema()
    schema["properties"]["future_topology"] = {"type": "object"}

    assert canonical_capability_fields(schema) != CANONICAL_CAPABILITY_FIELDS


def test_capability_field_extraction_rejects_missing_required_schema_node() -> None:
    schema = canonical_set_pipeline_schema()
    del schema["properties"]

    with pytest.raises(KeyError, match="properties"):
        canonical_capability_fields(schema)


def test_manifest_rejects_schema_and_terminal_updated_without_documented_field() -> None:
    schema = canonical_set_pipeline_schema()
    schema["properties"]["future_topology"] = {"type": "object"}
    tools = planner_tool_definitions(dialect=ToolContractDialect.NONE)
    tools[-1]["function"]["parameters"]["properties"]["pipeline"] = schema

    with pytest.raises(AuditIntegrityError, match="documented capability fields drifted"):
        build_planner_capability_manifest(
            messages=_messages(build_system_prompt(None)),
            tools=tools,
            canonical_schema=schema,
        )


@pytest.mark.parametrize("mutation", ("missing_start", "missing_family", "malformed_fields"))
def test_documented_field_inventory_fails_closed_on_drift(mutation: str) -> None:
    core = load_pipeline_capability_core()
    if mutation == "missing_start":
        changed = core.replace("<!-- canonical-field-inventory:start -->", "", 1)
    elif mutation == "missing_family":
        changed = core.replace("| trigger | `count`, `timeout_seconds`, `condition` |\n", "", 1)
    else:
        changed = core.replace("| metadata | `name`, `description` |", "| metadata | name, description |", 1)

    with pytest.raises(AuditIntegrityError):
        if mutation == "missing_family":
            validate_capability_field_contract(canonical_set_pipeline_schema(), changed)
        else:
            documented_capability_fields(changed)


def test_manifest_uses_exact_ordered_advertised_tool_definitions() -> None:
    tools = planner_tool_definitions(dialect=ToolContractDialect.NONE)
    manifest = _manifest(build_system_prompt(None))

    assert [tool["function"]["name"] for tool in tools] == [
        *PLANNER_DISCOVERY_TOOL_NAMES,
        "handoff_to_user",
        "restore_parked_graph",
        "emit_pipeline_proposal",
    ]
    assert manifest.effective_tool_hash == stable_hash(tools)
    assert manifest.canonical_schema_hash == stable_hash(canonical_set_pipeline_schema())


def test_manifest_accepts_an_order_preserving_dynamic_discovery_subset() -> None:
    tools = planner_tool_definitions(dialect=ToolContractDialect.NONE)
    retained = {"get_plugin_assistance", "get_plugin_schema", "list_models", "handoff_to_user", "restore_parked_graph"}
    subset = [tool for tool in tools if tool["function"]["name"] in retained or tool is tools[-1]]

    manifest = build_planner_capability_manifest(
        messages=_messages(build_system_prompt(None)),
        tools=subset,
        canonical_schema=canonical_set_pipeline_schema(),
    )

    assert [tool["function"]["name"] for tool in subset] == [
        "get_plugin_assistance",
        "get_plugin_schema",
        "list_models",
        "handoff_to_user",
        "restore_parked_graph",
        "emit_pipeline_proposal",
    ]
    assert manifest.effective_tool_hash == stable_hash(subset)


@pytest.mark.parametrize("mutation", ("reordered", "unknown"))
def test_manifest_rejects_invalid_dynamic_discovery_subset(mutation: str) -> None:
    tools = planner_tool_definitions(dialect=ToolContractDialect.NONE)
    retained = {"get_plugin_assistance", "get_plugin_schema", "handoff_to_user", "restore_parked_graph", "emit_pipeline_proposal"}
    subset = [tool for tool in tools if tool["function"]["name"] in retained]
    build_planner_capability_manifest(
        messages=_messages(build_system_prompt(None)), tools=subset, canonical_schema=canonical_set_pipeline_schema()
    )
    if mutation == "reordered":
        subset[0], subset[1] = subset[1], subset[0]
    else:
        subset[0]["function"]["name"] = "unknown_discovery"

    with pytest.raises(AuditIntegrityError, match="identities or order"):
        build_planner_capability_manifest(
            messages=_messages(build_system_prompt(None)),
            tools=subset,
            canonical_schema=canonical_set_pipeline_schema(),
        )


def test_freeform_planner_builds_capability_manifest_core() -> None:
    manifest = _manifest(build_system_prompt(None))

    assert manifest.capability_core_hash == hashlib.sha256(load_pipeline_capability_core().encode("utf-8")).hexdigest()
    assert manifest.canonical_schema_hash == stable_hash(canonical_set_pipeline_schema())
    assert manifest.effective_tool_hash == stable_hash(planner_tool_definitions(dialect=ToolContractDialect.NONE))


@pytest.mark.parametrize("mutation", ("missing_core", "duplicate_core", "reordered_tools", "mutated_schema"))
def test_manifest_fails_closed_on_capability_identity_drift(mutation: str) -> None:
    core = load_pipeline_capability_core()
    messages = _messages(build_system_prompt(None))
    tools = planner_tool_definitions(dialect=ToolContractDialect.NONE)
    if mutation == "missing_core":
        messages[0]["content"] = "interaction only"
    elif mutation == "duplicate_core":
        messages[0]["content"] = f"{core}{core}"
    elif mutation == "reordered_tools":
        tools[0], tools[1] = tools[1], tools[0]
    else:
        tools[-1]["function"]["parameters"]["properties"]["pipeline"]["properties"].pop("edges")

    with pytest.raises(AuditIntegrityError):
        build_planner_capability_manifest(
            messages=messages,
            tools=tools,
            canonical_schema=canonical_set_pipeline_schema(),
        )


def test_manifest_rejects_message_missing_required_role() -> None:
    messages = _messages(build_system_prompt(None))
    del messages[0]["role"]

    with pytest.raises(KeyError, match="role"):
        build_planner_capability_manifest(
            messages=messages,
            tools=planner_tool_definitions(dialect=ToolContractDialect.NONE),
            canonical_schema=canonical_set_pipeline_schema(),
        )


def test_manifest_rejects_terminal_missing_required_parameters() -> None:
    tools = planner_tool_definitions(dialect=ToolContractDialect.NONE)
    del tools[-1]["function"]["parameters"]

    with pytest.raises(KeyError, match="parameters"):
        build_planner_capability_manifest(
            messages=_messages(build_system_prompt(None)),
            tools=tools,
            canonical_schema=canonical_set_pipeline_schema(),
        )


def test_manifest_is_hash_only_and_never_copies_private_prompt_values(tmp_path: Path) -> None:
    private_value = "sk-private-provider-value-never-public"
    deployment = tmp_path / "skills"
    deployment.mkdir()
    (deployment / "pipeline_composer.md").write_text(f"Private deployment instruction: {private_value}\n")

    manifest = _manifest(
        build_system_prompt(str(tmp_path)),
        sensitive_user_text=private_value,
    )
    rendered = repr(asdict(manifest))

    assert private_value not in rendered
    assert set(asdict(manifest)) == {
        "planner_implementation_id",
        "capability_core_hash",
        "canonical_schema_hash",
        "effective_tool_hash",
        "rendered_prompt_hash",
    }


def test_manifest_hashes_are_the_separate_stable_hashes_of_what_it_validated() -> None:
    """Byte identity with the pre-fix-14 manifest, which hashed each input separately."""
    rendered = build_system_prompt(None)
    messages = _messages(rendered)
    tools = planner_tool_definitions(dialect=ToolContractDialect.NONE)

    manifest = build_planner_capability_manifest(messages=messages, tools=tools, canonical_schema=canonical_set_pipeline_schema())

    assert manifest.rendered_prompt_hash == stable_hash(messages)
    assert manifest.effective_tool_hash == stable_hash(tools)
    assert manifest.canonical_schema_hash == stable_hash(tools[-1]["function"]["parameters"]["properties"]["pipeline"])
