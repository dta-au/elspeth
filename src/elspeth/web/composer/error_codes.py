"""Closed registry of every ``error_code`` the Web Composer emits.

A tool response's ``validation.errors[].error_code`` is a server-authored,
closed identifier, never model or operator text. Redaction lets the field
through only for codes registered here, so a persisted rejection keeps *why*
it was rejected while any unregistered value is still summarized away.

This is a leaf module: it imports only L0 contracts and the plugin-policy
enum, so the producers, ``tools/generation.py`` and ``redaction.py`` can all
import it without a load-order cycle (``generation`` imports ``redaction`` at
load time).

Membership is pinned by
``tests/unit/web/composer/test_error_code_redaction.py``: every code a
producer passes as ``error_code`` (keyword, ``ValidationEntry`` positional, or
``"error_code"`` dict key) anywhere under ``web/`` is registered, apart from
the census's named exclusions (guided mode and the deployment acceptance
clients). That covers the producers outside ``web/composer`` whose codes reach
a tool response: plugin-policy findings (``validate_composition_state``) and
execution validation (``ToolResult.runtime_preflight``). The guidance
catalogue ``_VALIDATION_GUIDANCE_BY_CODE`` is a subset.
"""

from __future__ import annotations

from typing import Final, get_args

from elspeth.contracts.blobs_inline import BlobInlineValidationCategory
from elspeth.contracts.composer_audit import ToolArgumentErrorCategory
from elspeth.web.plugin_policy.models import PluginUnavailableReason

_EMITTED_VALIDATION_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "BLOB_QUOTA_EXCEEDED",
        "DISCOVERY_NO_GAIN",
        "TERMINAL_CALL_NOT_ALONE",
        "advisor_signoff_blocked",
        "aggregation_expected_output_count_mode_invalid",
        "aggregation_missing_on_error",
        "aggregation_missing_plugin",
        "aggregation_on_error_unknown_sink",
        "aggregation_on_success_dangling",
        "aggregation_output_mode_invalid",
        "aggregation_trigger_invalid",
        "argument_error",
        "batch_required_fields_invalid",
        "batch_transform_misplaced",
        "batch_value_field_not_numeric",
        "bound_region_aggregation_invalid",
        "bound_region_sink_inside",
        "canonical_schema",
        "coalesce_best_effort_requires_timeout",
        "coalesce_branch_alias_unreachable",
        "coalesce_branch_unreachable",
        "coalesce_branches_invalid",
        "coalesce_config_invalid",
        "coalesce_merge_invalid",
        "coalesce_merge_select_unsupported",
        "coalesce_missing_branches",
        "coalesce_on_success_must_be_sink",
        "coalesce_on_success_unknown_sink",
        "coalesce_policy_invalid",
        "coalesce_policy_quorum_unsupported",
        "coalesce_schema_mode_mixed",
        "coalesce_timeout_invalid",
        "coalesce_union_type_incompatible",
        "collector_config_invalid",
        "collector_has_on_error_invalid",
        "collector_has_trigger_invalid",
        "collector_missing_plugin",
        "collector_missing_scope",
        "collector_on_success_dangling",
        "collector_plugin_not_batch_aware",
        "collector_scope_policy_invalid",
        "connection_sink_name_overlap",
        "contract_config_invalid",
        "deferred_intent_claim",
        "diff_baseline_unavailable",
        "duplicate_connection_consumer",
        "duplicate_connection_producer",
        "duplicate_edge_id",
        "duplicate_node_id",
        "duplicate_output_name",
        "edge_field_type_incompatible",
        "edge_not_lowerable",
        "edge_route_conflict",
        "edge_route_mismatch",
        "edge_unknown_node",
        "failsink_chain",
        "failsink_ineligible_plugin",
        "failsink_self_reference",
        "failsink_unknown_output",
        "file_sink_write_policy_invalid",
        "fork_branch_declared_by_multiple_gates",
        "fork_branch_multiple_barriers",
        "fork_branch_no_destination",
        "fork_mixed_closure_invalid",
        "fork_multiple_closers_invalid",
        "fork_roster_mismatch",
        "gate_condition_ignores_stated_threshold",
        "gate_condition_invalid",
        "gate_config_invalid",
        "gate_duplicate_fork_branch",
        "gate_fork_route_without_fork_to",
        "gate_fork_to_empty",
        "gate_fork_to_without_fork_route",
        "gate_missing_condition",
        "gate_missing_routes",
        "gate_on_error_unknown_sink",
        "gate_route_labels_mismatch",
        "gate_route_target_unknown",
        "gate_routes_empty",
        "guided_amend_contract_violation",
        "guided_collector_opener_unresolved",
        "guided_correction_unchanged",
        "guided_delta_authority_violation",
        "guided_delta_duplicate_stable_id",
        "guided_delta_nonincident_route",
        "guided_delta_reviewed_failure_route_required",
        "guided_delta_unknown_reference",
        "guided_delta_unknown_stable_id",
        "guided_output_alias_collision",
        "guided_reviewed_name_shadowed",
        "guided_revision_unchanged",
        "guided_route_target_unknown",
        "interpretation_requirements_invalid",
        "interpretation_review_contract_unsatisfied",
        "interpretation_review_draft_malformed",
        "interpretation_review_orphaned",
        "interpretation_review_pending",
        "llm_system_prompt_missing",
        "llm_user_prompt_missing",
        "locked_input_extras",
        "no_sinks_configured",
        "no_source_configured",
        "node_id_collides_with_source_or_sink",
        "node_id_invalid",
        "node_input_not_reachable",
        "node_scope_fields_unsupported",
        "node_timeout_unsupported",
        "on_error_closer_out_of_region",
        "output_name_invalid",
        "passthrough_cannot_produce_declared_fields",
        "pipeline_collection_cap_exceeded",
        "pipeline_cycle",
        "pipeline_decision_unregistered",
        "plugin_options_invalid",
        "prompt_template_parts_required",
        "prompt_template_unbound_variables",
        "prompt_template_undeclared_row_fields",
        "proof_repair_exhausted",
        "proposal_missing_requested_transforms",
        "quarantine_unknown_output",
        "query_input_columns_undeclared",
        "query_template_unbound_row_fields",
        "queue_config_invalid",
        "queue_name_collision",
        "queue_no_consumer",
        "reserved_node_id",
        "review_reconciliation_failed",
        "reviewed_output_projection_conflict",
        "round_trip_unavailable",
        "row_union_branch_aggregation_invalid",
        "row_union_branch_alias_unreachable",
        "row_union_branch_invalid",
        "row_union_branch_not_downstream",
        "row_union_branch_unreachable",
        "row_union_branches_invalid",
        "row_union_config_invalid",
        "row_union_downstream_group_invalid",
        "row_union_input_mismatch",
        "row_union_name_invalid",
        "row_union_nested_fork_invalid",
        "row_union_on_success_dangling",
        "row_union_on_success_invalid",
        "row_union_on_success_must_be_connection",
        "row_union_schema_incompatible",
        "row_union_timeout_invalid",
        "runtime_preflight_not_run",
        "schema_contract_violation",
        "scope_name_duplicate",
        "scope_name_invalid",
        "scope_opener_duplicate",
        "scope_opener_not_multi_row",
        "scope_opener_unknown",
        "semantic_contract_violation",
        "sink_contract_violation",
        "sink_locked_extras",
        "source_name_invalid",
        "source_on_success_dangling",
        "splice_validation_failed",
        "structural_node_plugin_forbidden",
        "surface_projection_unavailable",
        "transform_contract_violation",
        "transform_declared_output_not_guaranteed",
        "transform_missing_on_error",
        "transform_missing_on_success",
        "transform_missing_plugin",
        "transform_on_error_unknown_sink",
        "transform_on_success_dangling",
        "transform_string_input_field_type_incompatible",
        "transform_unexpected_condition",
        "transform_unexpected_routes",
        "unknown_node_type",
        "vague_term_unwired",
        "validation_error",
        "web_scrape_http_identity_invalid",
    }
)

