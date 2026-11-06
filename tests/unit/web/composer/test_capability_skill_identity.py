"""Capability-core identity, coverage, and planner-manifest contracts."""

from __future__ import annotations

import re

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer._producer_resolver import _IMPLICIT_SELF_PUBLISHING_NODE_TYPES
from elspeth.web.composer.capability_skill import (
    CANONICAL_CAPABILITY_FIELDS,
    CAPABILITY_CORE_NODE_GUIDANCE,
    canonical_capability_fields,
    documented_capability_fields,
    load_pipeline_capability_core,
    validate_capability_field_contract,
)
from elspeth.web.composer.prompts import build_system_prompt
from elspeth.web.composer.skills import load_skill
from elspeth.web.composer.state import COMPOSER_NODE_TYPES
from elspeth.web.composer.tools.schema_contract import canonical_set_pipeline_schema


def test_freeform_planner_prepends_capability_core_once() -> None:
    core = load_pipeline_capability_core()
    rendered = build_system_prompt(None)
    assert rendered.startswith(core)
    assert rendered.count(core) == 1


def test_capability_core_names_no_planner_exclusive_terminal_tool() -> None:
    # Both the capability core and the rendered live prompt advertise the
    # complete set_pipeline document and exclude the retired terminal.
    core = load_pipeline_capability_core()

    assert "emit_pipeline_proposal" not in core
    assert "emit_pipeline_proposal" not in build_system_prompt(None)
    assert "`set_pipeline`" in core


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


def test_capability_contract_rejects_an_undocumented_schema_field() -> None:
    schema = canonical_set_pipeline_schema()
    schema["properties"]["future_topology"] = {"type": "object"}
    with pytest.raises(AuditIntegrityError, match="documented capability fields drifted"):
        validate_capability_field_contract(schema, load_pipeline_capability_core())


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
