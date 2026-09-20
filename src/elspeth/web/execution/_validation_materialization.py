"""Materialization and provider-policy phases for execution validation.

Every function returns immutable evidence. The validation runner is the sole
owner of check ordering and ledger mutation.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, cast
from uuid import UUID

import yaml

from elspeth.contracts.blobs import AllowedMimeType, BlobNotFoundError, BlobRecord, BlobStateError
from elspeth.contracts.blobs_inline import BlobContentResolutionError, BlobInlineRef, BlobInlineValidationViolation
from elspeth.contracts.enums import CreationModality, is_llm_authored_creation_modality
from elspeth.contracts.errors import PipelineLoweringError
from elspeth.contracts.freeze import freeze_fields
from elspeth.contracts.plugin_capabilities import PluginCapability
from elspeth.core.blobs_inline import (
    BLOB_INLINE_AGGREGATE_BYTE_CAP,
    BLOB_INLINE_PER_REF_BYTE_CAP,
    _discover_blob_content_refs,
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
    ValidationCheck,
    ValidationError,
)
from elspeth.web.plugin_policy.coverage import node_has_capability, source_has_capability
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId
from elspeth.web.provider_config_policy import (
    web_aws_s3_endpoint_url_policy_error,
    web_aws_s3_source_policy_error,
    web_llm_base_url_policy_error,
    web_llm_retry_budget_policy_error,
    web_llm_tracing_policy_error,
)


@dataclass(frozen=True, slots=True)
class _LLMPolicyComponent:
    """One source or transform subject to component-neutral LLM policy."""

    component_id: str
    component_type: Literal["source", "transform"]
    label: str
    options: Mapping[str, Any]

    def __post_init__(self) -> None:
        # ``options`` arrives from ``CompositionState``, which already
        # deep-freezes source/node options in its own ``__post_init__``, so in
        # practice this is a cheap re-freeze of an already-immutable mapping.
        # It is stated here anyway because the component may be constructed
        # from any Mapping and both readers — ``web_llm_base_url_policy_error``
        # and ``web_llm_tracing_policy_error`` — are policy gates whose input
        # must not be mutable behind their back. No consumer compares this
        # field by identity.
        freeze_fields(self, "options")


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
            options=source.options,
        )
        for source_name, source in source_items
        if source_has_capability(source, PluginCapability.LLM)
    ]
    # Every LLM-capable plugin-bearing node, not ``node_type == "transform"``
    # alone (elspeth-df8082552d). Capability resolution supplies both the
    # plugin-bearing check and the LLM subject vocabulary (elspeth-c7626ae109),
    # so neither node kinds nor the builtin plugin name are restated here.
    #
    # This is LIVE, not a latent tidy-up: ``llm`` on an AGGREGATION node
    # validates with zero composer errors (the batch-aware constraint is
    # collector-only, in ``state.py``'s ``_collector_intrinsic_errors``), so
    # before this widening a web-authored aggregation could carry an
    # attacker-supplied ``base_url`` past the egress gate and be stopped only
    # at runtime, by an unrelated check, with a runtime-shaped error instead
    # of ``llm_base_url_not_allowed``.
    #
    # ``component_type`` deliberately stays ``"transform"``: it is a closed
    # wire Literal, and the readers below branch on it to choose between
    # "LLM nodes" and "LLM sources" phrasing. Widening THAT would change a
    # wire vocabulary and a message contract, which a coverage fix has no
    # business doing. The node kind rides on ``label``, which exists only to
    # be interpolated into the human-readable ``detail`` string.
    components.extend(
        _LLMPolicyComponent(
            component_id=node.id,
            component_type="transform",
            label=node.node_type.capitalize(),
            options=node.options,
        )
        for node in state.nodes
        if node_has_capability(node, PluginCapability.LLM)
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


_LLM_PROMPT_SURFACE_OPTION_ROOTS: Final[frozenset[str]] = frozenset({"prompt_template", "system_prompt", "model"})


def llm_prompt_surface_field(field_path: str) -> tuple[str, str] | None:
    """Return ``(node_name, option_path)`` when an inline-ref path is an ``llm`` prompt surface or model.

    The one definition of the domain every LLM-authored inline-blob refusal
    guards: ``wire_blob_inline_ref`` and the node-option authoring tools, the
    /validate materialization phase, and run admission. ``field_path`` is the
    canonical ``node:<name>.options.<key>[.<key>...]`` form
    ``_discover_blob_content_refs`` produces. The guarded options are
    ``prompt_template``, ``system_prompt`` and ``model`` (and anything beneath
    them), and ``queries`` as a whole value, a whole ``queries.<name>`` value,
    or that query's ``template``: every value that is, or would carry, a query
    template. Other options, including a query's ``input_fields``, are not
    guarded. Whether ``<name>`` is an ``llm`` node is the caller's decision,
    made from the representation it holds.
    """
    prefix, separator, option_path = field_path.partition(".options.")
    if separator == "" or not prefix.startswith("node:"):
        return None
    keys = option_path.split(".")
    root = keys[0]
    is_query_prompt = root == "queries" and (len(keys) <= 2 or keys[2] == "template")
    if root not in _LLM_PROMPT_SURFACE_OPTION_ROOTS and not is_query_prompt:
        return None
    return prefix.removeprefix("node:"), option_path


def is_llm_authored_prompt_surface_binding(
    field_path: str,
    *,
    llm_node_names: Collection[str],
    creation_modality: CreationModality,
) -> bool:
    """Whether binding a blob of ``creation_modality`` at ``field_path`` is refused.

    ADR-034 admits ``inline_content`` markers in prompt fields so a USER-uploaded
    prompt artifact can back an ``llm`` node. The ``llm_prompt_template`` and
    ``llm_model_choice`` reviews read those options as strings, and the run
    substitutes blob bytes afterwards, so an LLM-authored blob there would become
    the executed prompt or model with no operator review. Only user-verbatim blob
    content may stand in for that text.
    """
    surface = llm_prompt_surface_field(field_path)
    return surface is not None and surface[0] in llm_node_names and is_llm_authored_creation_modality(creation_modality)


def _llm_authored_prompt_surface_refs(
    refs: list[BlobInlineRef],
    records_by_blob_id: Mapping[UUID, BlobRecord],
    *,
    llm_node_names: Collection[str],
) -> list[BlobInlineRef]:
    """Inline refs that bind an LLM-authored blob into an ``llm`` prompt surface or model.

    Called only after the metadata validation found no violation, so every ref
    is well formed and its blob resolved: ``records_by_blob_id`` holds the
    record that validation read for each one, and no metadata is read again.
    """
    refused: list[BlobInlineRef] = []
    for ref in refs:
        record = records_by_blob_id[ref.blob_id]
        if is_llm_authored_prompt_surface_binding(
            ref.field_path,
            llm_node_names=llm_node_names,
            creation_modality=record.creation_modality,
        ):
            refused.append(ref)
    return refused


def _llm_authored_prompt_surface_error(ref: BlobInlineRef) -> ValidationError:
    return ValidationError(
        component_id=_blob_inline_component_id(ref.field_path),
        component_type=_blob_inline_component_type(ref.field_path),
        message=(
            f"Inline content blob reference at {ref.field_path} is llm_authored: blob {ref.blob_id} was written by "
            "the composer, and an llm node's prompt template, system prompt, query template or model admits only "
            "user-uploaded blob content, because the prompt-template and model-choice reviews cover those fields"
        ),
        suggestion=(
            "Write the text directly into the option with patch_node_options or upsert_node so its review is "
            "staged, or bind a user-uploaded blob instead."
        ),
        error_code="llm_authored_inline_blob_content",
    )


def materialize_validation_yaml(
    interpretation: InterpretationValidatedState,
    *,
    yaml_generator: YamlGenerator,
    data_dir: Path,
    session_id: str | None,
    blob_get_metadata: Callable[[UUID], BlobRecord | None] | None,
    load_yaml: Callable[[str], object],
    blob_get_content: Callable[[UUID], tuple[BlobRecord, bytes]] | None = None,
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

    if "blob_ref" in pipeline_yaml and "inline_content" in pipeline_yaml:
        loaded = load_yaml(pipeline_yaml)
        if type(loaded) is not dict:
            raise TypeError(f"generate_yaml() produced non-dict YAML (got {type(loaded).__name__}) — this is a bug in the YAML generator")
        config_dict = cast(dict[str, object], loaded)
        try:
            refs = _discover_blob_content_refs(config_dict)
        except BlobContentResolutionError as exc:
            malformed = [
                BlobInlineValidationViolation(category="malformed", field_path=field_path, detail=reason)
                for field_path, reason in exc.malformed
            ]
            detail = _blob_inline_validation_detail(malformed)
            return PhaseFailure(
                passed_checks=(),
                failed_check=ValidationCheck(
                    name=CHECK_BLOB_INLINE_REFS, passed=False, detail=detail, affected_nodes=(), outcome_code=None
                ),
                errors=tuple(_blob_inline_validation_error(violation) for violation in malformed),
                readiness=_blocked_readiness(code="blob_inline_refs", detail=detail),
                semantic_contracts=interpretation.authored.semantic_contracts,
            )
        if not refs:
            # The YAML substring check also matches literal prompt/table text.
            # Only discovered markers require authorized blob readers.
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
                        detail="No inline-content blob references found",
                        affected_nodes=(),
                        outcome_code=None,
                    ),
                ),
            )
        if blob_get_metadata is None:
            unavailable = [
                BlobInlineValidationViolation(
                    category="not_ready", field_path=ref.field_path, detail="authorized blob metadata read is unavailable"
                )
                for ref in refs
            ]
            detail = _blob_inline_validation_detail(unavailable)
            return PhaseFailure(
                passed_checks=(),
                failed_check=ValidationCheck(
                    name=CHECK_BLOB_INLINE_REFS, passed=False, detail=detail, affected_nodes=(), outcome_code=None
                ),
                errors=tuple(_blob_inline_validation_error(violation) for violation in unavailable),
                readiness=_blocked_readiness(code="blob_inline_refs", detail=detail),
                semantic_contracts=interpretation.authored.semantic_contracts,
            )
        # The modality refusal below reuses the records this validation reads,
        # so each ref's metadata is fetched once and both checks judge one record.
        resolved_records: dict[UUID, BlobRecord] = {}

        def _recorded_blob_metadata(blob_id: UUID) -> BlobRecord | None:
            if blob_id in resolved_records:
                return resolved_records[blob_id]
            record = blob_get_metadata(blob_id)
            if record is not None:
                resolved_records[blob_id] = record
            return record

        blob_violations = _validate_blob_content_refs_sync(
            _recorded_blob_metadata,
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
        # Readiness parity with run admission (InlineBlobPromptSurfaceAdmissionError):
        # the same predicate refuses an LLM-authored blob in an llm prompt surface
        # or model here, so /validate is not ready for exactly the pipelines whose
        # run would be refused after creation. The runtime YAML names each node by
        # its composer id (collectors, named by scope, cannot carry the llm plugin).
        llm_authored_refs = _llm_authored_prompt_surface_refs(
            refs,
            resolved_records,
            llm_node_names=frozenset(node.id for node in interpretation.materialized_state.nodes if node.plugin == "llm"),
        )
        if llm_authored_refs:
            detail = "; ".join(f"{ref.field_path}: llm_authored" for ref in llm_authored_refs)
            return PhaseFailure(
                passed_checks=(),
                failed_check=ValidationCheck(
                    name=CHECK_BLOB_INLINE_REFS,
                    passed=False,
                    detail=detail,
                    affected_nodes=(),
                    outcome_code=None,
                ),
                errors=tuple(_llm_authored_prompt_surface_error(ref) for ref in llm_authored_refs),
                readiness=_blocked_readiness(code="blob_inline_refs", detail=detail),
                semantic_contracts=interpretation.authored.semantic_contracts,
            )
        if blob_get_content is None:
            unavailable = [
                BlobInlineValidationViolation(
                    category="not_ready", field_path=ref.field_path, detail="authorized blob content read is unavailable"
                )
                for ref in refs
            ]
            detail = _blob_inline_validation_detail(unavailable)
            return PhaseFailure(
                passed_checks=(),
                failed_check=ValidationCheck(
                    name=CHECK_BLOB_INLINE_REFS, passed=False, detail=detail, affected_nodes=(), outcome_code=None
                ),
                errors=tuple(_blob_inline_validation_error(violation) for violation in unavailable),
                readiness=_blocked_readiness(code="blob_inline_refs", detail=detail),
                semantic_contracts=interpretation.authored.semantic_contracts,
            )
        fetched: dict[BlobInlineRef, bytes] = {}
        content_by_blob_id: dict[UUID, bytes] = {}
        content_failure_by_blob_id: dict[UUID, BlobInlineValidationViolation] = {}
        content_violations: list[BlobInlineValidationViolation] = []
        actual_total_bytes = 0
        for ref in refs:
            if ref.blob_id in content_failure_by_blob_id:
                previous_failure = content_failure_by_blob_id[ref.blob_id]
                content_violations.append(
                    BlobInlineValidationViolation(
                        category=previous_failure.category, field_path=ref.field_path, detail=previous_failure.detail
                    )
                )
                continue
            if ref.blob_id not in content_by_blob_id:
                try:
                    content_record, content = blob_get_content(ref.blob_id)
                except BlobNotFoundError:
                    failure = BlobInlineValidationViolation(category="missing", field_path=ref.field_path, detail="blob not found")
                    content_failure_by_blob_id[ref.blob_id] = failure
                    content_violations.append(failure)
                    continue
                except BlobStateError:
                    failure = BlobInlineValidationViolation(category="not_ready", field_path=ref.field_path, detail="blob is not ready")
                    content_failure_by_blob_id[ref.blob_id] = failure
                    content_violations.append(failure)
                    continue
                metadata_record = resolved_records[ref.blob_id]
                if (
                    content_record.id != metadata_record.id
                    or content_record.session_id != metadata_record.session_id
                    or content_record.status != "ready"
                    or content_record.content_hash != metadata_record.content_hash
                    or content_record.mime_type != metadata_record.mime_type
                    or content_record.size_bytes != metadata_record.size_bytes
                    or len(content) != metadata_record.size_bytes
                    or content_record.creation_modality != metadata_record.creation_modality
                ):
                    failure = BlobInlineValidationViolation(
                        category="not_ready", field_path=ref.field_path, detail="blob changed during validation"
                    )
                    content_failure_by_blob_id[ref.blob_id] = failure
                    content_violations.append(failure)
                    continue
                content_by_blob_id[ref.blob_id] = content
            content = content_by_blob_id[ref.blob_id]
            if len(content) > BLOB_INLINE_PER_REF_BYTE_CAP:
                content_violations.append(
                    BlobInlineValidationViolation(
                        category="oversized",
                        field_path=ref.field_path,
                        detail=f"{len(content)} bytes exceeds per-ref cap {BLOB_INLINE_PER_REF_BYTE_CAP}",
                    )
                )
            actual_total_bytes += len(content)
            fetched[ref] = content
        if actual_total_bytes > BLOB_INLINE_AGGREGATE_BYTE_CAP:
            content_violations.append(
                BlobInlineValidationViolation(
                    category="oversized",
                    field_path="(aggregate)",
                    detail=f"total resolved bytes {actual_total_bytes} exceeds aggregate cap {BLOB_INLINE_AGGREGATE_BYTE_CAP}",
                )
            )
        if content_violations:
            detail = _blob_inline_validation_detail(content_violations)
            return PhaseFailure(
                passed_checks=(),
                failed_check=ValidationCheck(
                    name=CHECK_BLOB_INLINE_REFS, passed=False, detail=detail, affected_nodes=(), outcome_code=None
                ),
                errors=tuple(_blob_inline_validation_error(violation) for violation in content_violations),
                readiness=_blocked_readiness(code="blob_inline_refs", detail=detail),
                semantic_contracts=interpretation.authored.semantic_contracts,
            )
        try:
            resolved_config = _substitute_blob_content_refs_for_validation(
                config_dict,
                fetched,
                refs=refs,
                blob_metadata={
                    record.id: (cast(AllowedMimeType, record.mime_type), len(content_by_blob_id[record.id]))
                    for record in resolved_records.values()
                },
            )
        except BlobContentResolutionError as exc:
            decode_violations = [
                BlobInlineValidationViolation(category="malformed", field_path=field_path, detail=f"cannot decode as {encoding}")
                for field_path, encoding in exc.undecodable
            ]
            detail = _blob_inline_validation_detail(decode_violations)
            return PhaseFailure(
                passed_checks=(),
                failed_check=ValidationCheck(
                    name=CHECK_BLOB_INLINE_REFS, passed=False, detail=detail, affected_nodes=(), outcome_code=None
                ),
                errors=tuple(_blob_inline_validation_error(violation) for violation in decode_violations),
                readiness=_blocked_readiness(code="blob_inline_refs", detail=detail),
                semantic_contracts=interpretation.authored.semantic_contracts,
            )
        check_detail = "All inline-content blob references and bytes are valid"
        pipeline_yaml = yaml.dump(resolved_config, default_flow_style=False)
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


def validate_llm_retry_budget_policy(materialized: MaterializedYaml) -> PhaseReport[MaterializedYaml] | PhaseFailure:
    """Reject unsafe sequential multi-query LLM retry budgets.

    Subject set is every LLM-capable plugin-bearing node, regardless of node
    kind (elspeth-df8082552d, elspeth-c7626ae109). Note this gate carries its
    OWN node walk — it is not fed by
    ``_llm_policy_components`` despite gating the same plugin, so widening
    that helper alone would have left this site behind.
    """
    for node in materialized.authored.policy.state.nodes:
        if not node_has_capability(node, PluginCapability.LLM):
            continue
        policy_error = web_llm_retry_budget_policy_error(node.options)
        if policy_error is None:
            continue
        return PhaseFailure(
            passed_checks=(),
            failed_check=ValidationCheck(
                name=CHECK_LLM_RETRY_BUDGET_POLICY,
                passed=False,
                detail=f"{node.node_type.capitalize()} '{node.id}' uses disallowed sequential multi-query LLM retry budget",
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
                detail=f"{node.node_type} {node.id} uses an unsafe sequential multi-query LLM retry budget",
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
        policy_error = web_llm_base_url_policy_error(component.options)
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
        policy_error = web_llm_tracing_policy_error(component.options)
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
]
