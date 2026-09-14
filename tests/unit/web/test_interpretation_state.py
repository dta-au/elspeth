"""Tests for structured composer interpretation-review authoring state."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from elspeth.contracts.composer_interpretation import InterpretationKind
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata, SourceSpec
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    PROMPT_SHIELD_USER_TERM,
    PROMPT_SHIELD_WARNING_DRAFT,
    PROMPT_TEMPLATE_PARTS_KEY,
    RAW_HTML_CLEANUP_USER_TERM,
    SOURCE_AUTHORING_KEY,
    InterpretationReviewPending,
    interpretation_sites,
    materialize_state_for_authoring,
    materialize_state_for_execution,
    pipeline_decision_artifact_hash,
    prompt_shield_recommendation_warning_pairs,
    prompt_structure_hash_from_options,
    raw_html_cleanup_review_contract_error,
    strip_authoring_options,
    vague_term_wiring_count,
)


def _state_with_llm(options: dict[str, object]) -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(
            NodeSpec(
                id="rate_coolness",
                node_type="transform",
                plugin="llm",
                input="source",
                on_success="output",
                on_error="stop",
                options=options,
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _state_with_cleanup_node(options: dict[str, object]) -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(
            NodeSpec(
                id="drop_raw_html",
                node_type="transform",
                plugin="field_mapper",
                input="scored_rows",
                on_success="clean_rows",
                on_error="stop",
                options=options,
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _state_with_web_scrape_identity_node(
    *,
    abuse_contact: str = "abuse-contact-unset@elspeth.example.gov.au",
    scraping_reason: str = "User-requested public web fetch for rules download",
    allowed_hosts: object | None = "public_only",
) -> CompositionState:
    http: dict[str, object] = {
        "abuse_contact": abuse_contact,
        "scraping_reason": scraping_reason,
    }
    if allowed_hosts is not None:
        http["allowed_hosts"] = allowed_hosts
    return CompositionState(
        source=None,
        nodes=(
            NodeSpec(
                id="fetch_pages",
                node_type="transform",
                plugin="web_scrape",
                input="rows",
                on_success="scraped_rows",
                on_error="discard",
                options={
                    "schema": {"mode": "observed"},
                    "url_field": "url",
                    "content_field": "content",
                    "fingerprint_field": "content_fingerprint",
                    "format": "markdown",
                    "http": http,
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _state_with_web_scrape_cleanup_node(options: dict[str, object]) -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(
            NodeSpec(
                id="fetch_pages",
                node_type="transform",
                plugin="web_scrape",
                input="rows",
                on_success="scraped_rows",
                on_error="stop",
                options={
                    "url_field": "url",
                    "content_field": "content",
                    "fingerprint_field": "content_fingerprint",
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
            NodeSpec(
                id="drop_raw_html",
                node_type="transform",
                plugin="field_mapper",
                input="coloured_rows",
                on_success="clean_rows",
                on_error="stop",
                options=options,
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _state_with_web_scrape_gate_to_llm() -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(
            NodeSpec(
                id="fetch_pages",
                node_type="transform",
                plugin="web_scrape",
                input="rows",
                on_success="scraped_rows",
                on_error="stop",
                options={"url_field": "url", "content_field": "content"},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
            NodeSpec(
                id="interesting_pages",
                node_type="gate",
                plugin=None,
                input="scraped_rows",
                on_success=None,
                on_error=None,
                options={},
                condition="row['interesting'] == true",
                routes={"true": "llm_input", "false": "discard"},
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
            NodeSpec(
                id="summarise_pages",
                node_type="transform",
                plugin="llm",
                input="llm_input",
                on_success="summaries",
                on_error="stop",
                options={"prompt_template": "Summarise {{ row.content }}."},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _state_with_web_scrape_gate_shield_to_llm() -> CompositionState:
    state = _state_with_web_scrape_gate_to_llm()
    shield = NodeSpec(
        id="shield_pages",
        node_type="transform",
        plugin="azure_prompt_shield",
        input="llm_input",
        on_success="shielded_rows",
        on_error="stop",
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )
    llm = replace(state.nodes[2], input="shielded_rows")
    return replace(state, nodes=(state.nodes[0], state.nodes[1], shield, llm))


def _pipeline_decision_options(*, status: str = "pending", artifact_hash: str | None = None) -> dict[str, object]:
    options: dict[str, object] = {
        "mapping": {
            "url": "url",
            "agency": "agency",
            "primary_colours": "primary_colours",
        },
        "select_only": True,
    }
    options[INTERPRETATION_REQUIREMENTS_KEY] = [
        {
            "id": "drop_raw_html_review",
            "kind": "pipeline_decision",
            "user_term": "drop_raw_html_fields",
            "status": status,
            "draft": "Drop the scraped raw HTML and fingerprint fields before saving the JSON output.",
            "event_id": "event-raw-html-drop" if status == "resolved" else None,
            "accepted_value": (
                "Drop the scraped raw HTML and fingerprint fields before saving the JSON output." if status == "resolved" else None
            ),
            "accepted_artifact_hash": artifact_hash,
            "resolved_prompt_template_hash": None,
        }
    ]
    return options


def _pending_options() -> dict[str, object]:
    return {
        "prompt_template": "Rate pending interpretation: {{ row.text }}",
        PROMPT_TEMPLATE_PARTS_KEY: [
            {"kind": "text", "text": "Rate "},
            {"kind": "interpretation_ref", "requirement_id": "coolness"},
            {"kind": "text", "text": ": {{ row.text }}"},
        ],
        INTERPRETATION_REQUIREMENTS_KEY: [
            {
                "id": "coolness",
                "kind": "vague_term",
                "user_term": "coolness",
                "status": "pending",
                "draft": "well-designed and useful",
                "event_id": "event-1",
                "accepted_value": None,
                "accepted_artifact_hash": None,
                "resolved_prompt_template_hash": None,
            }
        ],
    }


def test_legacy_placeholder_materializes_for_authoring_without_mutating_source() -> None:
    state = _state_with_llm({"prompt_template": "Rate {{interpretation:coolness}}: {{ row.text }}"})

    authoring = materialize_state_for_authoring(state)

    assert authoring.nodes[0].options["prompt_template"] == "Rate pending interpretation: {{ row.text }}"
    assert state.nodes[0].options["prompt_template"] == "Rate {{interpretation:coolness}}: {{ row.text }}"


def test_pending_structured_requirement_blocks_execution_with_typed_site() -> None:
    state = _state_with_llm(_pending_options())

    result = materialize_state_for_execution(state)

    assert isinstance(result, InterpretationReviewPending)
    site = result.sites[0]
    assert site.component_id == "rate_coolness"
    assert site.component_type == "transform"
    assert site.user_term == "coolness"
    assert site.kind is InterpretationKind.VAGUE_TERM


def test_interpretation_sites_reports_legacy_and_structured_pending_sites() -> None:
    legacy = _state_with_llm({"prompt_template": "Rate {{interpretation:coolness}}: {{ row.text }}"})
    structured = _state_with_llm(_pending_options())

    legacy_sites = interpretation_sites(legacy)
    structured_sites = interpretation_sites(structured)

    assert len(legacy_sites) == 2
    assert legacy_sites[0].component_id == "rate_coolness"
    assert legacy_sites[0].component_type == "transform"
    assert legacy_sites[0].user_term == "coolness"
    assert legacy_sites[0].kind is InterpretationKind.VAGUE_TERM
    assert legacy_sites[1].component_id == "rate_coolness"
    assert legacy_sites[1].component_type == "transform"
    assert legacy_sites[1].user_term == "llm_prompt_template:rate_coolness"
    assert legacy_sites[1].kind is InterpretationKind.LLM_PROMPT_TEMPLATE

    assert structured_sites[0].component_id == "rate_coolness"
    assert structured_sites[0].component_type == "transform"
    assert structured_sites[0].user_term == "coolness"
    assert structured_sites[0].kind is InterpretationKind.VAGUE_TERM
    assert structured_sites[1].component_id == "rate_coolness"
    assert structured_sites[1].component_type == "transform"
    assert structured_sites[1].user_term == "llm_prompt_template:rate_coolness"
    assert structured_sites[1].kind is InterpretationKind.LLM_PROMPT_TEMPLATE


def test_legacy_interpretation_requirement_missing_kind_defaults_to_vague_term() -> None:
    options = _pending_options()
    requirement = dict(options[INTERPRETATION_REQUIREMENTS_KEY][0])  # type: ignore[index]
    del requirement["kind"]
    options[INTERPRETATION_REQUIREMENTS_KEY] = [requirement]
    state = _state_with_llm(options)

    sites = interpretation_sites(state)

    assert sites[0].kind is InterpretationKind.VAGUE_TERM


def test_interpretation_requirement_non_string_kind_still_fails_closed() -> None:
    options = _pending_options()
    requirement = dict(options[INTERPRETATION_REQUIREMENTS_KEY][0])  # type: ignore[index]
    requirement["kind"] = 123
    options[INTERPRETATION_REQUIREMENTS_KEY] = [requirement]
    state = _state_with_llm(options)

    with pytest.raises(TypeError, match="interpretation requirement kind must be a string"):
        interpretation_sites(state)


def test_resolved_requirement_materializes_prompt_and_hash() -> None:
    options = _pending_options()
    requirement = dict(options[INTERPRETATION_REQUIREMENTS_KEY][0])  # type: ignore[index]
    requirement["status"] = "resolved"
    requirement["accepted_value"] = "well-designed and useful"
    prompt = "Rate well-designed and useful: {{ row.text }}"
    # The prompt-template review anchors to the prompt SKELETON (parts structure),
    # not the substituted text — so it stays valid after the vague term resolves.
    skeleton_hash = prompt_structure_hash_from_options(options)
    assert skeleton_hash is not None
    options[INTERPRETATION_REQUIREMENTS_KEY] = [
        requirement,
        {
            "id": "prompt-template-review",
            "kind": "llm_prompt_template",
            "user_term": "rating prompt",
            "status": "resolved",
            "draft": "Rate pending interpretation: {{ row.text }}",
            "event_id": "event-2",
            "accepted_value": prompt,
            "accepted_artifact_hash": None,
            "resolved_prompt_template_hash": skeleton_hash,
        },
    ]
    state = _state_with_llm(options)

    materialized = materialize_state_for_execution(state)

    assert isinstance(materialized, CompositionState)
    materialized_prompt = materialized.nodes[0].options["prompt_template"]
    assert materialized_prompt == prompt
    # Node-level hash remains the final-prompt-string hash (runtime reads it).
    assert materialized.nodes[0].options["resolved_prompt_template_hash"] == stable_hash(prompt)


def test_pending_invented_source_requirement_blocks_execution() -> None:
    state = CompositionState(
        source=SourceSpec(
            plugin="json",
            on_success="rows",
            on_validation_failure="fail",
            options={
                SOURCE_AUTHORING_KEY: {
                    "modality": "llm_generated",
                    "content_hash": "a" * 64,
                    "review_event_id": None,
                    "resolved_kind": None,
                },
                INTERPRETATION_REQUIREMENTS_KEY: [
                    {
                        "id": "source-urls",
                        "kind": "invented_source",
                        "user_term": "inline_source_url_list",
                        "status": "pending",
                        "draft": "https://example.gov.au",
                        "event_id": None,
                        "accepted_value": None,
                        "accepted_artifact_hash": None,
                        "resolved_prompt_template_hash": None,
                    }
                ],
            },
        ),
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )

    result = materialize_state_for_execution(state)

    assert isinstance(result, InterpretationReviewPending)
    assert result.sites[0].kind is InterpretationKind.INVENTED_SOURCE
    assert result.sites[0].component_id == "source"
    assert result.sites[0].component_type == "source"


def test_every_named_llm_authored_source_has_an_independent_review_site() -> None:
    def _source(*, content_hash: str, accepted_hash: str | None) -> SourceSpec:
        status = "resolved" if accepted_hash is not None else "pending"
        event_id = "event-1" if accepted_hash is not None else None
        return SourceSpec(
            plugin="json",
            on_success="rows",
            on_validation_failure="fail",
            options={
                SOURCE_AUTHORING_KEY: {
                    "modality": "llm_generated",
                    "content_hash": content_hash,
                    "review_event_id": event_id,
                    "resolved_kind": "invented_source" if event_id is not None else None,
                },
                INTERPRETATION_REQUIREMENTS_KEY: [
                    {
                        "id": "source-review",
                        "kind": "invented_source",
                        "user_term": "inline_source_data",
                        "status": status,
                        "draft": "generated rows",
                        "event_id": event_id,
                        "accepted_value": "accepted" if event_id is not None else None,
                        "accepted_artifact_hash": accepted_hash,
                        "resolved_prompt_template_hash": None,
                    }
                ],
            },
        )

    state = CompositionState(
        sources={
            "source": _source(content_hash="a" * 64, accepted_hash="a" * 64),
            "orders": _source(content_hash="b" * 64, accepted_hash=None),
            "refunds": _source(content_hash="c" * 64, accepted_hash="d" * 64),
        },
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )

    sites = interpretation_sites(state)

    assert [(site.component_id, site.user_term) for site in sites] == [
        ("source:orders", "inline_source_data"),
        ("source:refunds", "inline_source_data"),
    ]
    result = materialize_state_for_execution(state)
    assert isinstance(result, InterpretationReviewPending)
    assert result.sites == sites


def test_pending_llm_prompt_template_requirement_blocks_execution() -> None:
    state = _state_with_llm(
        {
            "prompt_template": "Rate {{ row.text }}",
            INTERPRETATION_REQUIREMENTS_KEY: [
                {
                    "id": "prompt-template-review",
                    "kind": "llm_prompt_template",
                    "user_term": "rating prompt",
                    "status": "pending",
                    "draft": "Rate {{ row.text }}",
                    "event_id": None,
                    "accepted_value": None,
                    "accepted_artifact_hash": None,
                    "resolved_prompt_template_hash": None,
                }
            ],
        }
    )

    result = materialize_state_for_execution(state)

    assert isinstance(result, InterpretationReviewPending)
    assert result.sites[0].component_id == "rate_coolness"
    assert result.sites[0].component_type == "transform"
    assert result.sites[0].kind is InterpretationKind.LLM_PROMPT_TEMPLATE


def test_pending_pipeline_decision_requirement_blocks_execution_on_non_llm_transform() -> None:
    state = _state_with_cleanup_node(_pipeline_decision_options())

    result = materialize_state_for_execution(state)

    assert isinstance(result, InterpretationReviewPending)
    assert result.sites[0].component_id == "drop_raw_html"
    assert result.sites[0].component_type == "transform"
    assert result.sites[0].user_term == "drop_raw_html_fields"
    assert result.sites[0].kind is InterpretationKind.PIPELINE_DECISION


def test_unreviewed_field_mapper_drop_of_web_scrape_raw_fields_blocks_execution() -> None:
    state = _state_with_web_scrape_cleanup_node(
        {
            "mapping": {
                "url": "url",
                "primary_colours": "primary_colours",
            },
            "select_only": True,
        }
    )

    result = materialize_state_for_execution(state)

    assert isinstance(result, InterpretationReviewPending)
    assert result.sites[0].component_id == "drop_raw_html"
    assert result.sites[0].component_type == "transform"
    assert result.sites[0].user_term == "drop_raw_html_fields"
    assert result.sites[0].kind is InterpretationKind.PIPELINE_DECISION


def test_term_matched_cleanup_row_with_rephrased_draft_is_malformed_not_absent() -> None:
    state = _state_with_web_scrape_cleanup_node(
        {
            "mapping": {
                "url": "url",
                "summary": "summary",
            },
            "select_only": True,
            INTERPRETATION_REQUIREMENTS_KEY: [
                {
                    "id": "cleanup-decision",
                    "kind": "pipeline_decision",
                    "user_term": RAW_HTML_CLEANUP_USER_TERM,
                    "status": "pending",
                    "draft": "Drop the scraped HTML content and fingerprint columns before the sink.",
                    "event_id": None,
                    "accepted_value": None,
                    "accepted_artifact_hash": None,
                    "resolved_prompt_template_hash": None,
                }
            ],
        }
    )

    contract_error = raw_html_cleanup_review_contract_error(state)

    assert contract_error is not None
    assert "malformed" in contract_error
    assert "raw html" in contract_error
    assert "fingerprint" in contract_error
    assert "copy" in contract_error.lower()
    assert "Stage a pending pipeline_decision" not in contract_error


def test_unrelated_pipeline_decision_does_not_satisfy_raw_html_cleanup_review() -> None:
    state = _state_with_web_scrape_cleanup_node(
        {
            "mapping": {
                "url": "url",
                "primary_colours": "primary_colours",
            },
            "select_only": True,
            INTERPRETATION_REQUIREMENTS_KEY: [
                {
                    "id": "unrelated-pipeline-decision",
                    "kind": "pipeline_decision",
                    "user_term": "some_other_pipeline_choice",
                    "status": "pending",
                    "draft": "Approve a different row-shaping decision.",
                    "event_id": None,
                    "accepted_value": None,
                    "accepted_artifact_hash": None,
                    "resolved_prompt_template_hash": None,
                }
            ],
        }
    )

    contract_error = raw_html_cleanup_review_contract_error(state)
    sites = interpretation_sites(state)

    assert contract_error is not None
    assert "drop_raw_html_fields" in contract_error
    assert ("drop_raw_html", "drop_raw_html_fields", InterpretationKind.PIPELINE_DECISION) in (
        (site.component_id, site.user_term, site.kind) for site in sites
    )


def test_gate_routed_web_scrape_into_llm_warns_without_prompt_shield() -> None:
    # The gate topology routes web_scrape output into the LLM via gate routes,
    # exercising the _output_stream_graph producer walk (routes/fork_to). The
    # Prompt-shield recommendation is advisory, not blocking:
    # an unshielded LLM-over-scrape surfaces a warning and still composes.
    state = _state_with_web_scrape_gate_to_llm()

    warning_pairs = prompt_shield_recommendation_warning_pairs(state)
    result = materialize_state_for_execution(state)

    assert warning_pairs
    assert any(component == "node:summarise_pages" for component, _message in warning_pairs)
    assert any("prompt-injection shield" in message for _component, message in warning_pairs)
    # Advisory, not blocking: the LLM node still triggers a prompt-template
    # review, but the prompt-shield recommendation is NOT a blocking review site.
    assert isinstance(result, InterpretationReviewPending)
    assert all(site.user_term != PROMPT_SHIELD_USER_TERM for site in result.sites)


def test_web_scrape_fed_llm_keeps_untrusted_content_drafts_verbatim() -> None:
    """The untrusted-producer branch is a genuine security advisory: its wording
    must stay byte-identical in both availability states — the local-content
    split must never soften a real scrape graph's card."""
    from elspeth.web.interpretation_state import (
        PROMPT_SHIELD_AVAILABLE_DRAFT,
        PROMPT_SHIELD_WARNING_DRAFT,
    )

    state = _state_with_web_scrape_gate_to_llm()

    pairs_c = prompt_shield_recommendation_warning_pairs(state, shield_available=False)
    assert any(PROMPT_SHIELD_WARNING_DRAFT in message for _component, message in pairs_c)

    pairs_b = prompt_shield_recommendation_warning_pairs(state, shield_available=True)
    assert any(PROMPT_SHIELD_AVAILABLE_DRAFT in message for _component, message in pairs_b)


