"""Materialization and provider-policy phases for execution validation.

Every function returns immutable evidence. The validation runner is the sole
owner of check ordering and ledger mutation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

import yaml

from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.blobs_inline import BlobInlineValidationViolation
from elspeth.contracts.errors import PipelineLoweringError
from elspeth.core.blobs_inline import (
    BLOB_INLINE_AGGREGATE_BYTE_CAP,
    BLOB_INLINE_PER_REF_BYTE_CAP,
    _substitute_blob_content_refs_for_validation,
    _validate_blob_content_refs_sync,
)
from elspeth.web.composer.state import CompositionState
from elspeth.web.execution._validation_model import (
    InterpretationValidatedState,
    MaterializedYaml,
    PhaseFailure,
    PhaseReport,
    _blocked_readiness,
    _snapshot_materialized_evidence,
)
from elspeth.web.execution.preflight import resolve_runtime_yaml_paths
from elspeth.web.execution.protocol import YamlGenerator
from elspeth.web.execution.schemas import (
    CHECK_AWS_S3_ENDPOINT_URL_POLICY,
    CHECK_AWS_S3_SOURCE_POLICY,
    CHECK_BLOB_INLINE_REFS,
    CHECK_LLM_BASE_URL_POLICY,
    CHECK_LLM_RETRY_BUDGET_POLICY,
    CHECK_LLM_TRACING_POLICY,
    CHECK_MANAGED_IDENTITY_POLICY,
    ValidationCheck,
    ValidationError,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId
from elspeth.web.provider_config_policy import (
    web_aws_s3_endpoint_url_policy_error,
    web_aws_s3_source_policy_error,
    web_llm_base_url_policy_error,
    web_llm_retry_budget_policy_error,
    web_llm_tracing_policy_error,
    web_rag_provider_config_policy_error,
)


@dataclass(frozen=True, slots=True)
class _LLMPolicyComponent:
    """One source or transform subject to component-neutral LLM policy."""

    component_id: str
    component_type: Literal["source", "transform"]
    label: str
    plugin: str | None
    options: Mapping[str, Any]


def _source_policy_component_id(source_name: object) -> str:
    """Return a stable, value-safe id for one possibly malformed source key."""
    if type(source_name) is not str:
        return "source:<invalid>"
    return "source" if source_name == "source" else f"source:{source_name}"


def _llm_policy_components(state: CompositionState) -> tuple[_LLMPolicyComponent, ...]:
    """Return source-first, stable components for shared LLM egress gates."""
    source_items = sorted(
        cast("Mapping[object, Any]", state.sources).items(),
        key=lambda item: (0, item[0]) if type(item[0]) is str else (1, ""),
    )
    components = [
        _LLMPolicyComponent(
            component_id=_source_policy_component_id(source_name),
            component_type="source",
            label="Source",
            plugin=source.plugin,
            options=source.options,
        )
        for source_name, source in source_items
    ]
    components.extend(
        _LLMPolicyComponent(
            component_id=node.id,
            component_type="transform",
            label="Transform",
            plugin=node.plugin,
            options=node.options,
        )
        for node in state.nodes
        if node.node_type == "transform"
    )
    return tuple(components)


def _blob_inline_component_id(field_path: str) -> str | None:
    if field_path == "(aggregate)":
        return None
    if field_path.startswith("source."):
        return "source"
    if field_path.startswith("node:"):
        component, _separator, _rest = field_path.partition(".")
        return component[len("node:") :]
    if field_path.startswith("output:"):
        component, _separator, _rest = field_path.partition(".")
        return component[len("output:") :]
    return field_path


def _blob_inline_component_type(field_path: str) -> str | None:
    if field_path == "(aggregate)":
        return None
    if field_path.startswith("source."):
        return "source"
    if field_path.startswith("node:"):
        return "transform"
    if field_path.startswith("output:"):
        return "sink"
    return None


def _blob_inline_validation_detail(violations: list[BlobInlineValidationViolation]) -> str:
    return "; ".join(f"{violation.field_path}: {violation.category}" for violation in violations)


def _blob_inline_validation_error(violation: BlobInlineValidationViolation) -> ValidationError:
    return ValidationError(
        component_id=_blob_inline_component_id(violation.field_path),
        component_type=_blob_inline_component_type(violation.field_path),
        message=f"Inline content blob reference at {violation.field_path} is {violation.category}: {violation.detail}",
        suggestion="Verify the blob exists, is ready, is under the configured size caps, and matches the pinned sha256.",
        error_code=f"{violation.category}_inline_blob_content",
    )


def materialize_validation_yaml(
    interpretation: InterpretationValidatedState,
    *,
    yaml_generator: YamlGenerator,
    data_dir: Path,
    session_id: str | None,
    blob_get_metadata: Callable[[UUID], BlobRecord | None] | None,
    load_yaml: Callable[[str], object],
) -> PhaseReport[MaterializedYaml] | PhaseFailure:
    """Generate the exact runtime YAML and validate inline-blob metadata.

    A state shape the generator refuses to lower (``PipelineLoweringError``) is an
    authoring defect reachable from any rehydrated session, so it becomes a red
    verdict here rather than a 500. Note the failure lands on
    ``CHECK_BLOB_INLINE_REFS``: the ledger requires a failure to occupy the next
    canonical core check, and materialization's canonical slot is that one — so
    the check NAME is positional, and ``error_code`` is what distinguishes a
    state-shape refusal from an actual blob problem.

    Only that typed error converts. ``resolve_runtime_yaml_paths`` and the
    non-dict check below stay outside the guard, and anything else the generator
    raises is a bug in code ELSPETH owns, which must keep escaping.
    """
    try:
        pipeline_yaml = yaml_generator.generate_yaml(interpretation.materialized_state)
    except PipelineLoweringError as exc:
        detail = f"Pipeline state cannot be materialized to runtime YAML: {exc}"
        return PhaseFailure(
            passed_checks=(),
            failed_check=ValidationCheck(
                name=CHECK_BLOB_INLINE_REFS,
                passed=False,
                detail=detail,
                affected_nodes=(),
                outcome_code=None,
            ),
            errors=(
                ValidationError(
                    component_id=None,
                    component_type=None,
                    message=detail,
                    suggestion="Repair the named node so the field is set — patch it with upsert_node/patch_node_options, or remove the node if it is no longer part of the pipeline.",
                    error_code="state_shape_materialization",
                ),
            ),
            readiness=_blocked_readiness(code="state_shape_materialization", detail=detail),
            semantic_contracts=interpretation.authored.semantic_contracts,
        )
    pipeline_yaml = resolve_runtime_yaml_paths(pipeline_yaml, str(data_dir), session_id=session_id)

    if blob_get_metadata is not None and "blob_ref" in pipeline_yaml and "inline_content" in pipeline_yaml:
        loaded = load_yaml(pipeline_yaml)
        if type(loaded) is not dict:
            raise TypeError(f"generate_yaml() produced non-dict YAML (got {type(loaded).__name__}) — this is a bug in the YAML generator")
        config_dict = cast(dict[str, object], loaded)
        blob_violations = _validate_blob_content_refs_sync(
            blob_get_metadata,
            config_dict,
            per_ref_byte_cap=BLOB_INLINE_PER_REF_BYTE_CAP,
            aggregate_byte_cap=BLOB_INLINE_AGGREGATE_BYTE_CAP,
        )
        if blob_violations:
            detail = _blob_inline_validation_detail(blob_violations)
            return PhaseFailure(
                passed_checks=(),
                failed_check=ValidationCheck(
                    name=CHECK_BLOB_INLINE_REFS,
                    passed=False,
                    detail=detail,
                    affected_nodes=(),
                    outcome_code=None,
                ),
                errors=tuple(_blob_inline_validation_error(violation) for violation in blob_violations),
                readiness=_blocked_readiness(code="blob_inline_refs", detail=detail),
                semantic_contracts=interpretation.authored.semantic_contracts,
            )
        check_detail = "All inline-content blob references are valid"
        pipeline_yaml = yaml.dump(_substitute_blob_content_refs_for_validation(config_dict), default_flow_style=False)
    elif blob_get_metadata is None:
        check_detail = "No blob metadata service — check skipped"
    else:
        check_detail = "No inline-content blob references found"

    return PhaseReport(
        artifact=MaterializedYaml(
            authored=interpretation.authored,
            materialized_state=interpretation.materialized_state,
            pipeline_yaml=pipeline_yaml,
        ),
        checks=(
            ValidationCheck(
                name=CHECK_BLOB_INLINE_REFS,
                passed=True,
                detail=check_detail,
                affected_nodes=(),
                outcome_code=None,
            ),
        ),
    )


def validate_managed_identity_policy(materialized: MaterializedYaml) -> PhaseReport[MaterializedYaml] | PhaseFailure:
    """Reject web-authored managed identity configuration."""
    for node in materialized.authored.policy.state.nodes:
        if node.node_type != "transform":
            continue
        policy_error = web_rag_provider_config_policy_error(node.options)
        if policy_error is None:
            continue
        return PhaseFailure(
            passed_checks=(),
            failed_check=ValidationCheck(
                name=CHECK_MANAGED_IDENTITY_POLICY,
                passed=False,
                detail=f"Transform '{node.id}' uses disallowed managed identity provider_config",
                affected_nodes=(node.id,),
                outcome_code=None,
            ),
            errors=(
                ValidationError(
                    component_id=node.id,
                    component_type="transform",
                    message=policy_error,
                    suggestion="Use api_key authentication or an operator-controlled named connector/allowlist.",
                    error_code=None,
                ),
            ),
            readiness=_blocked_readiness(
                code=CHECK_MANAGED_IDENTITY_POLICY,
                detail=f"transform {node.id} enables managed identity from web-authored provider_config",
                component_id=node.id,
                component_type="transform",
            ),
            semantic_contracts=materialized.authored.semantic_contracts,
        )
    return PhaseReport(
        artifact=_snapshot_materialized_evidence(materialized),
        checks=(
            ValidationCheck(
                name=CHECK_MANAGED_IDENTITY_POLICY,
                passed=True,
                detail="No web-authored managed identity provider_config",
                affected_nodes=(),
                outcome_code=None,
            ),
        ),
    )


def validate_llm_retry_budget_policy(materialized: MaterializedYaml) -> PhaseReport[MaterializedYaml] | PhaseFailure:
    """Reject unsafe sequential multi-query LLM retry budgets."""
    for node in materialized.authored.policy.state.nodes:
        if node.node_type != "transform":
            continue
        policy_error = web_llm_retry_budget_policy_error(node.plugin, node.options)
        if policy_error is None:
            continue
        return PhaseFailure(
            passed_checks=(),
            failed_check=ValidationCheck(
                name=CHECK_LLM_RETRY_BUDGET_POLICY,
                passed=False,
                detail=f"Transform '{node.id}' uses disallowed sequential multi-query LLM retry budget",
                affected_nodes=(node.id,),
                outcome_code=None,
            ),
            errors=(
                ValidationError(
                    component_id=node.id,
                    component_type="transform",
                    message=policy_error,
                    suggestion="Set max_capacity_retry_seconds to a small positive value or configure pool_size > 1 for pooled retry handling.",
                    error_code=None,
                ),
            ),
            readiness=_blocked_readiness(
                code=CHECK_LLM_RETRY_BUDGET_POLICY,
                detail=f"transform {node.id} uses an unsafe sequential multi-query LLM retry budget",
                component_id=node.id,
                component_type="transform",
            ),
            semantic_contracts=materialized.authored.semantic_contracts,
        )
    return PhaseReport(
        artifact=_snapshot_materialized_evidence(materialized),
        checks=(
            ValidationCheck(
                name=CHECK_LLM_RETRY_BUDGET_POLICY,
                passed=True,
                detail="No unsafe web-authored sequential multi-query LLM retry budget",
                affected_nodes=(),
                outcome_code=None,
            ),
        ),
    )


def validate_llm_base_url_policy(materialized: MaterializedYaml) -> PhaseReport[MaterializedYaml] | PhaseFailure:
    """Reject web-authored OpenRouter base URL overrides."""
    for component in _llm_policy_components(materialized.authored.policy.state):
        policy_error = web_llm_base_url_policy_error(component.plugin, component.options)
        if policy_error is None:
            continue
        policy_message = policy_error if component.component_type == "transform" else policy_error.replace("LLM nodes", "LLM sources")
        return PhaseFailure(
            passed_checks=(),
            failed_check=ValidationCheck(
                name=CHECK_LLM_BASE_URL_POLICY,
                passed=False,
                detail=f"{component.label} '{component.component_id}' overrides OpenRouter base_url in a web-authored pipeline",
                affected_nodes=(component.component_id,),
                outcome_code=None,
            ),
            errors=(
                ValidationError(
                    component_id=component.component_id,
                    component_type=component.component_type,
                    message=policy_message,
                    suggestion="Remove the base_url option to use the canonical OpenRouter endpoint.",
                    error_code="llm_base_url_not_allowed",
                ),
            ),
            readiness=_blocked_readiness(
                code=CHECK_LLM_BASE_URL_POLICY,
                detail=f"{component.component_type} {component.component_id} overrides OpenRouter base_url in a web-authored pipeline",
                component_id=component.component_id,
                component_type=component.component_type,
            ),
            semantic_contracts=materialized.authored.semantic_contracts,
        )
    return PhaseReport(
        artifact=_snapshot_materialized_evidence(materialized),
        checks=(
            ValidationCheck(
                name=CHECK_LLM_BASE_URL_POLICY,
                passed=True,
                detail="No web-authored OpenRouter base_url override",
                affected_nodes=(),
                outcome_code=None,
            ),
        ),
    )


def validate_llm_tracing_policy(materialized: MaterializedYaml) -> PhaseReport[MaterializedYaml] | PhaseFailure:
    """Reject author-controlled LLM tracing configuration."""
    for component in _llm_policy_components(materialized.authored.policy.state):
        policy_error = web_llm_tracing_policy_error(component.plugin, component.options)
        if policy_error is None:
            continue
        policy_message = policy_error if component.component_type == "transform" else policy_error.replace("LLM nodes", "LLM sources")
        return PhaseFailure(
            passed_checks=(),
            failed_check=ValidationCheck(
                name=CHECK_LLM_TRACING_POLICY,
                passed=False,
                detail=f"{component.label} '{component.component_id}' configures tracing in a web-authored pipeline",
                affected_nodes=(component.component_id,),
                outcome_code=None,
            ),
            errors=(
                ValidationError(
                    component_id=component.component_id,
                    component_type=component.component_type,
                    message=policy_message,
                    suggestion="Remove tracing; tracing destinations and credentials are operator-controlled.",
                    error_code="llm_tracing_not_allowed",
                ),
            ),
            readiness=_blocked_readiness(
                code=CHECK_LLM_TRACING_POLICY,
                detail=f"{component.component_type} {component.component_id} configures tracing in a web-authored pipeline",
                component_id=component.component_id,
                component_type=component.component_type,
            ),
            semantic_contracts=materialized.authored.semantic_contracts,
        )
    return PhaseReport(
        artifact=_snapshot_materialized_evidence(materialized),
        checks=(
            ValidationCheck(
                name=CHECK_LLM_TRACING_POLICY,
                passed=True,
                detail="No web-authored LLM tracing configuration",
                affected_nodes=(),
                outcome_code=None,
            ),
        ),
    )


def validate_aws_s3_endpoint_url_policy(
    materialized: MaterializedYaml,
    *,
    plugin_snapshot: PluginAvailabilitySnapshot,
) -> PhaseReport[MaterializedYaml] | PhaseFailure:
    """Reject author-controlled AWS S3 endpoints for web principals."""
    policy_state = materialized.authored.policy.state
    if not plugin_snapshot.is_trained_operator:
        for source_name, source in policy_state.sources.items():
            policy_error = web_aws_s3_endpoint_url_policy_error(source.plugin, source.options)
            if policy_error is None:
                continue
            source_component = "source" if source_name == "source" else f"source:{source_name}"
            return PhaseFailure(
                passed_checks=(),
                failed_check=ValidationCheck(
                    name=CHECK_AWS_S3_ENDPOINT_URL_POLICY,
                    passed=False,
                    detail=f"Source '{source_name}' sets aws_s3 endpoint_url in a web-authored pipeline",
                    affected_nodes=(source_component,),
                    outcome_code=None,
                ),
                errors=(
                    ValidationError(
                        component_id=source_component,
                        component_type="source",
                        message=policy_error,
                        suggestion="Remove endpoint_url and use operator-controlled AWS configuration.",
                        error_code="aws_s3_endpoint_url_not_allowed",
                    ),
                ),
                readiness=_blocked_readiness(
                    code=CHECK_AWS_S3_ENDPOINT_URL_POLICY,
                    detail=f"source {source_component} sets aws_s3 endpoint_url in a web-authored pipeline",
                    component_id=source_component,
                    component_type="source",
                ),
                semantic_contracts=materialized.authored.semantic_contracts,
            )

        for output in policy_state.outputs:
            policy_error = web_aws_s3_endpoint_url_policy_error(output.plugin, output.options)
            if policy_error is None:
                continue
            return PhaseFailure(
                passed_checks=(),
                failed_check=ValidationCheck(
                    name=CHECK_AWS_S3_ENDPOINT_URL_POLICY,
                    passed=False,
                    detail=f"Sink '{output.name}' sets aws_s3 endpoint_url in a web-authored pipeline",
                    affected_nodes=(output.name,),
                    outcome_code=None,
                ),
                errors=(
                    ValidationError(
                        component_id=output.name,
                        component_type="sink",
                        message=policy_error,
                        suggestion="Remove endpoint_url and use operator-controlled AWS configuration.",
                        error_code="aws_s3_endpoint_url_not_allowed",
                    ),
                ),
                readiness=_blocked_readiness(
                    code=CHECK_AWS_S3_ENDPOINT_URL_POLICY,
                    detail=f"sink {output.name} sets aws_s3 endpoint_url in a web-authored pipeline",
                    component_id=output.name,
                    component_type="sink",
                ),
                semantic_contracts=materialized.authored.semantic_contracts,
            )

    detail = (
        "No web-authored aws_s3 endpoint_url override"
        if not plugin_snapshot.is_trained_operator
        else "Local trained-operator validation is exempt from the web aws_s3 endpoint_url policy"
    )
    return PhaseReport(
        artifact=_snapshot_materialized_evidence(materialized),
        checks=(
            ValidationCheck(
                name=CHECK_AWS_S3_ENDPOINT_URL_POLICY,
                passed=True,
                detail=detail,
                affected_nodes=(),
                outcome_code=None,
            ),
        ),
    )


def validate_aws_s3_source_policy(
    materialized: MaterializedYaml,
    *,
    plugin_snapshot: PluginAvailabilitySnapshot,
) -> PhaseReport[MaterializedYaml] | PhaseFailure:
    """Require profile-derived authority for every Web-authored S3 source."""
    profiled_source = False
    if not plugin_snapshot.is_trained_operator:
        source_id = PluginId("source", "aws_s3")
        operator_profile_available = source_id in plugin_snapshot.available
        for source_name, source in materialized.authored.policy.state.sources.items():
            policy_error = web_aws_s3_source_policy_error(
                source.plugin,
                operator_profile_available=operator_profile_available,
            )
            if policy_error is None:
                profiled_source = profiled_source or source.plugin == "aws_s3"
                continue
            source_component = "source" if source_name == "source" else f"source:{source_name}"
            return PhaseFailure(
                passed_checks=(),
                failed_check=ValidationCheck(
                    name=CHECK_AWS_S3_SOURCE_POLICY,
                    passed=False,
                    detail=f"Source '{source_name}' uses aws_s3 in a web-authored pipeline",
                    affected_nodes=(source_component,),
                    outcome_code=None,
                ),
                errors=(
                    ValidationError(
                        component_id=source_component,
                        component_type="source",
                        message=policy_error,
                        suggestion="Ask an operator to configure an S3 source profile, or use batch/CLI for raw S3 options.",
                        error_code="aws_s3_source_profile_required",
                    ),
                ),
                readiness=_blocked_readiness(
                    code=CHECK_AWS_S3_SOURCE_POLICY,
                    detail=f"source {source_component} uses aws_s3 in a web-authored pipeline",
                    component_id=source_component,
                    component_type="source",
                ),
                semantic_contracts=materialized.authored.semantic_contracts,
            )

    detail = (
        (
            "Web-authored aws_s3 source is bound through an available operator profile"
            if profiled_source
            else "Web-authored pipeline does not use an aws_s3 source"
        )
        if not plugin_snapshot.is_trained_operator
        else "Local trained-operator validation is exempt from the web aws_s3 source policy"
    )
    return PhaseReport(
        artifact=_snapshot_materialized_evidence(materialized),
        checks=(
            ValidationCheck(
                name=CHECK_AWS_S3_SOURCE_POLICY,
                passed=True,
                detail=detail,
                affected_nodes=(),
                outcome_code=None,
            ),
        ),
    )


__all__ = [
    "materialize_validation_yaml",
    "validate_aws_s3_endpoint_url_policy",
    "validate_aws_s3_source_policy",
    "validate_llm_base_url_policy",
    "validate_llm_retry_budget_policy",
    "validate_llm_tracing_policy",
    "validate_managed_identity_policy",
]