# Codes produced outside ``web/composer`` that reach a composer tool response.
_EMITTED_POLICY_AND_EXECUTION_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        # web/plugin_policy/validation.py findings, embedded as ValidationEntry
        # by validate_authored_composition_state.
        "aws_s3_endpoint_url_not_allowed",
        "profile_alias_used_as_bucket",
        "required_control_coverage",
        "required_control_unavailable",
        # web/execution validation, carried by ToolResult.runtime_preflight.
        "aws_s3_source_profile_required",
        "disallowed_secret_ref",
        "empty_pipeline",
        "fabricated_secret",
        "interpretation_review_drift",
        "llm_authored_inline_blob_content",
        "llm_base_url_not_allowed",
        "llm_tracing_not_allowed",
        "missing_secret_ref",
        "missing_sink",
        "missing_source",
        "state_shape_materialization",
        "unauthorized_secret_ref",
        "web_fetch_private_network_not_allowed",
        "web_fetch_resource_config_invalid",
        "web_fetch_resource_limit_exceeded",
        "web_scrape_private_network_not_allowed",
        # Bounded source-proof blockers (tools/generation.py
        # _BLOCKING_DIAGNOSTIC_CODES), merged into the authoritative preflight
        # as ValidationError codes by web/execution/service.py.
        "aggregation_numeric_value_field_type_mismatch_against_source_schema",
        "csv_duplicate_headers",
        "csv_fixed_schema_omits_observed_columns",
        "csv_source_blob_header_mismatch",
        "csv_source_field_resolution_error",
        "declared_input_type_mismatch_against_source_schema",
        "gate_expression_preview_memory_exhaustion",
        "gate_expression_type_mismatch_against_source_schema",
        "gate_expression_unbounded_string_amplification",
        "source_inspection_failed",
        "text_source_url_without_web_scrape",
        # Session-route validation (persisted runtime-preflight failure and
        # guided replay). Not tool responses; registered so the census covers
        # the whole web tree without a special case.
        "guided_composition_invalid",
        "runtime_preflight_failed",
    }
)

REGISTERED_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        *_EMITTED_VALIDATION_ERROR_CODES,
        *_EMITTED_POLICY_AND_EXECUTION_ERROR_CODES,
        # Inline-blob validation builds its code from the closed category.
        *(f"{category}_inline_blob_content" for category in get_args(BlobInlineValidationCategory)),
        # The closed ``ToolArgumentError.code`` values, forwarded as
        # ``error_code`` by planner and discovery argument feedback.
        "DISCOVERY_ONLY",
        "DUPLICATE_RESOLVED_INTERPRETATION",
        "RATE_CAP_PER_SESSION_DAY",
        "RATE_CAP_PER_TERM",
        "SCHEMA_VALIDATION",
        # Plugin-policy rejections carry the unavailability reason as their code.
        *(reason.value for reason in PluginUnavailableReason),
        # Argument-rejection categories are the pipeline planner's rejection
        # codes and the planner-facing argument-error code.
        *(category.value for category in ToolArgumentErrorCategory),
    }
)