@pytest.mark.parametrize(
    ("producer_plugin", "intermediate_plugin"),
    [
        ("blob_fetch", "blob_text_expand"),
        ("azure_document_intelligence", None),
        ("rag_retrieval", None),
        ("llm", None),
    ],
)
def test_untrusted_producers_use_provenance_neutral_content_draft(
    producer_plugin: str,
    intermediate_plugin: str | None,
) -> None:
    producer = NodeSpec(
        id="remote_producer",
        node_type="transform",
        plugin=producer_plugin,
        input="source_rows",
        on_success="remote_result",
        on_error="stop",
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )
    nodes = [producer]
    llm_input = "remote_result"
    if intermediate_plugin is not None:
        nodes.append(
            NodeSpec(
                id="remote_expander",
                node_type="transform",
                plugin=intermediate_plugin,
                input="remote_result",
                on_success="remote_content",
                on_error="stop",
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        llm_input = "remote_content"
    nodes.append(_llm(input_stream=llm_input))

    warning_pairs = prompt_shield_recommendation_warning_pairs(_state(tuple(nodes)))

    assert any(PROMPT_SHIELD_WARNING_DRAFT in message for _component, message in warning_pairs)
    message = next(message for component, message in warning_pairs if component == "node:classify")
    assert "untrusted or externally controlled upstream content" in message
    assert f"produced by {producer_plugin}" in message
    if producer_plugin in {"azure_document_intelligence", "rag_retrieval", "llm"}:
        assert all(term not in message.casefold() for term in ("fetch", "internet", "remote"))


def test_refiner_upgrades_local_content_draft_variant() -> None:
    """C->B upgrade must stay variant-aligned: a local-content warning upgrades
    to the local-content B draft, never to producer-specific wording."""
    from elspeth.web.interpretation_state import (
        PROMPT_SHIELD_AVAILABLE_DRAFT,
        PROMPT_SHIELD_LOCAL_CONTENT_AVAILABLE_DRAFT,
        PROMPT_SHIELD_LOCAL_CONTENT_WARNING_DRAFT,
        refine_prompt_shield_warnings_for_availability,
    )

    refined = refine_prompt_shield_warnings_for_availability(
        [
            {
                "component": "node:rate_node",
                "message": f"lead {PROMPT_SHIELD_LOCAL_CONTENT_WARNING_DRAFT}",
                "severity": "medium",
            }
        ],
        shield_available=True,
    )
    assert PROMPT_SHIELD_LOCAL_CONTENT_AVAILABLE_DRAFT in refined[0]["message"]
    assert PROMPT_SHIELD_AVAILABLE_DRAFT not in refined[0]["message"]
    assert PROMPT_SHIELD_LOCAL_CONTENT_WARNING_DRAFT not in refined[0]["message"]


def test_gate_routed_web_scrape_through_prompt_shield_emits_no_warning() -> None:
    state = _state_with_web_scrape_gate_shield_to_llm()

    warning_pairs = prompt_shield_recommendation_warning_pairs(state)

    assert warning_pairs == ()


# ── Queue fan-in prompt-shield asymmetry (elspeth-a5b86149d4) ────────────
# A declared queue fans multiple upstream producers into one LLM. The
# prompt-injection shield analysis MUST stay conservative across every
# predecessor path — never resolve to whichever producer registered last.
#   consumes_untrusted  = ANY predecessor path reaches untrusted-without-shield
#   has_authorized_shield = ALL predecessor paths prove a shield
#   a missing/unknown predecessor path is fail-safe (NOT proven safe)


def _queue(queue_id: str = "inbound") -> NodeSpec:
    return NodeSpec(
        id=queue_id,
        node_type="queue",
        plugin=None,
        input=queue_id,
        on_success=None,
        on_error=None,
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _row_union(
    *,
    branches: dict[str, str],
    on_success: str = "inbound",
) -> NodeSpec:
    return NodeSpec(
        id="variant_union",
        node_type="row_union",
        plugin=None,
        input=next(iter(branches.values())),
        on_success=on_success,
        on_error=None,
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=branches,
        policy=None,
        merge=None,
    )


def _web_scrape(node_id: str, *, input_stream: str, on_success: str) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin="web_scrape",
        input=input_stream,
        on_success=on_success,
        on_error="stop",
        options={"url_field": "url", "content_field": "content"},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _textract(node_id: str, *, input_stream: str, on_success: str) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin="aws_textract_document_analysis",
        input=input_stream,
        on_success=on_success,
        on_error="stop",
        options={"bucket_field": "bucket", "key_field": "key", "feature_types": ["TABLES"], "text_field": "content"},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _shield(node_id: str, *, input_stream: str, on_success: str) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin="azure_prompt_shield",
        input=input_stream,
        on_success=on_success,
        on_error="stop",
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _passthrough(node_id: str, *, input_stream: str, on_success: str) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin="value_transform",
        input=input_stream,
        on_success=on_success,
        on_error="stop",
        options={"operations": [{"target": "x", "expression": "1"}]},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _llm(node_id: str = "classify", *, input_stream: str = "inbound") -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin="llm",
        input=input_stream,
        on_success="out",
        on_error="stop",
        options={"prompt_template": "Classify {{ row.content }}."},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _state(nodes: tuple[NodeSpec, ...]) -> CompositionState:
    return CompositionState(
        source=None,
        nodes=nodes,
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def test_prompt_shield_before_web_scrape_does_not_bless_downstream_llm() -> None:
    """A shield before the untrusted producer cannot sanitize content it has not produced yet."""
    state = _state(
        (
            _shield("premature_shield", input_stream="rows", on_success="shielded_rows"),
            _web_scrape("scrape", input_stream="shielded_rows", on_success="inbound"),
            _llm(),
        )
    )

    warning_pairs = prompt_shield_recommendation_warning_pairs(state)

    assert warning_pairs
    message = next(message for component, message in warning_pairs if component == "node:classify")
    assert "produced by web_scrape" in message
    assert "without an authorized prompt-injection shield between them" in message


def test_queue_fan_in_untrusted_on_any_predecessor_marks_downstream_untrusted() -> None:
    """One untrusted predecessor among many taints the downstream LLM (ANY-path rule)."""
    # Order: the benign transform registers LAST, so the pre-fix single-producer
    # map would resolve `inbound` to it and MISS the untrusted web_scrape path.
    state = _state(
        (
            _web_scrape("scrape_a", input_stream="rows_a", on_success="inbound"),
            _passthrough("benign_b", input_stream="rows_b", on_success="inbound"),
            _queue(),
            _llm(),
        )
    )

    warning_pairs = prompt_shield_recommendation_warning_pairs(state)

    assert warning_pairs
    message = next(msg for component, msg in warning_pairs if component == "node:classify")
    assert "consumes untrusted or externally controlled upstream content produced by web_scrape" in message


# ── Document-extraction producers are untrusted too ──────────────────────
# Text extracted from an uploaded document is attacker-controlled in exactly
# the way scraped web content is: the author does not write it, and it lands
# in an LLM prompt. Classifying it as trusted let a
# source -> textract -> llm pipeline claim it consumed no untrusted content.


def test_textract_upstream_marks_downstream_llm_as_consuming_untrusted_content() -> None:
    """Document-extracted text is externally controlled, so it taints a downstream LLM."""
    state = _state(
        (
            _textract("extract", input_stream="rows", on_success="inbound"),
            _llm(),
        )
    )

    warning_pairs = prompt_shield_recommendation_warning_pairs(state)

    assert warning_pairs
    message = next(msg for component, msg in warning_pairs if component == "node:classify")
    assert "consumes untrusted or externally controlled upstream content produced by aws_textract_document_analysis" in message
    assert all(term not in message.casefold() for term in ("fetch", "internet", "remote"))


def test_inline_textract_upstream_marks_downstream_llm_as_consuming_untrusted_content() -> None:
    """The synchronous Textract sibling extracts the same attacker-controlled text."""
    inline = NodeSpec(
        id="extract",
        node_type="transform",
        plugin="aws_textract_inline_analysis",
        input="rows",
        on_success="inbound",
        on_error="stop",
        options={"document_format": "png", "feature_types": ["TABLES"], "text_field": "content"},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )
    state = _state((inline, _llm()))

    warning_pairs = prompt_shield_recommendation_warning_pairs(state)

    assert warning_pairs
    message = next(msg for component, msg in warning_pairs if component == "node:classify")
    assert "consumes untrusted or externally controlled upstream content produced by aws_textract_inline_analysis" in message
    assert all(term not in message.casefold() for term in ("fetch", "internet", "remote"))


def test_untrusted_producer_lead_names_the_actual_producer_not_web_scrape() -> None:
    """The advisory must name the producer it found — never assert a web_scrape that is absent."""
    state = _state(
        (
            _textract("extract", input_stream="rows", on_success="inbound"),
            _llm(),
        )
    )

    message = next(msg for component, msg in prompt_shield_recommendation_warning_pairs(state) if component == "node:classify")

    assert "web_scrape" not in message


def test_web_scrape_untrusted_lead_uses_shared_provenance_neutral_wording() -> None:
    """All untrusted producers share one provenance-neutral operator record."""
    state = _state(
        (
            _web_scrape("scrape", input_stream="rows", on_success="inbound"),
            _llm(),
        )
    )

    message = next(msg for component, msg in prompt_shield_recommendation_warning_pairs(state) if component == "node:classify")

    assert (
        "consumes untrusted or externally controlled upstream content produced by web_scrape without an authorized "
        "prompt-injection shield between them. " in message
    )


def test_shield_between_textract_and_llm_is_silent() -> None:
    """An authorized shield downstream of the extraction sanitizes it (State A)."""
    state = _state(
        (
            _textract("extract", input_stream="rows", on_success="extracted"),
            _shield("shield", input_stream="extracted", on_success="inbound"),
            _llm(),
        )
    )

    assert prompt_shield_recommendation_warning_pairs(state) == ()


def test_queue_fan_in_shield_authorized_only_when_all_predecessors_shielded() -> None:
    """A shield on one predecessor is not enough: an unshielded path still warns (ALL-path rule)."""
    # Order: shield_a registers LAST, so the pre-fix single-producer map would
    # resolve `inbound` to the shield and silence the warning (the inversion).
    partially_shielded = _state(
        (
            _web_scrape("scrape_a", input_stream="rows_a", on_success="scraped_a"),
            _web_scrape("scrape_b", input_stream="rows_b", on_success="inbound"),
            _shield("shield_a", input_stream="scraped_a", on_success="inbound"),
            _queue(),
            _llm(),
        )
    )

    warning_pairs = prompt_shield_recommendation_warning_pairs(partially_shielded)

    assert warning_pairs, "an unshielded predecessor path must still surface the shield advisory"
    message = next(msg for component, msg in warning_pairs if component == "node:classify")
    assert "consumes untrusted or externally controlled upstream content produced by web_scrape" in message

    # When EVERY predecessor path is shielded, State A is silent.
    fully_shielded = _state(
        (
            _web_scrape("scrape_a", input_stream="rows_a", on_success="scraped_a"),
            _web_scrape("scrape_b", input_stream="rows_b", on_success="scraped_b"),
            _shield("shield_a", input_stream="scraped_a", on_success="inbound"),
            _shield("shield_b", input_stream="scraped_b", on_success="inbound"),
            _queue(),
            _llm(),
        )
    )

    assert prompt_shield_recommendation_warning_pairs(fully_shielded) == ()


def test_row_union_requires_every_branch_to_be_prompt_shielded() -> None:
    branches = {"control": "control_done", "treatment": "treatment_done"}
    partially_shielded = _state(
        (
            _web_scrape("control_scrape", input_stream="control_url", on_success="control_raw"),
            _shield("control_shield", input_stream="control_raw", on_success="control_done"),
            _web_scrape("treatment_scrape", input_stream="treatment_url", on_success="treatment_done"),
            _row_union(branches=branches),
            _llm(),
        )
    )

    warning_pairs = prompt_shield_recommendation_warning_pairs(partially_shielded)

    assert warning_pairs
    message = next(message for component, message in warning_pairs if component == "node:classify")
    assert "produced by web_scrape" in message
    assert "without an authorized prompt-injection shield between them" in message

    fully_shielded = _state(
        (
            _web_scrape("control_scrape", input_stream="control_url", on_success="control_raw"),
            _shield("control_shield", input_stream="control_raw", on_success="control_done"),
            _web_scrape("treatment_scrape", input_stream="treatment_url", on_success="treatment_raw"),
            _shield("treatment_shield", input_stream="treatment_raw", on_success="treatment_done"),
            _row_union(branches=branches),
            _llm(),
        )
    )

    assert prompt_shield_recommendation_warning_pairs(fully_shielded) == ()


def test_row_union_artifact_hash_covers_every_branch_path() -> None:
    def build(treatment_id: str) -> CompositionState:
        return _state(
            (
                _web_scrape("control_scrape", input_stream="control_url", on_success="control_done"),
                _web_scrape(treatment_id, input_stream="treatment_url", on_success="treatment_done"),
                _row_union(branches={"control": "control_done", "treatment": "treatment_done"}),
                _llm(),
            )
        )

    baseline = build("treatment_scrape")
    changed = build("treatment_scrape_changed")
    baseline_llm = next(node for node in baseline.nodes if node.plugin == "llm")
    changed_llm = next(node for node in changed.nodes if node.plugin == "llm")

    assert pipeline_decision_artifact_hash(
        baseline_llm,
        baseline.nodes,
        user_term=PROMPT_SHIELD_USER_TERM,
    ) != pipeline_decision_artifact_hash(
        changed_llm,
        changed.nodes,
        user_term=PROMPT_SHIELD_USER_TERM,
    )


def test_queue_fan_in_one_unknown_predecessor_emits_conservative_warning() -> None:
    """An unprovable (missing-upstream) predecessor is fail-safe, so the advisory still fires."""
    # shield_a registers LAST → pre-fix map silences; post-fix ALL-path proof
    # fails because the mystery predecessor's upstream is unknown.
    state = _state(
        (
            _web_scrape("scrape_a", input_stream="rows_a", on_success="scraped_a"),
            _passthrough("mystery_b", input_stream="ghost_stream", on_success="inbound"),
            _shield("shield_a", input_stream="scraped_a", on_success="inbound"),
            _queue(),
            _llm(),
        )
    )

    warning_pairs = prompt_shield_recommendation_warning_pairs(state)

    assert warning_pairs, "an unknown predecessor path must not be treated as proven-shielded"
    assert any(component == "node:classify" for component, _msg in warning_pairs)


def test_queue_fan_in_artifact_hash_covers_all_sorted_predecessor_paths() -> None:
    """The prompt-shield artifact hash binds every predecessor path, order-invariant."""
    baseline = _state(
        (
            _web_scrape("scrape_a", input_stream="rows_a", on_success="inbound"),
            _web_scrape("scrape_b", input_stream="rows_b", on_success="inbound"),
            _queue(),
            _llm(),
        )
    )
    reversed_order = _state(
        (
            _web_scrape("scrape_b", input_stream="rows_b", on_success="inbound"),
            _web_scrape("scrape_a", input_stream="rows_a", on_success="inbound"),
            _queue(),
            _llm(),
        )
    )

    def _hash(state: CompositionState) -> str:
        llm = next(node for node in state.nodes if node.plugin == "llm")
        return pipeline_decision_artifact_hash(llm, state.nodes, user_term=PROMPT_SHIELD_USER_TERM)

    baseline_hash = _hash(baseline)

    # Insertion order of the two predecessors does not change the hash.
    assert baseline_hash == _hash(reversed_order)

    # Changing EITHER predecessor changes the hash.
    changed_a = _state(
        (
            _web_scrape("scrape_a_renamed", input_stream="rows_a", on_success="inbound"),
            _web_scrape("scrape_b", input_stream="rows_b", on_success="inbound"),
            _queue(),
            _llm(),
        )
    )
    changed_b = _state(
        (
            _web_scrape("scrape_a", input_stream="rows_a", on_success="inbound"),
            _passthrough("scrape_b", input_stream="rows_b", on_success="inbound"),
            _queue(),
            _llm(),
        )
    )
    assert _hash(changed_a) != baseline_hash
    assert _hash(changed_b) != baseline_hash


def _state_with_plain_llm_only() -> CompositionState:
    """One llm node with NO upstream producer at all (no web_scrape, no shield)."""
    return CompositionState(
        source=None,
        nodes=(
            NodeSpec(
                id="rate_node",
                node_type="transform",
                plugin="llm",
                input="rows",
                on_success="out",
                on_error="stop",
                options={
                    "provider": "openrouter",
                    "model": "anthropic/claude-sonnet-4.6",
                    "prompt_template": "Rate {{ row.text }} and return JSON.",
                    "temperature": 0,
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def test_plain_unshielded_llm_warns_always_on() -> None:
    # Always-on: an llm node with no upstream producer and no shield still
    # surfaces the advisory (State C default). The pre-change code returned () here.
    state = _state_with_plain_llm_only()
    warning_pairs = prompt_shield_recommendation_warning_pairs(state)
    assert warning_pairs
    assert any(component == "node:rate_node" for component, _message in warning_pairs)


def test_prompt_shield_warning_uses_available_draft_in_state_b() -> None:
    """A fetch-less graph gets the LOCAL-CONTENT drafts in both states.

    The producer-specific constants assert a declared untrusted-content
    producer; on this graph (plain llm over operator-supplied rows, no upstream
    producer) that claim is false, and the staged review
    card carries the draft alone — so the draft itself must tell the truth
    (build-review finding L2, session 94bdae4f).
    """
    from elspeth.web.interpretation_state import (
        PROMPT_SHIELD_LOCAL_CONTENT_AVAILABLE_DRAFT,
        PROMPT_SHIELD_LOCAL_CONTENT_WARNING_DRAFT,
    )

    state = _state_with_plain_llm_only()
    pairs_b = prompt_shield_recommendation_warning_pairs(state, shield_available=True)
    assert pairs_b
    assert any(PROMPT_SHIELD_LOCAL_CONTENT_AVAILABLE_DRAFT in message for _component, message in pairs_b)
    assert all("external-content fetch step and this LLM" not in message for _component, message in pairs_b)

    pairs_c = prompt_shield_recommendation_warning_pairs(state, shield_available=False)
    assert pairs_c
    assert any(PROMPT_SHIELD_LOCAL_CONTENT_WARNING_DRAFT in message for _component, message in pairs_c)
    assert any("continuing without a shield is allowed" in message for _component, message in pairs_c)


def test_field_mapper_projection_without_web_scrape_raw_fields_does_not_create_cleanup_review_site() -> None:
    state = _state_with_cleanup_node(
        {
            "mapping": {
                "url": "url",
                "primary_colours": "primary_colours",
            },
            "select_only": True,
        }
    )

    result = materialize_state_for_execution(state)

    assert isinstance(result, CompositionState)


def test_resolved_pipeline_decision_requires_matching_node_hash() -> None:
    reviewed = _state_with_cleanup_node(_pipeline_decision_options())
    artifact_hash = pipeline_decision_artifact_hash(
        reviewed.nodes[0],
        reviewed.nodes,
        user_term=RAW_HTML_CLEANUP_USER_TERM,
    )
    state = _state_with_cleanup_node(_pipeline_decision_options(status="resolved", artifact_hash=artifact_hash))

    materialized = materialize_state_for_execution(state)

    assert isinstance(materialized, CompositionState)
    assert materialized.nodes[0].options["mapping"] == strip_authoring_options(reviewed.nodes[0].options)["mapping"]


def test_resolved_pipeline_decision_hash_drift_fails_closed() -> None:
    state = _state_with_cleanup_node(_pipeline_decision_options(status="resolved", artifact_hash=stable_hash("old node shape")))

    with pytest.raises(ValueError, match="pipeline-decision review hash drifted"):
        materialize_state_for_execution(state)


def test_resolved_raw_html_cleanup_decision_rejects_mapping_that_preserves_raw_fields() -> None:
    reviewed = _state_with_cleanup_node(_pipeline_decision_options())
    bad_options = dict(reviewed.nodes[0].options)
    bad_options["mapping"] = {
        "url": "url",
        "content": "content",
        "content_fingerprint": "content_fingerprint",
        "primary_colours": "primary_colours",
    }
    clean_options = strip_authoring_options(bad_options)
    artifact_hash = stable_hash(
        {
            "id": "drop_raw_html",
            "node_type": "transform",
            "plugin": "field_mapper",
            "input": "scored_rows",
            "on_success": "clean_rows",
            "on_error": "stop",
            "options": clean_options,
        }
    )
    state = _state_with_cleanup_node(bad_options)
    requirement = dict(state.nodes[0].options[INTERPRETATION_REQUIREMENTS_KEY][0])
    requirement["status"] = "resolved"
    requirement["event_id"] = "event-raw-html-drop"
    requirement["accepted_value"] = requirement["draft"]
    requirement["accepted_artifact_hash"] = artifact_hash
    patched_options = dict(state.nodes[0].options)
    patched_options[INTERPRETATION_REQUIREMENTS_KEY] = [requirement]
    state = _state_with_cleanup_node(patched_options)

    with pytest.raises(ValueError, match="preserves raw HTML/fingerprint field"):
        materialize_state_for_execution(state)


def test_resolved_raw_html_cleanup_decision_rejects_custom_raw_field_preservation() -> None:
    options = _pipeline_decision_options(status="resolved")
    options["mapping"] = {
        "url": "url",
        "page_body": "page_body",
        "page_hash": "page_hash",
        "primary_colours": "primary_colours",
    }
    base = _state_with_web_scrape_cleanup_node(options)
    scrape = replace(
        base.nodes[0],
        options={
            "url_field": "url",
            "content_field": "page_body",
            "fingerprint_field": "page_hash",
        },
    )
    mapper = replace(base.nodes[1], id="select_fields")
    state_without_hash = replace(base, nodes=(scrape, mapper))
    artifact_hash = pipeline_decision_artifact_hash(
        state_without_hash.nodes[1],
        state_without_hash.nodes,
        user_term=RAW_HTML_CLEANUP_USER_TERM,
    )
    requirement = dict(options[INTERPRETATION_REQUIREMENTS_KEY][0])  # type: ignore[index]
    requirement["accepted_artifact_hash"] = artifact_hash
    patched_options = dict(options)
    patched_options[INTERPRETATION_REQUIREMENTS_KEY] = [requirement]
    state = replace(state_without_hash, nodes=(scrape, replace(mapper, options=patched_options)))

    with pytest.raises(ValueError, match="page_body"):
        materialize_state_for_execution(state)


def test_resolved_llm_prompt_template_requires_matching_hash() -> None:
    prompt = "Rate {{ row.text }}"
    state = _state_with_llm(
        {
            "prompt_template": prompt,
            INTERPRETATION_REQUIREMENTS_KEY: [
                {
                    "id": "prompt-template-review",
                    "kind": "llm_prompt_template",
                    "user_term": "rating prompt",
                    "status": "resolved",
                    "draft": prompt,
                    "event_id": "event-1",
                    "accepted_value": prompt,
                    "accepted_artifact_hash": None,
                    "resolved_prompt_template_hash": stable_hash("different prompt"),
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="prompt-template review hash drifted"):
        materialize_state_for_execution(state)


def test_plain_llm_prompt_template_without_review_metadata_blocks_execution() -> None:
    state = _state_with_llm({"prompt_template": "Summarize {{ row.text }}"})

    result = materialize_state_for_execution(state)

    assert isinstance(result, InterpretationReviewPending)
    assert result.sites[0].component_id == "rate_coolness"
    assert result.sites[0].component_type == "transform"
    assert result.sites[0].user_term == "llm_prompt_template:rate_coolness"
    assert result.sites[0].kind is InterpretationKind.LLM_PROMPT_TEMPLATE


def test_resolved_llm_prompt_template_requirement_without_hash_fails_closed() -> None:
    prompt = "Rate {{ row.text }}"
    state = _state_with_llm(
        {
            "prompt_template": prompt,
            INTERPRETATION_REQUIREMENTS_KEY: [
                {
                    "id": "prompt-template-review",
                    "kind": "llm_prompt_template",
                    "user_term": "rating prompt",
                    "status": "resolved",
                    "draft": prompt,
                    "event_id": "event-1",
                    "accepted_value": prompt,
                    "accepted_artifact_hash": None,
                    "resolved_prompt_template_hash": None,
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="prompt-template review hash drifted"):
        materialize_state_for_execution(state)


def test_llm_generated_source_metadata_without_requirement_is_review_site() -> None:
    state = CompositionState(
        source=SourceSpec(
            plugin="json",
            on_success="rows",
            on_validation_failure="fail",
            options={
                SOURCE_AUTHORING_KEY: {
                    "modality": "llm_generated",
                    "content_hash": "a" * 64,
                    "review_event_id": None,
                    "resolved_kind": None,
                }
            },
        ),
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )

    sites = interpretation_sites(state)
    assert len(sites) == 1
    assert sites[0].component_id == "source"
    assert sites[0].component_type == "source"
    assert sites[0].user_term == "llm_generated_source"
    assert sites[0].kind is InterpretationKind.INVENTED_SOURCE


def test_llm_generated_source_metadata_without_resolved_requirement_blocks_execution() -> None:
    state = CompositionState(
        source=SourceSpec(
            plugin="json",
            on_success="rows",
            on_validation_failure="fail",
            options={
                SOURCE_AUTHORING_KEY: {
                    "modality": "llm_generated",
                    "content_hash": "a" * 64,
                    "review_event_id": None,
                    "resolved_kind": None,
                }
            },
        ),
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )

    result = materialize_state_for_execution(state)

    assert isinstance(result, InterpretationReviewPending)
    assert result.sites[0].component_id == "source"
    assert result.sites[0].component_type == "source"
    assert result.sites[0].user_term == "llm_generated_source"
    assert result.sites[0].kind is InterpretationKind.INVENTED_SOURCE


def test_resolved_invented_source_requirement_requires_matching_artifact_hash() -> None:
    state = CompositionState(
        source=SourceSpec(
            plugin="json",
            on_success="rows",
            on_validation_failure="fail",
            options={
                SOURCE_AUTHORING_KEY: {
                    "modality": "llm_generated",
                    "content_hash": "a" * 64,
                    "review_event_id": "event-1",
                    "resolved_kind": "invented_source",
                },
                INTERPRETATION_REQUIREMENTS_KEY: [
                    {
                        "id": "source-urls",
                        "kind": "invented_source",
                        "user_term": "inline_source_url_list",
                        "status": "resolved",
                        "draft": "https://example.gov.au",
                        "event_id": "event-1",
                        "accepted_value": "accepted source artifact",
                        "accepted_artifact_hash": "b" * 64,
                        "resolved_prompt_template_hash": None,
                    }
                ],
            },
        ),
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )

    # Drift after review (accepted_artifact_hash != current source content_hash)
    # is a readiness blocker surfaced through the structured interpretation-review
    # machinery, NOT a bare ValueError that leaks to the route layer as a 404/500.
    # (This assertion was previously `pytest.raises(ValueError, ...)`, which
    # codified the defect rather than the desired behaviour — inverted for
    # elspeth-5a94855935.)
    result = materialize_state_for_execution(state)

    assert isinstance(result, InterpretationReviewPending)
    assert result.sites[0].component_type == "source"
    assert result.sites[0].component_id == "source"
    assert result.sites[0].kind is InterpretationKind.INVENTED_SOURCE


def test_resolved_invented_source_drift_surfaces_as_pending_review_site() -> None:
    state = CompositionState(
        source=SourceSpec(
            plugin="json",
            on_success="rows",
            on_validation_failure="fail",
            options={
                SOURCE_AUTHORING_KEY: {
                    "modality": "llm_generated",
                    "content_hash": "a" * 64,
                    "review_event_id": "event-1",
                    "resolved_kind": "invented_source",
                },
                INTERPRETATION_REQUIREMENTS_KEY: [
                    {
                        "id": "source-urls",
                        "kind": "invented_source",
                        "user_term": "inline_source_url_list",
                        "status": "resolved",
                        "draft": "https://example.gov.au",
                        "event_id": "event-1",
                        "accepted_value": "accepted source artifact",
                        "accepted_artifact_hash": "b" * 64,
                        "resolved_prompt_template_hash": None,
                    }
                ],
            },
        ),
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )

    # The readiness detector (single source of truth for /validate and /execute)
    # must report the drifted-after-review source as a pending review site, so the
    # existing InterpretationReviewPending path handles it instead of the
    # downstream bare ValueError.
    sites = interpretation_sites(state)

    assert len(sites) == 1
    assert sites[0].component_type == "source"
    assert sites[0].component_id == "source"
    assert sites[0].kind is InterpretationKind.INVENTED_SOURCE
    assert sites[0].user_term == "inline_source_url_list"

    result = materialize_state_for_execution(state)

    assert isinstance(result, InterpretationReviewPending)
    assert result.sites[0].kind is InterpretationKind.INVENTED_SOURCE


def test_resolved_invented_source_matching_hash_does_not_block_execution() -> None:
    state = CompositionState(
        source=SourceSpec(
            plugin="json",
            on_success="rows",
            on_validation_failure="fail",
            options={
                SOURCE_AUTHORING_KEY: {
                    "modality": "llm_generated",
                    "content_hash": "a" * 64,
                    "review_event_id": "event-1",
                    "resolved_kind": "invented_source",
                },
                INTERPRETATION_REQUIREMENTS_KEY: [
                    {
                        "id": "source-urls",
                        "kind": "invented_source",
                        "user_term": "inline_source_url_list",
                        "status": "resolved",
                        "draft": "https://example.gov.au",
                        "event_id": "event-1",
                        "accepted_value": "accepted source artifact",
                        "accepted_artifact_hash": "a" * 64,
                        "resolved_prompt_template_hash": None,
                    }
                ],
            },
        ),
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )

    assert interpretation_sites(state) == ()
    result = materialize_state_for_execution(state)
    assert not isinstance(result, InterpretationReviewPending)


def test_strip_authoring_options_removes_metadata_keys() -> None:
    options = {
        "prompt_template": "Rate {{ row.text }}",
        PROMPT_TEMPLATE_PARTS_KEY: [],
        INTERPRETATION_REQUIREMENTS_KEY: [],
        SOURCE_AUTHORING_KEY: {
            "modality": "llm_generated",
            "content_hash": "abc123",
            "review_event_id": None,
            "resolved_kind": None,
        },
        "resolved_prompt_template_hash": "a" * 64,
    }

    stripped = strip_authoring_options(options)

    assert PROMPT_TEMPLATE_PARTS_KEY not in stripped
    assert INTERPRETATION_REQUIREMENTS_KEY not in stripped
    assert SOURCE_AUTHORING_KEY not in stripped
    assert stripped["resolved_prompt_template_hash"] == "a" * 64


# ---------------------------------------------------------------------------
# pipeline_decision_artifact_hash — material-only scoping
#
# Regression coverage for the staging incident where the composer LLM swapped
# the LLM ``model`` field (claude-3.7-sonnet → claude-3.5-sonnet) after the
# prompt-shield-recommendation review had already resolved. The legacy
# whole-stripped-options hash treated the model change as material drift and
# crashed preflight with a confusing "pipeline-decision review hash drifted"
# message. The narrowed hash binds to topology only, so model edits no longer
# spurious-drift the review.
# ---------------------------------------------------------------------------


def _state_with_web_scrape_llm_pair(llm_options: dict[str, Any]) -> CompositionState:
    """Two-node state: untrusted web_scrape upstream feeding an LLM."""

    return CompositionState(
        source=None,
        nodes=(
            NodeSpec(
                id="fetch_pages",
                node_type="transform",
                plugin="web_scrape",
                input="url_rows",
                on_success="scraped_pages",
                on_error="stop",
                options={
                    "url_field": "url",
                    "content_field": "content",
                    "fingerprint_field": "fingerprint",
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
            NodeSpec(
                id="identify_colours",
                node_type="transform",
                plugin="llm",
                input="scraped_pages",
                on_success="coloured_pages",
                on_error="stop",
                options=llm_options,
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _shielded_three_node_state(llm_options: dict[str, Any]) -> CompositionState:
    """Three-node state: web_scrape → azure_prompt_shield → LLM."""

    return CompositionState(
        source=None,
        nodes=(
            NodeSpec(
                id="fetch_pages",
                node_type="transform",
                plugin="web_scrape",
                input="url_rows",
                on_success="scraped_pages",
                on_error="stop",
                options={
                    "url_field": "url",
                    "content_field": "content",
                    "fingerprint_field": "fingerprint",
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
            NodeSpec(
                id="shield_content",
                node_type="transform",
                plugin="azure_prompt_shield",
                input="scraped_pages",
                on_success="shielded_pages",
                on_error="stop",
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
            NodeSpec(
                id="identify_colours",
                node_type="transform",
                plugin="llm",
                input="shielded_pages",
                on_success="coloured_pages",
                on_error="stop",
                options=llm_options,
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _shield_review_llm_options(*, model: str, prompt_template: str) -> dict[str, Any]:
    return {
        "provider": "openrouter",
        "model": model,
        "prompt_template": prompt_template,
        "temperature": 0,
        INTERPRETATION_REQUIREMENTS_KEY: [
            {
                "id": "prompt_injection_shield_review:identify_colours",
                "kind": "pipeline_decision",
                "user_term": PROMPT_SHIELD_USER_TERM,
                "status": "resolved",
                "draft": "Public web content from web_scrape flows directly into the LLM. Recommend inserting azure_prompt_shield.",
                "event_id": "shield-resolve-1",
                "accepted_value": "Public web content from web_scrape flows directly into the LLM. Recommend inserting azure_prompt_shield.",
                "accepted_artifact_hash": None,
                "resolved_prompt_template_hash": None,
            }
        ],
    }


def _unshielded_review_llm_options(*, model: str, prompt_template: str) -> dict[str, Any]:
    return {
        "provider": "openrouter",
        "model": model,
        "prompt_template": prompt_template,
        "temperature": 0,
        INTERPRETATION_REQUIREMENTS_KEY: [
            {
                "id": "prompt_template_review:identify_colours",
                "kind": "llm_prompt_template",
                "user_term": "llm_prompt_template:identify_colours",
                "status": "resolved",
                "draft": prompt_template,
                "event_id": "prompt-resolve-1",
                "accepted_value": prompt_template,
                "accepted_artifact_hash": None,
                "resolved_prompt_template_hash": stable_hash(prompt_template),
            },
            {
                "id": "model_choice_review:identify_colours",
                "kind": "llm_model_choice",
                "user_term": "llm_model_choice:identify_colours",
                "status": "resolved",
                "draft": model,
                "event_id": "model-resolve-1",
                "accepted_value": model,
                "accepted_artifact_hash": None,
                "resolved_prompt_template_hash": stable_hash(model),
            },
        ],
    }


def test_prompt_shield_hash_is_stable_across_model_swap() -> None:
    """Regression: changing the LLM model after the shield review must not drift the hash.

    This is the exact staging failure — composer LLM resolved the shield review
    with model=3.7-sonnet, then swapped model=3.5-sonnet after a separate
    value-source validation failure. The narrowed hash binds to topology, not
    model identity, so the swap is immaterial.
    """

    state_v37 = _state_with_web_scrape_llm_pair(
        _shield_review_llm_options(model="anthropic/claude-3.7-sonnet", prompt_template="Identify colours: {{ row.url }} {{ row.content }}")
    )
    state_v35 = _state_with_web_scrape_llm_pair(
        _shield_review_llm_options(model="anthropic/claude-3.5-sonnet", prompt_template="Identify colours: {{ row.url }} {{ row.content }}")
    )

    hash_v37 = pipeline_decision_artifact_hash(state_v37.nodes[1], state_v37.nodes, user_term=PROMPT_SHIELD_USER_TERM)
    hash_v35 = pipeline_decision_artifact_hash(state_v35.nodes[1], state_v35.nodes, user_term=PROMPT_SHIELD_USER_TERM)

    assert hash_v37 == hash_v35


def test_prompt_shield_hash_is_stable_across_prompt_template_edit() -> None:
    """Editing prompt_template doesn't invalidate the shield recommendation."""

    state_a = _state_with_web_scrape_llm_pair(
        _shield_review_llm_options(model="anthropic/claude-3.7-sonnet", prompt_template="Original prompt: {{ row.url }}")
    )
    state_b = _state_with_web_scrape_llm_pair(
        _shield_review_llm_options(
            model="anthropic/claude-3.7-sonnet", prompt_template="Rewritten prompt with extra prose: {{ row.url }} {{ row.content }}"
        )
    )

    hash_a = pipeline_decision_artifact_hash(state_a.nodes[1], state_a.nodes, user_term=PROMPT_SHIELD_USER_TERM)
    hash_b = pipeline_decision_artifact_hash(state_b.nodes[1], state_b.nodes, user_term=PROMPT_SHIELD_USER_TERM)

    assert hash_a == hash_b


def test_prompt_shield_hash_changes_when_authorized_shield_inserted() -> None:
    """Inserting azure_prompt_shield between web_scrape and LLM is material.

    The review's premise was "no shield exists." Adding a shield invalidates
    that premise — the operator needs to confirm whether the prior review is
    still meaningful (it is moot, but the audit trail should reflect that).
    """

    unshielded = _state_with_web_scrape_llm_pair(
        _shield_review_llm_options(model="anthropic/claude-3.7-sonnet", prompt_template="Identify: {{ row.content }}")
    )
    shielded = _shielded_three_node_state(
        _shield_review_llm_options(model="anthropic/claude-3.7-sonnet", prompt_template="Identify: {{ row.content }}")
    )

    hash_unshielded = pipeline_decision_artifact_hash(unshielded.nodes[1], unshielded.nodes, user_term=PROMPT_SHIELD_USER_TERM)
    hash_shielded = pipeline_decision_artifact_hash(shielded.nodes[2], shielded.nodes, user_term=PROMPT_SHIELD_USER_TERM)

    assert hash_unshielded != hash_shielded


def test_prompt_shield_hash_survives_model_swap() -> None:
    """Narrow shield-hash invariant: model swap does not invalidate shield review.

    Mirrors the staging session timeline (b930f0aa-…) — the composer LLM
    resolved the shield recommendation against a v4 state with model=3.7-sonnet
    and then patched the model to 3.5-sonnet at v8. The narrowed
    ``pipeline_decision_artifact_hash`` domain (which deliberately excludes
    ``options.model``) MUST keep the prompt-shield review valid through
    that swap — the shield review's premise is "untrusted content into an
    LLM", not "this specific model".

    Asserts ONLY the hash-domain invariant. The model swap separately
    surfaces a new ``llm_model_choice`` review (algorithmic enforcement
    of the "every model choice surfaced" contract), and the auto-stager
    is responsible for re-staging that requirement when the mutation
    pipeline patches ``options.model``. That behavior is pinned in the
    auto-stager tests; this test focuses on the shield-hash invariant
    alone.
    """

    prompt = "Identify: {{ row.url }} {{ row.content }}"

    pre_swap = _state_with_web_scrape_llm_pair(_shield_review_llm_options(model="anthropic/claude-3.7-sonnet", prompt_template=prompt))
    pre_swap_hash = pipeline_decision_artifact_hash(pre_swap.nodes[1], pre_swap.nodes, user_term=PROMPT_SHIELD_USER_TERM)

    post_swap = _state_with_web_scrape_llm_pair(_shield_review_llm_options(model="anthropic/claude-3.5-sonnet", prompt_template=prompt))
    post_swap_hash = pipeline_decision_artifact_hash(post_swap.nodes[1], post_swap.nodes, user_term=PROMPT_SHIELD_USER_TERM)

    assert pre_swap_hash == post_swap_hash


def test_web_scrape_http_identity_hash_binds_wire_visible_defaults() -> None:
    state = _state_with_web_scrape_identity_node()

    artifact_hash = pipeline_decision_artifact_hash(
        state.nodes[0],
        state.nodes,
        user_term="web_scrape_http_identity",
    )

    assert artifact_hash == stable_hash(
        {
            "review_kind": "web_scrape_http_identity",
            "node_id": "fetch_pages",
            "abuse_contact": "abuse-contact-unset@elspeth.example.gov.au",
            "scraping_reason": "User-requested public web fetch for rules download",
            "allowed_hosts": "public_only",
        }
    )


def test_web_scrape_http_identity_hash_changes_when_identity_changes() -> None:
    state_a = _state_with_web_scrape_identity_node()
    state_b = _state_with_web_scrape_identity_node(abuse_contact="ops@agency.gov.au")

    hash_a = pipeline_decision_artifact_hash(state_a.nodes[0], state_a.nodes, user_term="web_scrape_http_identity")
    hash_b = pipeline_decision_artifact_hash(state_b.nodes[0], state_b.nodes, user_term="web_scrape_http_identity")

    assert hash_a != hash_b


def test_web_scrape_http_identity_hash_treats_omitted_allowed_hosts_as_public_only() -> None:
    state_explicit = _state_with_web_scrape_identity_node(allowed_hosts="public_only")
    state_omitted = _state_with_web_scrape_identity_node(allowed_hosts=None)

    hash_explicit = pipeline_decision_artifact_hash(
        state_explicit.nodes[0],
        state_explicit.nodes,
        user_term="web_scrape_http_identity",
    )
    hash_omitted = pipeline_decision_artifact_hash(
        state_omitted.nodes[0],
        state_omitted.nodes,
        user_term="web_scrape_http_identity",
    )

    assert hash_explicit == hash_omitted


def test_web_scrape_http_identity_hash_changes_when_fetch_boundary_changes() -> None:
    state_a = _state_with_web_scrape_identity_node()
    state_b = _state_with_web_scrape_identity_node(allowed_hosts=["10.0.0.0/8"])

    hash_a = pipeline_decision_artifact_hash(state_a.nodes[0], state_a.nodes, user_term="web_scrape_http_identity")
    hash_b = pipeline_decision_artifact_hash(state_b.nodes[0], state_b.nodes, user_term="web_scrape_http_identity")

    assert hash_a != hash_b


def test_prompt_shield_warning_is_advisory_not_blocking() -> None:
    state = _state_with_web_scrape_llm_pair(
        _unshielded_review_llm_options(
            model="anthropic/claude-3.7-sonnet", prompt_template="Identify colours: {{ row.url }} {{ row.content }}"
        )
    )

    validation = state.validate()
    warning_text = " ".join(w.message for w in validation.warnings)

    # The warning identifies its subject in user register — the raw registry
    # key (`prompt_injection_shield_recommendation`) rides the requirement's
    # structured user_term field, never the message body (elspeth-9665dcca32).
    assert "prompt-injection" in warning_text
    assert "prompt_injection_shield_recommendation" not in warning_text
    assert "continuing without it is allowed" in warning_text

    materialized = materialize_state_for_execution(state)
    assert isinstance(materialized, CompositionState)


def test_raw_html_cleanup_hash_includes_upstream_raw_field_set() -> None:
    """Adding a new raw field to upstream web_scrape re-stages cleanup review.

    If the upstream web_scrape suddenly emits a new raw field (e.g. an
    additional fingerprint variant), the prior cleanup review didn't authorise
    dropping it. The hash domain has to surface that as drift so the operator
    re-confirms.
    """

    base = _state_with_web_scrape_cleanup_node({"mapping": {"url": "url", "primary_colours": "primary_colours"}, "select_only": True})
    hash_a = pipeline_decision_artifact_hash(base.nodes[1], base.nodes, user_term=RAW_HTML_CLEANUP_USER_TERM)

    # Upstream web_scrape now also exports a "fingerprint" field via fingerprint_field.
    extended_nodes = list(base.nodes)
    web_scrape = extended_nodes[0]
    extended_options = dict(web_scrape.options)
    extended_options["fingerprint_field"] = "fingerprint_v2"
    extended_nodes[0] = NodeSpec(
        id=web_scrape.id,
        node_type=web_scrape.node_type,
        plugin=web_scrape.plugin,
        input=web_scrape.input,
        on_success=web_scrape.on_success,
        on_error=web_scrape.on_error,
        options=extended_options,
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )
    extended_state = CompositionState(
        source=None,
        nodes=tuple(extended_nodes),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )
    hash_b = pipeline_decision_artifact_hash(extended_state.nodes[1], extended_state.nodes, user_term=RAW_HTML_CLEANUP_USER_TERM)

    assert hash_a != hash_b


def test_pipeline_decision_artifact_hash_rejects_unknown_user_term() -> None:
    """Adding a new pipeline-decision kind requires a registered helper.

    We refuse to fall through to a permissive default — every kind needs its
    own material projection or the audit boundary becomes lossy.
    """

    state = _state_with_cleanup_node({"mapping": {"url": "url"}, "select_only": True})

    with pytest.raises(ValueError, match="unknown pipeline_decision user_term"):
        pipeline_decision_artifact_hash(state.nodes[0], state.nodes, user_term="some_new_review_we_havent_implemented")


# --------------------------------------------------------------------------- #
# vague_term_wiring_count — the single resolvability contract shared by the
# tool boundary, the staging repair loop, and (mirrored) the resolver.
# --------------------------------------------------------------------------- #


def _vague_requirement(*, term: str = "cool", status: str = "pending") -> dict[str, object]:
    return {
        "id": term,
        "kind": InterpretationKind.VAGUE_TERM.value,
        "user_term": term,
        "status": status,
        "draft": "visually appealing",
        "event_id": None,
        "accepted_value": None,
        "resolved_prompt_template_hash": None,
    }


def test_wiring_count_structured_with_ref_is_resolvable() -> None:
    options = {
        "prompt_template": "Rate pending: {{ row.text }}",
        PROMPT_TEMPLATE_PARTS_KEY: [
            {"kind": "text", "text": "Rate "},
            {"kind": "interpretation_ref", "requirement_id": "cool"},
            {"kind": "text", "text": ": {{ row.text }}"},
        ],
        INTERPRETATION_REQUIREMENTS_KEY: [_vague_requirement()],
    }
    assert vague_term_wiring_count(options, user_term="cool") == 1


def test_wiring_count_structured_requirement_without_parts_is_unresolvable() -> None:
    """The demo-blocking shape: a requirement with no prompt_template_parts."""
    options = {
        "prompt_template": "Rate pending: {{ row.text }}",
        INTERPRETATION_REQUIREMENTS_KEY: [_vague_requirement()],
    }
    assert vague_term_wiring_count(options, user_term="cool") == 0


def test_wiring_count_structured_parts_without_ref_is_unresolvable() -> None:
    """Parts present but no interpretation_ref → the resolver would silent-drop."""
    options = {
        "prompt_template": "Rate pending: {{ row.text }}",
        PROMPT_TEMPLATE_PARTS_KEY: [{"kind": "text", "text": "Rate this row"}],
        INTERPRETATION_REQUIREMENTS_KEY: [_vague_requirement()],
    }
    assert vague_term_wiring_count(options, user_term="cool") == 0


def test_wiring_count_legacy_placeholder_coexists_with_autostaged_requirements() -> None:
    """A legacy {{interpretation:cool}} placeholder is resolvable even when the
    node carries auto-staged prompt-template / model-choice requirements (which
    are NOT vague_term). This is the production hello-world shape.
    """
    options = {
        "prompt_template": "Rate how {{interpretation:cool}} this row is.",
        INTERPRETATION_REQUIREMENTS_KEY: [
            {
                "id": "prompt_template_review:rate_node",
                "kind": InterpretationKind.LLM_PROMPT_TEMPLATE.value,
                "user_term": "llm_prompt_template:rate_node",
                "status": "pending",
            },
            {
                "id": "model_choice_review:rate_node",
                "kind": InterpretationKind.LLM_MODEL_CHOICE.value,
                "user_term": "llm_model_choice:rate_node",
                "status": "pending",
            },
        ],
    }
    assert vague_term_wiring_count(options, user_term="cool") == 1


def test_wiring_count_no_wiring_at_all_is_unresolvable() -> None:
    options = {"prompt_template": "Rate how this row is."}
    assert vague_term_wiring_count(options, user_term="cool") == 0


def test_wiring_count_wrong_term_is_unresolvable() -> None:
    options = {
        "prompt_template": "Rate how {{interpretation:important}} this row is.",
    }
    assert vague_term_wiring_count(options, user_term="cool") == 0


# ---------------------------------------------------------------------------
# prompt_shield_state_for_node — A/B/C state helper
# ---------------------------------------------------------------------------


def test_prompt_shield_state_for_node_returns_A_when_shielded() -> None:
    from elspeth.web.interpretation_state import prompt_shield_state_for_node

    state = _state_with_web_scrape_gate_shield_to_llm()
    llm = next(n for n in state.nodes if n.plugin == "llm")
    assert prompt_shield_state_for_node(llm, state.nodes, shield_available=True) == "A"
    assert prompt_shield_state_for_node(llm, state.nodes, shield_available=False) == "A"


def test_prompt_shield_state_for_node_B_vs_C() -> None:
    from elspeth.web.interpretation_state import prompt_shield_state_for_node

    state = _state_with_plain_llm_only()
    llm = next(n for n in state.nodes if n.plugin == "llm")
    assert prompt_shield_state_for_node(llm, state.nodes, shield_available=True) == "B"
    assert prompt_shield_state_for_node(llm, state.nodes, shield_available=False) == "C"


# refine_prompt_shield_warnings_for_availability — B-vs-C post-processor
# ---------------------------------------------------------------------------


def test_refine_prompt_shield_warnings_rewrites_c_to_b_when_available() -> None:
    from elspeth.web.interpretation_state import (
        PROMPT_SHIELD_AVAILABLE_DRAFT,
        PROMPT_SHIELD_WARNING_DRAFT,
        refine_prompt_shield_warnings_for_availability,
    )

    c_warnings = [
        {"component": "node:rate_node", "message": f"lead {PROMPT_SHIELD_WARNING_DRAFT}", "severity": "medium"},
        {"component": "node:other", "message": "unrelated warning", "severity": "medium"},
    ]
    refined = refine_prompt_shield_warnings_for_availability(c_warnings, shield_available=True)
    shield = [w for w in refined if w["component"] == "node:rate_node"]
    assert shield
    assert PROMPT_SHIELD_AVAILABLE_DRAFT in shield[0]["message"]
    assert PROMPT_SHIELD_WARNING_DRAFT not in shield[0]["message"]
    other = [w for w in refined if w["component"] == "node:other"]
    assert other[0]["message"] == "unrelated warning"
    unchanged = refine_prompt_shield_warnings_for_availability(c_warnings, shield_available=False)
    assert any(PROMPT_SHIELD_WARNING_DRAFT in w["message"] for w in unchanged)


def test_pipeline_decision_semantics_rejects_unregistered_user_term() -> None:
    """A pipeline_decision review term outside the closed registry must be
    rejected at validation — the resolve-side artifact-hash registry raises
    on unknown terms, so accepting one at authoring mints an event that can
    NEVER be resolved and wedges the session (live: session 0b33d895,
    model-invented term 'ab_reconciliation_retention' → resolve 500)."""
    import pytest as _pytest

    from elspeth.web.interpretation_state import validate_pipeline_decision_semantics

    with _pytest.raises(ValueError, match="registered"):
        validate_pipeline_decision_semantics(
            node_id="reconcile",
            plugin="field_mapper",
            node_type="transform",
            options={},
            condition=None,
            routes=None,
            user_term="ab_reconciliation_retention",
            draft="Retain both variants in the reconciled row.",
            context="test",
            web_scrape_raw_fields=frozenset(),
        )


# --------------------------------------------------------------------------- #
# gate_condition_authored — the planner-authored gate-semantics review.
#
# The composer prompt doctrine instructs the planner to escalate a gate
# threshold, category literal, or route direction it CHOSE ITSELF (rather than
# carrying the user's stated value verbatim) as a pipeline_decision review. That
# instruction was inert until this term was registered: an unregistered term is
# rejected at validate_pipeline_decision_semantics and again at
# pipeline_decision_artifact_hash, so the doctrine routed the planner into an
# unresolvable card (elspeth-c2c35e52ae).
#
# A gate is a NODE TYPE, not a plugin (NodeSpec.plugin is None for gates), so
# this arm binds on node_type — the mechanism differs from web_scrape_http_identity,
# which binds on plugin.
# --------------------------------------------------------------------------- #


_DEFAULT_GATE_ROUTES: dict[str, str] = {"true": "accepted", "false": "rejected"}


def _gate(
    *,
    node_id: str = "score_gate",
    condition: str = "row.score >= 80",
    # Sentinel-free explicit default: ``routes=None`` is a REAL case under test
    # (a gate whose route mapping is absent), so it must not collapse into the
    # convenience default.
    routes: dict[str, str] | None = _DEFAULT_GATE_ROUTES,
    fork_to: tuple[str, ...] | None = None,
    options: dict[str, Any] | None = None,
) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="gate",
        plugin=None,
        input="inbound",
        on_success=None,
        on_error=None,
        options=options if options is not None else {},
        condition=condition,
        routes=routes,
        fork_to=fork_to,
        branches=None,
        policy=None,
        merge=None,
    )


def _gate_state(gate: NodeSpec) -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(gate,),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def test_gate_condition_authored_is_registered() -> None:
    """The escalation path the prompt doctrine names must be resolvable.

    Both the authoring-time validator and the resolve-time hash registry are
    closed sets keyed on this constant; membership is what makes an authored
    gate escalation stageable at all.
    """

    from elspeth.web.interpretation_state import (
        COMPOSER_AUTHORED_PIPELINE_DECISION_USER_TERMS,
        GATE_CONDITION_AUTHORED_USER_TERM,
        REGISTERED_PIPELINE_DECISION_USER_TERMS,
    )

    assert GATE_CONDITION_AUTHORED_USER_TERM in REGISTERED_PIPELINE_DECISION_USER_TERMS
    # The planner authors this row itself — it is not a server-staged disclosure.
    assert GATE_CONDITION_AUTHORED_USER_TERM in COMPOSER_AUTHORED_PIPELINE_DECISION_USER_TERMS


def test_registered_pipeline_decision_user_terms_is_the_exact_closed_set() -> None:
    """Pin the closed registry membership itself.

    Every member must have BOTH a validation arm and an artifact-hash helper.
    Adding a term without them mints review cards that can never be resolved,
    so growth of this set is a deliberate, reviewed act — not a side effect.
    """

    from elspeth.web.interpretation_state import REGISTERED_PIPELINE_DECISION_USER_TERMS

    assert (
        frozenset(
            {
                "drop_raw_html_fields",
                "gate_condition_authored",
                "prompt_injection_shield_recommendation",
                "required_control_auto_wired",
                "web_scrape_http_identity",
            }
        )
        == REGISTERED_PIPELINE_DECISION_USER_TERMS
    )


def test_gate_condition_authored_passes_semantics_on_a_gate_node() -> None:
    from elspeth.web.interpretation_state import (
        GATE_CONDITION_AUTHORED_USER_TERM,
        validate_pipeline_decision_node_semantics,
    )

    gate = _gate()
    validate_pipeline_decision_node_semantics(
        node=gate,
        all_nodes=(gate,),
        user_term=GATE_CONDITION_AUTHORED_USER_TERM,
        draft="I chose the 80 cutoff; you did not state one.",
        context="test",
    )


def test_gate_condition_authored_rejects_a_non_gate_node() -> None:
    """A registered term with no binding validates on ANY node.

    Without this arm the planner could stage the gate escalation on an llm or
    field_mapper node, pass set_pipeline, mint the card, and only then have
    pipeline_decision_artifact_hash raise at resolve time — displacing the
    wedge downstream instead of preventing it.
    """

    from elspeth.web.interpretation_state import (
        GATE_CONDITION_AUTHORED_USER_TERM,
        validate_pipeline_decision_node_semantics,
    )

    not_a_gate = NodeSpec(
        id="scorer",
        node_type="transform",
        plugin="llm",
        input="inbound",
        on_success="scored",
        on_error="errors",
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )

    with pytest.raises(ValueError, match="gate node"):
        validate_pipeline_decision_node_semantics(
            node=not_a_gate,
            all_nodes=(not_a_gate,),
            user_term=GATE_CONDITION_AUTHORED_USER_TERM,
            draft="I chose the 80 cutoff.",
            context="test",
        )


@pytest.mark.parametrize(
    ("condition", "routes"),
    [
        pytest.param("", {"true": "a", "false": "b"}, id="blank-condition"),
        pytest.param("   ", {"true": "a", "false": "b"}, id="whitespace-condition"),
        pytest.param("row.score >= 80", None, id="absent-routes"),
    ],
)
def test_gate_condition_authored_requires_reviewable_gate_semantics(condition: str, routes: dict[str, str] | None) -> None:
    """There must be something to review.

    A blank condition or absent route mapping pins nothing — the card would
    adjudicate an empty artifact.
    """

    from elspeth.web.interpretation_state import (
        GATE_CONDITION_AUTHORED_USER_TERM,
        validate_pipeline_decision_node_semantics,
    )

    gate = _gate(condition=condition, routes=routes)
    with pytest.raises(ValueError, match="gate"):
        validate_pipeline_decision_node_semantics(
            node=gate,
            all_nodes=(gate,),
            user_term=GATE_CONDITION_AUTHORED_USER_TERM,
            draft="I chose the cutoff.",
            context="test",
        )


def test_gate_condition_authored_artifact_hash_is_deterministic() -> None:
    from elspeth.web.interpretation_state import GATE_CONDITION_AUTHORED_USER_TERM

    gate = _gate()
    twin = _gate()
    assert pipeline_decision_artifact_hash(gate, (gate,), user_term=GATE_CONDITION_AUTHORED_USER_TERM) == pipeline_decision_artifact_hash(
        twin, (twin,), user_term=GATE_CONDITION_AUTHORED_USER_TERM
    )


def test_gate_condition_authored_artifact_hash_is_route_order_insensitive() -> None:
    """Route insertion order is not an adjudicated fact — the mapping is."""

    from elspeth.web.interpretation_state import GATE_CONDITION_AUTHORED_USER_TERM

    forward = _gate(routes={"true": "accepted", "false": "rejected"})
    reversed_order = _gate(routes={"false": "rejected", "true": "accepted"})
    assert pipeline_decision_artifact_hash(
        forward, (forward,), user_term=GATE_CONDITION_AUTHORED_USER_TERM
    ) == pipeline_decision_artifact_hash(reversed_order, (reversed_order,), user_term=GATE_CONDITION_AUTHORED_USER_TERM)


def test_gate_condition_authored_artifact_hash_tracks_the_condition() -> None:
    """Fabrication axis 1+2: threshold value and category literal.

    Silently re-cutting 80 to 70 after review must drift the accepted hash.
    """

    from elspeth.web.interpretation_state import GATE_CONDITION_AUTHORED_USER_TERM

    at_80 = _gate(condition="row.score >= 80")
    at_70 = _gate(condition="row.score >= 70")
    assert pipeline_decision_artifact_hash(at_80, (at_80,), user_term=GATE_CONDITION_AUTHORED_USER_TERM) != pipeline_decision_artifact_hash(
        at_70, (at_70,), user_term=GATE_CONDITION_AUTHORED_USER_TERM
    )


@pytest.mark.parametrize(
    "mutated_routes",
    [
        pytest.param({"true": "rejected", "false": "accepted"}, id="routes-inverted"),
        pytest.param({"true": "quarantine", "false": "rejected"}, id="true-destination-changed"),
        pytest.param({"true": "accepted", "false": "quarantine"}, id="false-destination-changed"),
    ],
)
def test_gate_condition_authored_artifact_hash_tracks_each_route_destination(mutated_routes: dict[str, str]) -> None:
    """Fabrication axis 3: route direction.

    EACH destination is material — an inversion is the exact failure the
    doctrine's "never invert stated routes" rule guards, so it must drift the
    hash, not just a change to the mapping's size.
    """

    from elspeth.web.interpretation_state import GATE_CONDITION_AUTHORED_USER_TERM

    baseline = _gate(routes={"true": "accepted", "false": "rejected"})
    mutated = _gate(routes=mutated_routes)
    assert pipeline_decision_artifact_hash(
        baseline, (baseline,), user_term=GATE_CONDITION_AUTHORED_USER_TERM
    ) != pipeline_decision_artifact_hash(mutated, (mutated,), user_term=GATE_CONDITION_AUTHORED_USER_TERM)


def test_gate_condition_authored_artifact_hash_tracks_fork_destinations() -> None:
    """A fork gate's route direction lives in fork_to, not routes."""

    from elspeth.web.interpretation_state import GATE_CONDITION_AUTHORED_USER_TERM

    forked = _gate(fork_to=("fast_path", "slow_path"))
    reforked = _gate(fork_to=("fast_path", "manual_path"))
    assert pipeline_decision_artifact_hash(
        forked, (forked,), user_term=GATE_CONDITION_AUTHORED_USER_TERM
    ) != pipeline_decision_artifact_hash(reforked, (reforked,), user_term=GATE_CONDITION_AUTHORED_USER_TERM)


def test_gate_condition_authored_artifact_hash_ignores_unrelated_option_edits() -> None:
    """Minimum-projection doctrine: unrelated edits must not re-stage the card."""

    from elspeth.web.interpretation_state import GATE_CONDITION_AUTHORED_USER_TERM

    plain = _gate(options={})
    annotated = _gate(options={"description": "score cutoff gate"})
    assert pipeline_decision_artifact_hash(plain, (plain,), user_term=GATE_CONDITION_AUTHORED_USER_TERM) == pipeline_decision_artifact_hash(
        annotated, (annotated,), user_term=GATE_CONDITION_AUTHORED_USER_TERM
    )


def test_staged_gate_pipeline_decision_survives_the_non_llm_kind_filter() -> None:
    """The whole point of the escalation: the card must actually surface.

    ``_pending_node_sites`` drops every pending kind except PIPELINE_DECISION on
    a non-llm node. A gate is non-llm, so this is the one kind that reaches the
    review surface there — and the review requirement must appear in
    ``interpretation_sites`` for /validate and /execute to block on it.
    """

    from elspeth.web.interpretation_state import GATE_CONDITION_AUTHORED_USER_TERM

    gate = _gate(
        options={
            INTERPRETATION_REQUIREMENTS_KEY: [
                {
                    "id": "score_gate:gate_condition_authored",
                    "kind": InterpretationKind.PIPELINE_DECISION.value,
                    "user_term": GATE_CONDITION_AUTHORED_USER_TERM,
                    "status": "pending",
                    "draft": "I chose the 80 cutoff; you did not state one.",
                    "event_id": None,
                    "accepted_value": None,
                    "accepted_artifact_hash": None,
                    "resolved_prompt_template_hash": None,
                }
            ]
        }
    )

    sites = interpretation_sites(_gate_state(gate))

    assert len(sites) == 1
    assert sites[0].component_id == "score_gate"
    assert sites[0].component_type == "transform"
    assert sites[0].user_term == GATE_CONDITION_AUTHORED_USER_TERM
    assert sites[0].kind is InterpretationKind.PIPELINE_DECISION


def test_resolved_gate_pipeline_decision_round_trips_through_materialization() -> None:
    """A resolved gate review must survive the execution drift guard.

    ``_validate_pipeline_decision_review`` re-runs BOTH the semantics arm and
    the hash arm on the resolved row, so a resolved gate escalation exercises
    the full write-then-read path the session service uses.
    """

    from elspeth.web.interpretation_state import GATE_CONDITION_AUTHORED_USER_TERM

    bare = _gate()
    accepted_hash = pipeline_decision_artifact_hash(bare, (bare,), user_term=GATE_CONDITION_AUTHORED_USER_TERM)
    resolved = _gate(
        options={
            INTERPRETATION_REQUIREMENTS_KEY: [
                {
                    "id": "score_gate:gate_condition_authored",
                    "kind": InterpretationKind.PIPELINE_DECISION.value,
                    "user_term": GATE_CONDITION_AUTHORED_USER_TERM,
                    "status": "resolved",
                    "draft": "I chose the 80 cutoff; you did not state one.",
                    "event_id": "evt-1",
                    "accepted_value": "Approved: 80 is the right cutoff.",
                    "accepted_artifact_hash": accepted_hash,
                    "resolved_prompt_template_hash": None,
                }
            ]
        }
    )

    assert interpretation_sites(_gate_state(resolved)) == ()
    # Materialization must not report the gate as blocking. Authoring options are
    # stripped later by ``strip_authoring_options`` at config lowering, not here,
    # so this asserts only that the resolved review clears the execution gate.
    assert not isinstance(materialize_state_for_execution(_gate_state(resolved)), InterpretationReviewPending)

    # Re-cutting the threshold after acceptance must NOT silently execute.
    drifted = _gate_state(replace(resolved, condition="row.score >= 70"))
    with pytest.raises(ValueError, match="drifted"):
        materialize_state_for_execution(drifted)


def test_gate_condition_authored_artifact_hash_survives_the_persistence_bridge() -> None:
    """The write side and the read side must agree across serialization.

    ``gate_condition_authored`` is the FIRST registered term whose projection
    reads NodeSpec fields OUTSIDE ``options`` (condition / routes / fork_to).
    The session service recomputes this hash from a persisted state record via
    ``NodeSpec.from_dict``, so any of those fields dropped or re-typed by the
    to_dict/from_dict round trip would make an accepted review read as drifted
    on reload — a wedge that only appears after a session is saved and
    reopened, never in an in-memory test.

    ``fork_to`` is the sharp edge: it is a tuple in memory and a list on the
    wire, so the projection must normalise it.
    """

    from elspeth.web.interpretation_state import GATE_CONDITION_AUTHORED_USER_TERM

    gate = _gate(routes={"true": "accepted", "false": "rejected"}, fork_to=("fast_path", "slow_path"))
    in_memory = pipeline_decision_artifact_hash(gate, (gate,), user_term=GATE_CONDITION_AUTHORED_USER_TERM)

    # Exactly the bridge sessions/service uses: state.to_dict() -> NodeSpec.from_dict.
    round_tripped = NodeSpec.from_dict(dict(_gate_state(gate).to_dict()["nodes"][0]))
    reloaded = pipeline_decision_artifact_hash(round_tripped, (round_tripped,), user_term=GATE_CONDITION_AUTHORED_USER_TERM)

    assert reloaded == in_memory


# --- Direct trust-boundary characterization tests --------------------------
#
# These call a @trust_boundary-decorated function directly through its
# declared source_param and assert the exact raise its invariant promises.
# They exist to satisfy the elspeth-lints `trust_boundary.tests` honesty gate
# (TBE1-4): every raising boundary must carry a test_ref to a pytest node
# whose own body contains a raising assertion invoking the decorated symbol
# through source_param — a test that only exercises the boundary indirectly
# (e.g. through interpretation_sites()) does not satisfy it.


def test_source_authoring_metadata_rejects_non_string_modality() -> None:
    from elspeth.web.interpretation_state import _source_authoring_metadata

    options = {SOURCE_AUTHORING_KEY: {"modality": 123, "content_hash": "abc"}}
    with pytest.raises(TypeError, match=r"source_authoring\.modality must be a non-empty string"):
        _source_authoring_metadata(options=options)


def test_prompt_parts_rejects_non_list_value() -> None:
    from elspeth.web.interpretation_state import _prompt_parts

    options = {PROMPT_TEMPLATE_PARTS_KEY: "not-a-list"}
    with pytest.raises(TypeError, match="prompt_template_parts must be a list"):
        _prompt_parts(options=options)


def test_requirements_rejects_non_list_value() -> None:
    from elspeth.web.interpretation_state import _requirements

    options = {INTERPRETATION_REQUIREMENTS_KEY: "not-a-list"}
    with pytest.raises(TypeError, match="interpretation_requirements must be a list"):
        _requirements(options=options)


def test_coerce_requirement_rejects_non_string_id() -> None:
    from elspeth.web.interpretation_state import _coerce_requirement

    with pytest.raises(TypeError, match="interpretation requirement id must be a non-empty string"):
        _coerce_requirement(value={"id": 123, "user_term": "x", "status": "pending"})


def test_coerce_requirement_rejects_non_string_draft() -> None:
    """Regression: draft previously passed through _coerce_requirement unchecked.

    InterpretationRequirement.draft is typed str | None, but the constructor
    read it straight from the input mapping with no validation — unlike every
    other field on the TypedDict. A non-string draft silently violated the
    declared type with no error anywhere. Closed alongside event_id below.
    """
    from elspeth.web.interpretation_state import _coerce_requirement

    with pytest.raises(TypeError, match="interpretation requirement draft must be a string or None"):
        _coerce_requirement(value={"id": "req-1", "user_term": "x", "status": "pending", "draft": 123})


def test_coerce_requirement_rejects_non_string_event_id() -> None:
    from elspeth.web.interpretation_state import _coerce_requirement

    with pytest.raises(TypeError, match="interpretation requirement event_id must be a string or None"):
        _coerce_requirement(value={"id": "req-1", "user_term": "x", "status": "pending", "event_id": 123})


def test_coerce_requirement_rejects_resolved_without_string_accepted_value() -> None:
    """The control that lets _render_prompt_parts read accepted_value nominally.

    `_render_prompt_parts` reaches `requirement["accepted_value"]` only on the
    non-pending arm, and `InterpretationRequirement.accepted_value` is typed
    `str | None`. This boundary is what guarantees the resolved case carries a
    real string, so the renderer needs no runtime type interrogation of its own.
    """
    from elspeth.web.interpretation_state import _coerce_requirement

    with pytest.raises(TypeError, match="resolved interpretation requirement must carry accepted_value"):
        _coerce_requirement(value={"id": "req-1", "user_term": "x", "status": "resolved", "accepted_value": 123})


def test_render_prompt_parts_substitutes_resolved_accepted_value() -> None:
    """A resolved interpretation_ref renders its accepted text into the prompt.

    Pins the resolved-ref arm of `_render_prompt_parts`, which previously
    re-checked `isinstance(accepted, str)` even though every requirement
    reaching it is built by `_coerce_requirement` (see the test above). The
    guard is now `accepted is None`, exactly equivalent over the declared
    `str | None`, and this asserts the substitution itself still happens.
    """
    from elspeth.web.interpretation_state import (
        _prompt_parts,
        _render_prompt_parts,
        _requirements_by_id,
    )

    options: dict[str, object] = {
        PROMPT_TEMPLATE_PARTS_KEY: [
            {"kind": "text", "text": "Rate "},
            {"kind": "interpretation_ref", "requirement_id": "coolness"},
            {"kind": "text", "text": ": {{ row.text }}"},
        ],
        INTERPRETATION_REQUIREMENTS_KEY: [
            {
                "id": "coolness",
                "kind": "vague_term",
                "user_term": "coolness",
                "status": "resolved",
                "draft": "well-designed and useful",
                "event_id": "event-1",
                "accepted_value": "well-designed and useful",
                "accepted_artifact_hash": None,
                "resolved_prompt_template_hash": None,
            }
        ],
    }

    parts = _prompt_parts(options)
    assert parts is not None
    rendered = _render_prompt_parts(parts, _requirements_by_id(options), unresolved_text=None)

    assert rendered == "Rate well-designed and useful: {{ row.text }}"


def test_validate_pipeline_decision_semantics_rejects_malformed_http_mapping() -> None:
    from elspeth.web.interpretation_state import (
        WEB_SCRAPE_HTTP_IDENTITY_USER_TERM,
        validate_pipeline_decision_semantics,
    )

    with pytest.raises(ValueError, match=r"requires options\.http"):
        validate_pipeline_decision_semantics(
            node_id="scrape",
            plugin="web_scrape",
            node_type="transform",
            options={"http": "not-a-mapping"},
            condition=None,
            routes=None,
            user_term=WEB_SCRAPE_HTTP_IDENTITY_USER_TERM,
            draft=None,
            context="test",
            web_scrape_raw_fields=frozenset(),
        )


def test_web_scrape_http_identity_artifact_hash_rejects_malformed_http_mapping() -> None:
    from elspeth.web.interpretation_state import _web_scrape_http_identity_artifact_hash

    node = _web_scrape("scrape", input_stream="rows", on_success="inbound")
    node = replace(node, options={"http": "not-a-mapping"})

    with pytest.raises(ValueError, match=r"requires options\.http"):
        _web_scrape_http_identity_artifact_hash(node=node)


def test_raw_html_cleanup_artifact_hash_rejects_malformed_mapping_shape() -> None:
    """Regression: a present-but-malformed field_mapper.mapping used to be
    silently coerced to {} instead of raising, unlike the identically-shaped
    check in validate_pipeline_decision_semantics and the sibling
    _web_scrape_http_identity_artifact_hash boundary."""
    from elspeth.web.interpretation_state import _raw_html_cleanup_artifact_hash

    node = NodeSpec(
        id="cleanup",
        node_type="transform",
        plugin="field_mapper",
        input="scraped",
        on_success="output",
        on_error="stop",
        options={"mapping": "not-a-mapping", "select_only": True},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )

    with pytest.raises(ValueError, match=r"requires field_mapper\.mapping to be a mapping"):
        _raw_html_cleanup_artifact_hash(node, ())


@pytest.mark.parametrize("bad_field", [123, None, b"content", ["content"], {"content": 1}])
def test_validated_mapping_field_rejects_non_string_mapping_sides(bad_field: object) -> None:
    """Tier-3 boundary honesty: a non-string side of an authored
    field_mapper mapping raises ValueError, never coerces or stringifies."""
    from elspeth.web.interpretation_state import _validated_mapping_field

    with pytest.raises(ValueError, match="must map string field names"):
        _validated_mapping_field(bad_field, context="raw-html cleanup review contract", node_id="drop-raw")


# ---------------------------------------------------------------------------
# Multi-query prompt surface attestation (session 94f6f00c).
#
# The llm_prompt_template review used to anchor to the node-level
# prompt_template alone. On a multi-query node every query may override it, so
# the operator attested text the model never sees while the per-query
# templates and the shared system_prompt went unreviewed. The review now
# anchors to the whole prompt surface for that node shape; single-prompt
# anchors are byte-identical to before.
# ---------------------------------------------------------------------------


def _multi_query_queries() -> dict[str, dict[str, object]]:
    return {
        "good_pair": {
            "input_fields": {"colour": "colour"},
            "template": "What is a good colour pair for {{ row.colour }}? Reply with the single colour name only.",
        },
        "hex_code": {
            "input_fields": {"colour": "colour"},
            "template": "What is the approximate hex code of the colour {{ row.colour }}?",
        },
    }


def _multi_query_options(**overrides: object) -> dict[str, object]:
    options: dict[str, object] = {
        "prompt_template": "Answer the question about the colour {{ row.colour }} in one short reply.",
        "system_prompt": "Reply with only the value asked for and nothing else.",
        "queries": _multi_query_queries(),
        "required_input_fields": ["colour"],
    }
    options.update(overrides)
    return options


def test_single_prompt_review_anchor_is_the_skeleton_hash_or_none() -> None:
    """Single-prompt anchors: structured nodes anchor to the skeleton hash,
    unstructured no-parts nodes return None (callers use the text hash)."""
    from elspeth.web.interpretation_state import (
        prompt_review_anchor_hash_from_options,
        prompt_review_draft_from_options,
    )

    structured = _pending_options()
    assert prompt_review_anchor_hash_from_options(structured) == prompt_structure_hash_from_options(structured)
    unstructured = {"prompt_template": "Rate {{ row.text }}"}
    assert prompt_review_anchor_hash_from_options(unstructured) is None
    assert prompt_review_draft_from_options(unstructured) == "Rate {{ row.text }}"


def test_multi_query_review_anchor_covers_every_prompt_the_model_receives() -> None:
    from elspeth.web.interpretation_state import PROMPT_SURFACE_HASH_DOMAIN, prompt_review_anchor_hash_from_options

    base = prompt_review_anchor_hash_from_options(_multi_query_options())
    assert base is not None
    assert base != stable_hash(_multi_query_options()["prompt_template"])

    queries = _multi_query_queries()
    queries["good_pair"] = {**queries["good_pair"], "template": "Name a colour that pairs with {{ row.colour }}."}
    assert prompt_review_anchor_hash_from_options(_multi_query_options(queries=queries)) != base
    assert prompt_review_anchor_hash_from_options(_multi_query_options(system_prompt="Be terse.")) != base
    assert prompt_review_anchor_hash_from_options(_multi_query_options(prompt_template="Different dead prompt.")) != base
    # Order-insensitive to unrelated keys, deterministic across calls.
    assert prompt_review_anchor_hash_from_options(_multi_query_options(model="x")) == base
    assert PROMPT_SURFACE_HASH_DOMAIN == "llm_prompt_surface/v1"


def test_multi_query_review_draft_shows_the_live_prompts_and_marks_the_dead_slot() -> None:
    from elspeth.web.interpretation_state import prompt_review_draft_from_options

    draft = prompt_review_draft_from_options(_multi_query_options())
    assert draft is not None
    assert "System prompt (sent with every query):" in draft
    assert "Reply with only the value asked for and nothing else." in draft
    assert "Query 'good_pair':" in draft
    assert "What is a good colour pair for {{ row.colour }}?" in draft
    assert "Query 'hex_code':" in draft
    assert "Node-level prompt_template: not used (every query supplies its own template)." in draft
    # The dead text itself is not presented as the prompt under review.
    assert "Answer the question about the colour" not in draft

    queries = _multi_query_queries()
    queries["hex_code"] = {"input_fields": {"colour": "colour"}}
    partial = prompt_review_draft_from_options(_multi_query_options(queries=queries))
    assert partial is not None
    assert "Query 'hex_code': uses the node-level prompt_template (below)." in partial
    assert "used by queries without their own template: hex_code" in partial
    assert "Answer the question about the colour {{ row.colour }} in one short reply." in partial


def test_well_formed_multi_query_review_anchors_are_pinned() -> None:
    """Captured from the code BEFORE the invalid-template state was added
    (HEAD 818d04577 + round-2 working set, 2026-09-14): a well-formed surface's
    anchor must never move, or every resolved multi-query review reopens."""
    from elspeth.web.interpretation_state import prompt_review_anchor_hash_from_options

    assert (
        prompt_review_anchor_hash_from_options(_multi_query_options()) == "18a8f2c191fa96d7b5eca1f1afb6d79533be2d484b5bcc9e62f73e82d00ed014"
    )
    queries = {
        "good_pair": {
            "input_fields": {"colour": "colour"},
            "template": "What is a good colour pair for {{ row.colour }}? Reply with the single colour name only.",
        },
        "hex_code": {"input_fields": {"colour": "colour"}},
    }
    assert (
        prompt_review_anchor_hash_from_options(_multi_query_options(queries=queries))
        == "e50b5fc7d96ac7bc5bf8baaca8b9d7ee18c55b14c9bd26d0b2b48792622e9f80"
    )
    list_form = {
        "prompt_template": "Dead node prompt",
        "queries": [
            {"name": "one", "input_fields": {"a": "b"}, "template": "One {{ row.a }}"},
            {"name": "two", "input_fields": {"a": "b"}},
        ],
    }
    assert prompt_review_anchor_hash_from_options(list_form) == "4a07d052ce6efd9ef81ec3075b17eb861105935380c0070a7efbc75a3b6946f8"


@pytest.mark.parametrize("bad_template", [["not", "text"], 7, {"nested": "x"}])
def test_multi_query_review_draft_marks_a_non_string_query_template_invalid(bad_template: object) -> None:
    """A present non-string template is neither prose nor a node-level-template user."""
    from elspeth.web.interpretation_state import (
        InvalidQueryTemplate,
        multi_query_prompt_surface_from_options,
        prompt_review_anchor_hash_from_options,
    )

    good_pair = {
        "input_fields": {"colour": "colour"},
        "template": "What is a good colour pair for {{ row.colour }}? Reply with the single colour name only.",
    }
    queries = {"good_pair": good_pair, "hex_code": {"input_fields": {"colour": "colour"}, "template": bad_template}}
    options = _multi_query_options(queries=queries)
    surface = multi_query_prompt_surface_from_options(options)
    assert surface is not None
    assert surface.queries[1] == ("hex_code", InvalidQueryTemplate())
    assert surface.node_template_users == ()

    draft = surface.render_for_review()
    assert "Query 'hex_code': (template value is not text; plugin validation rejects this node)" in draft
    assert "Query 'hex_code': uses the node-level prompt_template" not in draft
    assert "used by queries without their own template" not in draft
    assert "Answer the question about the colour" not in draft
    assert "Node-level prompt_template: not used (no query falls back to it)." in draft

    # Collision control: the invalid query's anchor differs from both the
    # fallback (None) and a real-template reading of the same node.
    invalid_anchor = prompt_review_anchor_hash_from_options(options)
    fallback = dict(queries)
    fallback["hex_code"] = {"input_fields": {"colour": "colour"}}
    assert invalid_anchor != prompt_review_anchor_hash_from_options(_multi_query_options(queries=fallback))
    assert invalid_anchor != prompt_review_anchor_hash_from_options(_multi_query_options())


def test_multi_query_review_draft_invalid_template_beside_a_fallback_query() -> None:
    from elspeth.web.interpretation_state import multi_query_prompt_surface_from_options

    queries = {
        "good_pair": {"input_fields": {"colour": "colour"}},
        "hex_code": {"input_fields": {"colour": "colour"}, "template": 42},
    }
    surface = multi_query_prompt_surface_from_options(_multi_query_options(queries=queries))
    assert surface is not None
    assert surface.node_template_users == ("good_pair",)
    draft = surface.render_for_review()
    assert "Node-level prompt_template, used by queries without their own template: good_pair\n" in draft
    assert "Query 'hex_code': (template value is not text; plugin validation rejects this node)" in draft


def test_multi_query_review_draft_stays_under_the_wire_bound_and_says_what_it_cut() -> None:
    from elspeth.web.interpretation_state import PROMPT_SURFACE_REVIEW_MAX_CHARS, multi_query_prompt_surface_from_options

    queries = {f"q{n}": {"input_fields": {"colour": "colour"}, "template": f"Q{n} " + ("x" * 3000) + " {{ row.colour }}"} for n in range(6)}
    surface = multi_query_prompt_surface_from_options(_multi_query_options(queries=queries))
    assert surface is not None
    draft = surface.render_for_review()
    assert len(draft) <= PROMPT_SURFACE_REVIEW_MAX_CHARS
    assert "more chars not shown; the review attests the full text" in draft
    # The anchor is over the COMPLETE texts regardless of display bounding.
    assert surface.anchor_hash() == multi_query_prompt_surface_from_options(_multi_query_options(queries=queries)).anchor_hash()  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Review-draft bounding: termination, byte stability, and the wire bound.
#
# The per-text shortening loop never terminated once every text sat at its
# floor and the total still exceeded the budget: 25 queries of 250-char
# templates pinned a worker at 100% CPU on every planner write that staged the
# review. The floor is now terminal, and whole-entry omission keeps the draft
# under the ``llm_draft`` wire cap (8192; an over-long draft is stored and then
# fails ``InterpretationEventResponse`` on read) when shortening cannot.
# ---------------------------------------------------------------------------


def _surface_fixture_text(tag: str, length: int) -> str:
    body = "".join(chr(97 + (i * 7 + len(tag)) % 26) for i in range(length))
    return f"{tag} {body} {{{{ row.colour }}}}"


def _exact_length_template(tag: str, length: int) -> str:
    return (f"{tag} {{{{ row.colour }}}} " + "y" * length)[:length]


def _fixture_queries(count: int, length: int) -> dict[str, object]:
    return {f"q{n}": {"input_fields": {"colour": "colour"}, "template": _surface_fixture_text(f"Q{n}", length)} for n in range(count)}


def _exact_length_queries(count: int, length: int) -> dict[str, object]:
    return {f"q{n}": {"input_fields": {"colour": "colour"}, "template": _exact_length_template(f"Q{n}", length)} for n in range(count)}


def _byte_stability_options(name: str) -> dict[str, object]:
    if name == "base":
        return _multi_query_options()
    if name == "single_long":
        return _multi_query_options(
            queries={"only": {"input_fields": {"colour": "colour"}, "template": _surface_fixture_text("ONLY", 20000)}}
        )
    if name == "six_x_3000":
        return _multi_query_options(
            queries={
                f"q{n}": {"input_fields": {"colour": "colour"}, "template": f"Q{n} " + ("x" * 3000) + " {{ row.colour }}"} for n in range(6)
            }
        )
    if name == "mix":
        return _multi_query_options(
            system_prompt=_surface_fixture_text("SYS", 5000),
            prompt_template=_surface_fixture_text("NODE", 1500),
            queries={
                "alpha": {"input_fields": {"colour": "colour"}, "template": _surface_fixture_text("ALPHA", 400)},
                "beta": {"input_fields": {"colour": "colour"}, "template": _surface_fixture_text("BETA", 2500)},
                "gamma": {"input_fields": {"colour": "colour"}},
                "delta": {"input_fields": {"colour": "colour"}, "template": _surface_fixture_text("DELTA", 1200)},
            },
        )
    if name == "ten_x_1000":
        return _multi_query_options(queries=_fixture_queries(10, 1000))
    if name == "twenty_four_x_250":
        return _multi_query_options(queries=_exact_length_queries(24, 250))
    if name == "twenty_three_x_300":
        # Reaches the 200-char display floor (300 // 2 < 200) and still fits,
        # so the floor constant itself is pinned.
        return _multi_query_options(queries=_exact_length_queries(23, 300))
    raise AssertionError(f"unknown byte-stability fixture {name!r}")


# (len, sha256) of ``render_for_review()`` captured from the pre-fix code for
# inputs it terminated on and fitted under the bound. The draft is the review's
# idempotency key, so a moved byte re-stages every such review.
_PRE_FIX_REVIEW_DRAFTS: dict[str, tuple[int, str]] = {
    "base": (446, "ef7ad5702eed2184a6b1f894d5c7ea1fb13eddb7eb30923d732c81183cf60ae6"),
    "single_long": (5370, "afff75e694d255c141419d60690600c47e9e0ebf1f43daf2172e9818368da675"),
    "six_x_3000": (6879, "2b2291236c7907742cdad092cc431761f3892166bedc1e7743606b639f3a84a5"),
    "mix": (7351, "49998c97e1262561a9981b53be06bfae90465dff81374d62af535939c83fa130"),
    "ten_x_1000": (7009, "6ff7c7511a4a0b6cd2f0f012af74a5d100d13fc49bf2edd93f07f616b8799da4"),
    "twenty_four_x_250": (6603, "8a989c7bf760791f810f466e4cbb24bb0c8cf652596dc1f420e82ad95dd50c6c"),
    "twenty_three_x_300": (6652, "0ca8a997fbc0f3f9a55fedb7465b8b91adb3948d0ab22d1c2a4dee51c79c425a"),
}


@pytest.mark.parametrize("name", sorted(_PRE_FIX_REVIEW_DRAFTS))
def test_multi_query_review_draft_is_byte_identical_to_the_pre_fix_render_that_fit(name: str) -> None:
    from elspeth.web.interpretation_state import multi_query_prompt_surface_from_options

    surface = multi_query_prompt_surface_from_options(_byte_stability_options(name))
    assert surface is not None
    draft = surface.render_for_review()
    assert (len(draft), hashlib.sha256(draft.encode()).hexdigest()) == _PRE_FIX_REVIEW_DRAFTS[name]


def _limit_review_draft_shortening_steps(monkeypatch: pytest.MonkeyPatch, *, text_count: int) -> list[int]:
    """Fail the draft's shortening loop after a bounded number of steps instead of letting it spin.

    ``_bounded_texts`` pops one heap entry per step, and every step either
    strictly shortens a text (halving it, so at most 63 times for any real
    string) or exhausts it (once). ``64 * text_count`` pops therefore bounds a
    terminating loop, while a loop that stops exhausting a text at its floor
    pops the same entry forever. The counter turns that into an assertion
    failure after a finite number of steps: deterministic, no timer. The
    returned cell holds the pop count, so a test can show the counter saw the
    loop at all.
    """
    import heapq

    from elspeth.web import interpretation_state

    limit = 64 * text_count
    pops = [0]

    def counting_heappop(heap: list[tuple[int, int]]) -> tuple[int, int]:
        pops[0] += 1
        if pops[0] > limit:
            raise AssertionError(f"review-draft shortening did not terminate within {limit} steps")
        return heapq.heappop(heap)

    monkeypatch.setattr(
        interpretation_state,
        "heapq",
        SimpleNamespace(heapify=heapq.heapify, heappush=heapq.heappush, heappop=counting_heappop),
    )
    return pops


def test_multi_query_review_draft_terminates_when_every_template_sits_at_the_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web.interpretation_state import PROMPT_SURFACE_REVIEW_MAX_CHARS, multi_query_prompt_surface_from_options

    surface = multi_query_prompt_surface_from_options(_multi_query_options(queries=_exact_length_queries(25, 250)))
    assert surface is not None
    pops = _limit_review_draft_shortening_steps(monkeypatch, text_count=25 + 2)
    draft = surface.render_for_review()
    assert pops[0] > 0
    assert len(draft) <= PROMPT_SURFACE_REVIEW_MAX_CHARS
    # A 250-char template cannot shrink (its shortened form is longer), so it
    # is shown in full; the floor alone fits this surface, so nothing is omitted.
    assert "Query 'q24':\n" + _exact_length_template("Q24", 250) in draft
    assert "not shown in this draft" not in draft
    assert "not listed in this draft" not in draft


def test_multi_query_review_draft_omits_whole_templates_from_the_end_when_the_floor_cannot_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web.interpretation_state import PROMPT_SURFACE_REVIEW_MAX_CHARS, multi_query_prompt_surface_from_options

    queries = _exact_length_queries(60, 250)
    surface = multi_query_prompt_surface_from_options(_multi_query_options(queries=queries))
    assert surface is not None
    pops = _limit_review_draft_shortening_steps(monkeypatch, text_count=60 + 2)

    class _Counting(_ScanCountingQueries):
        scans = 0

    draft = replace(surface, queries=_Counting(surface.queries)).render_for_review()
    assert pops[0] > 0
    # Whole-entry omission takes one sizing step per omitted template; a scan of
    # ``queries`` inside a step makes the render quadratic.
    assert _Counting.scans <= 16
    assert len(draft) <= PROMPT_SURFACE_REVIEW_MAX_CHARS
    omitted = draft.count(" chars not shown; the review attests the full text)")
    assert 0 < omitted < 60
    assert f"Query templates not shown in this draft: {omitted} of 60; the review attests every template in full." in draft
    # From the END: the last query is a fact line, the first is shown in full.
    assert "Query 'q59': (template of 250 chars not shown; the review attests the full text)" in draft
    assert "Query 'q0':\n" + _exact_length_template("Q0", 250) in draft
    assert "not listed in this draft" not in draft
    # The anchor still covers an omitted template.
    changed = dict(queries)
    changed["q59"] = {"input_fields": {"colour": "colour"}, "template": "Changed {{ row.colour }}"}
    changed_surface = multi_query_prompt_surface_from_options(_multi_query_options(queries=changed))
    assert changed_surface is not None
    assert changed_surface.anchor_hash() != surface.anchor_hash()


def test_multi_query_review_draft_stops_listing_trailing_queries_when_fact_lines_cannot_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web.interpretation_state import PROMPT_SURFACE_REVIEW_MAX_CHARS, multi_query_prompt_surface_from_options

    long_name = "n" * 60
    queries = {f"{long_name}{n}": {"input_fields": {"colour": "colour"}} for n in range(400)}
    options = _multi_query_options(
        system_prompt=_surface_fixture_text("SYS", 20000),
        prompt_template=_surface_fixture_text("NODE", 20000),
        queries=queries,
    )
    surface = multi_query_prompt_surface_from_options(options)
    assert surface is not None
    pops = _limit_review_draft_shortening_steps(monkeypatch, text_count=400 + 2)

    class _Counting(_ScanCountingQueries):
        scans = 0

    draft = replace(surface, queries=_Counting(surface.queries)).render_for_review()
    assert pops[0] > 0
    # Stopping the listing takes one sizing step per trailing query; a scan of
    # ``queries`` inside a step makes the render quadratic.
    assert _Counting.scans <= 16
    assert len(draft) <= PROMPT_SURFACE_REVIEW_MAX_CHARS
    assert "Queries not listed in this draft: the last " in draft
    assert f"Query '{long_name}0': uses the node-level prompt_template (below)." in draft
    assert f"Query '{long_name}399'" not in draft
    assert "more not listed" in draft
    # The anchor still covers an unlisted query.
    renamed = dict(queries)
    renamed[f"{long_name}399x"] = renamed.pop(f"{long_name}399")
    renamed_surface = multi_query_prompt_surface_from_options(_multi_query_options(**{**options, "queries": renamed}))
    assert renamed_surface is not None
    assert renamed_surface.anchor_hash() != surface.anchor_hash()


def test_multi_query_review_draft_measures_the_render_not_the_frame_estimate() -> None:
    """A fallback query's name is printed twice (its own line and the users
    line) but the frame estimate reserves it once, so a long name can push a
    draft whose texts fit the budget over the bound."""
    from elspeth.web.interpretation_state import PROMPT_SURFACE_REVIEW_MAX_CHARS, multi_query_prompt_surface_from_options

    system_prompt = _surface_fixture_text("SYS", 2000)
    options = _multi_query_options(
        system_prompt=system_prompt,
        prompt_template=_surface_fixture_text("NODE", 2000),
        queries={"F" * 3000: {"input_fields": {"colour": "colour"}}},
    )
    surface = multi_query_prompt_surface_from_options(options)
    assert surface is not None
    draft = surface.render_for_review()
    assert len(draft) <= PROMPT_SURFACE_REVIEW_MAX_CHARS
    assert "System prompt (sent with every query):\n" + system_prompt + "\n" in draft
    assert "Queries not listed in this draft: the last 1 of 1;" in draft


class _ScanCountingQueries(tuple[tuple[str, Any], ...]):
    """A queries tuple that counts full iterations over itself.

    Every whole-surface property of ``MultiQueryPromptSurface`` (fallback
    users, templated count, invalid-template check) is one ``__iter__`` over
    ``queries``, so the count is the number of O(n) scans one render made.
    """

    scans: int = 0

    def __iter__(self) -> Iterator[tuple[str, Any]]:
        type(self).scans += 1
        return super().__iter__()


@pytest.mark.parametrize("count", [500, 5000])
def test_multi_query_review_draft_scans_the_queries_a_size_independent_number_of_times(count: int) -> None:
    """Whole-entry omission takes up to ``2 * len(queries)`` sizing steps, so any
    scan of ``queries`` inside a step makes the render quadratic: at 5000
    queries the invalid-template check ran 9910 times per render (1.6 s
    serially). The render may scan ``queries`` only a fixed number of times,
    whatever its size. Counted, not timed, so the guard is deterministic under
    load."""
    from elspeth.web.interpretation_state import PROMPT_SURFACE_REVIEW_MAX_CHARS, multi_query_prompt_surface_from_options

    surface = multi_query_prompt_surface_from_options(_multi_query_options(queries=_fixture_queries(count, 250)))
    assert surface is not None
    expected_draft = surface.render_for_review()
    # Both sizes reach the stage that stops listing trailing queries, so they
    # take the same code path and must make the same number of scans.
    assert "Queries not listed in this draft: the last " in expected_draft

    class _Counting(_ScanCountingQueries):
        scans = 0

    counted = replace(surface, queries=_Counting(surface.queries))
    draft = counted.render_for_review()

    assert draft == expected_draft
    assert len(draft) <= PROMPT_SURFACE_REVIEW_MAX_CHARS
    assert _Counting.scans <= 16


def _resolved_prompt_review(anchor: str, draft: str) -> dict[str, object]:
    return {
        "id": "prompt-template-review",
        "kind": "llm_prompt_template",
        "user_term": "llm_prompt_template:rate_coolness",
        "status": "resolved",
        "draft": draft,
        "event_id": "event-9",
        "accepted_value": draft,
        "accepted_artifact_hash": None,
        "resolved_prompt_template_hash": anchor,
    }


def test_multi_query_execution_guard_accepts_the_surface_anchor_and_keeps_the_runtime_hash() -> None:
    from elspeth.web.interpretation_state import prompt_review_anchor_hash_from_options, prompt_review_draft_from_options

    options = _multi_query_options()
    anchor = prompt_review_anchor_hash_from_options(options)
    assert anchor is not None
    options[INTERPRETATION_REQUIREMENTS_KEY] = [_resolved_prompt_review(anchor, prompt_review_draft_from_options(options) or "")]

    materialized = materialize_state_for_execution(_state_with_llm(options))

    assert isinstance(materialized, CompositionState)
    # The node-level runtime hash the plugin re-validates is still the text hash
    # of prompt_template (LLMConfig demands exactly that); only the REVIEW anchor widened.
    assert materialized.nodes[0].options["resolved_prompt_template_hash"] == stable_hash(options["prompt_template"])


def _node_template_anchored_multi_query_options(anchor_kind: str) -> dict[str, object]:
    """A multi-query node whose RESOLVED review is anchored to the node-level template
    alone: its text hash, or (for a structured template) its skeleton hash."""
    if anchor_kind == "text_hash":
        options = _multi_query_options()
        prompt_template = options["prompt_template"]
        assert isinstance(prompt_template, str)
        anchor = stable_hash(prompt_template)
    else:
        prompt_template = "Rate {{ row.colour }}"
        options = _multi_query_options(
            prompt_template=prompt_template,
            **{PROMPT_TEMPLATE_PARTS_KEY: [{"kind": "text", "text": prompt_template}]},
        )
        skeleton_anchor = prompt_structure_hash_from_options(options)
        assert skeleton_anchor is not None
        assert skeleton_anchor != stable_hash(prompt_template)
        anchor = skeleton_anchor
    options[INTERPRETATION_REQUIREMENTS_KEY] = [_resolved_prompt_review(anchor, prompt_template)]
    return options


@pytest.mark.parametrize("anchor_kind", ["text_hash", "skeleton_hash"])
def test_multi_query_review_anchored_to_the_node_level_template_is_drift(anchor_kind: str) -> None:
    """The node-level template's hash is not a multi-query node's anchor (the surface
    anchor is), so a review carrying it takes the path every other anchor mismatch
    takes: no review site, and the execution gate refuses it as drift."""
    state = _state_with_llm(_node_template_anchored_multi_query_options(anchor_kind))

    assert interpretation_sites(state) == ()
    with pytest.raises(ValueError, match="prompt-template review hash drifted"):
        materialize_state_for_execution(state)


def test_resolved_surface_anchored_multi_query_review_is_not_a_site() -> None:
    from elspeth.web.interpretation_state import prompt_review_anchor_hash_from_options, prompt_review_draft_from_options

    options = _multi_query_options()
    anchor = prompt_review_anchor_hash_from_options(options)
    assert anchor is not None
    options[INTERPRETATION_REQUIREMENTS_KEY] = [_resolved_prompt_review(anchor, prompt_review_draft_from_options(options) or "")]

    assert interpretation_sites(_state_with_llm(options)) == ()


def test_resolved_single_prompt_review_with_its_text_hash_anchor_is_not_a_site() -> None:
    """For an unstructured single-prompt node ``stable_hash(prompt_template)`` IS the
    anchor, so a review carrying it is no site."""
    options: dict[str, object] = {"prompt_template": "Answer the question about the colour {{ row.colour }} in one short reply."}
    options[INTERPRETATION_REQUIREMENTS_KEY] = [
        _resolved_prompt_review(stable_hash(options["prompt_template"]), str(options["prompt_template"]))
    ]

    assert interpretation_sites(_state_with_llm(options)) == ()


def test_multi_query_execution_guard_still_reports_a_genuine_drift() -> None:
    options = _multi_query_options()
    options[INTERPRETATION_REQUIREMENTS_KEY] = [_resolved_prompt_review("0" * 64, "whatever")]

    with pytest.raises(ValueError, match="prompt-template review hash drifted"):
        materialize_state_for_execution(_state_with_llm(options))


def _reconcile_multi_query(previous_options: dict[str, object], proposed_options: dict[str, object]) -> dict[str, object]:
    from elspeth.web.interpretation_state import reconcile_authoritative_reviews

    reconciled = reconcile_authoritative_reviews(_state_with_llm(previous_options), _state_with_llm(proposed_options))
    return dict(reconciled.nodes[0].options)


def _prompt_review_of(options: dict[str, object]) -> dict[str, object]:
    requirements = options[INTERPRETATION_REQUIREMENTS_KEY]
    assert isinstance(requirements, tuple)
    matches = [dict(entry) for entry in requirements if entry["kind"] == "llm_prompt_template"]
    assert len(matches) == 1
    return matches[0]


def test_reconcile_carries_a_multi_query_review_whose_surface_is_unchanged() -> None:
    from elspeth.web.interpretation_state import prompt_review_anchor_hash_from_options, prompt_review_draft_from_options

    previous = _multi_query_options()
    anchor = prompt_review_anchor_hash_from_options(previous)
    assert anchor is not None
    review = _resolved_prompt_review(anchor, prompt_review_draft_from_options(previous) or "")
    previous[INTERPRETATION_REQUIREMENTS_KEY] = [review]
    proposed = _multi_query_options(model="different-but-not-prompt")
    proposed[INTERPRETATION_REQUIREMENTS_KEY] = [dict(review)]

    carried = _prompt_review_of(_reconcile_multi_query(previous, proposed))

    assert carried["status"] == "resolved"
    assert carried["resolved_prompt_template_hash"] == anchor


def test_reconcile_reopens_a_multi_query_review_when_a_query_template_changes() -> None:
    from elspeth.web.interpretation_state import prompt_review_anchor_hash_from_options, prompt_review_draft_from_options

    previous = _multi_query_options()
    anchor = prompt_review_anchor_hash_from_options(previous)
    assert anchor is not None
    review = _resolved_prompt_review(anchor, prompt_review_draft_from_options(previous) or "")
    previous[INTERPRETATION_REQUIREMENTS_KEY] = [review]
    queries = _multi_query_queries()
    queries["good_pair"] = {**queries["good_pair"], "template": "Name a colour that pairs with {{ row.colour }}."}
    proposed = _multi_query_options(queries=queries)
    proposed[INTERPRETATION_REQUIREMENTS_KEY] = [dict(review)]

    reopened = _prompt_review_of(_reconcile_multi_query(previous, proposed))

    assert reopened["status"] == "pending"
    assert reopened["accepted_value"] is None


def test_reconcile_reopens_a_multi_query_review_when_only_the_node_level_prompt_changes() -> None:
    """The node-level template is part of the reviewed surface even when every query
    overrides it, so editing only that text reopens a review anchored to the surface."""
    from elspeth.web.interpretation_state import prompt_review_anchor_hash_from_options, prompt_review_draft_from_options

    previous = _multi_query_options()
    anchor = prompt_review_anchor_hash_from_options(previous)
    assert anchor is not None
    review = _resolved_prompt_review(anchor, prompt_review_draft_from_options(previous) or "")
    previous[INTERPRETATION_REQUIREMENTS_KEY] = [review]
    proposed = _multi_query_options(prompt_template="Answer briefly about the colour {{ row.colour }}.")
    proposed[INTERPRETATION_REQUIREMENTS_KEY] = [dict(review)]

    reopened = _prompt_review_of(_reconcile_multi_query(previous, proposed))

    assert reopened["status"] == "pending"
    assert reopened["event_id"] is None
    assert reopened["accepted_value"] is None
    assert reopened["resolved_prompt_template_hash"] is None


def test_reconcile_raises_on_a_multi_query_review_anchored_to_the_node_level_text_hash() -> None:
    """The node-level text hash is not a multi-query node's anchor, so reconcile treats
    a review carrying it exactly like any other mismatched anchor."""
    previous = _multi_query_options()
    text_hash_anchor = stable_hash(previous["prompt_template"])
    review = _resolved_prompt_review(text_hash_anchor, str(previous["prompt_template"]))
    previous[INTERPRETATION_REQUIREMENTS_KEY] = [review]
    proposed = _multi_query_options()
    proposed[INTERPRETATION_REQUIREMENTS_KEY] = [dict(review)]

    with pytest.raises(ValueError, match="hash drifted"):
        _reconcile_multi_query(previous, proposed)


def test_reconcile_still_raises_on_a_genuinely_drifted_multi_query_review() -> None:
    previous = _multi_query_options()
    review = _resolved_prompt_review("0" * 64, "whatever")
    previous[INTERPRETATION_REQUIREMENTS_KEY] = [review]
    proposed = _multi_query_options()
    proposed[INTERPRETATION_REQUIREMENTS_KEY] = [dict(review)]

    with pytest.raises(ValueError, match="hash drifted"):
        _reconcile_multi_query(previous, proposed)
