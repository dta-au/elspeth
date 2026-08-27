"""Dry-run validation using real engine code paths.

Calls the same functions as `elspeth run`: load_settings(),
instantiate_runtime_plugins(), build_runtime_graph(),
graph.validate(), assemble_and_validate_pipeline_config() (route targets),
graph.validate_edge_compatibility().

W18 fix: Only typed exceptions are caught. Bare except Exception is forbidden.
Unknown exception types propagate as 500 Internal Server Error, signalling
that this function needs updating — not that the error should be swallowed.

Settings loading uses load_settings_from_yaml_string() — the same in-memory
loader as the execution service. This ensures validation exercises the exact
same code path as execution, and resolved secrets never touch disk.

Route-target validation (issue elspeth-127de6865a) closes the parity gap
where the orchestrator's four pre-init validators
(validate_route_destinations, validate_transform_error_sinks,
validate_source_quarantine_destination, validate_sink_failsink_destinations)
were not reached by /validate, letting dangling references pass preflight
only to be rejected pre-token at /execute.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.secrets import WebSecretResolver
from elspeth.contracts.trust_boundary import observation_boundary
from elspeth.core.config import ElspethSettings, load_bounded_pipeline_yaml, load_settings_from_config_dict, load_settings_from_yaml_string
from elspeth.core.dag.graph import ExecutionGraph
from elspeth.core.dag.models import EdgeContractError
from elspeth.engine.orchestrator.preflight import assemble_and_validate_pipeline_config
from elspeth.plugins.infrastructure.runtime_factory import PluginBundle
from elspeth.web.composer.state import (
    CompositionState,
)
from elspeth.web.execution._validation_authoring import (
    _DEFAULT_PLUGIN_POLICY_SUGGESTION as _AUTHORING_DEFAULT_PLUGIN_POLICY_SUGGESTION,
)
from elspeth.web.execution._validation_authoring import (
    _collect_secret_refs as _collect_secret_refs,
)
from elspeth.web.execution._validation_authoring import (
    lower_plugin_policy,
    review_interpretations,
    validate_batch_options,
    validate_path_policy,
    validate_secret_evidence,
    validate_semantic_evidence,
    validate_web_network_policy,
    validate_web_resource_policy,
)
from elspeth.web.execution._validation_diagnostics import (
    _build_edge_contract_suggestion_with_resolver,
    _edge_patch_target_for_node_id,
    _find_gate_fan_out_advisories,
    _find_identity_node_advisories,
    _find_static_llm_prompt_advisories,
    _format_edge_contract_message,
    _graph_warning_to_validation_warning,
    _reframe_settings_missing_parts,
)
from elspeth.web.execution._validation_diagnostics import (
    _infer_component_type_from_plugin_error as _infer_component_type_from_plugin_error,
)
from elspeth.web.execution._validation_ledger import ValidationLedger
from elspeth.web.execution._validation_materialization import (
    materialize_validation_yaml,
    validate_aws_s3_endpoint_url_policy,
    validate_aws_s3_source_policy,
    validate_llm_base_url_policy,
    validate_llm_retry_budget_policy,
    validate_llm_tracing_policy,
    validate_managed_identity_policy,
)
from elspeth.web.execution._validation_model import PhaseFailure, PhaseReport, _blocked_readiness
from elspeth.web.execution._validation_pipeline import ValidationDependencies, ValidationPipeline
from elspeth.web.execution._validation_runtime import (
    _GraphBuilder,
    build_gate_fan_out_advisory_checks,
    build_identity_advisory_checks,
    build_static_llm_prompt_advisory_checks,
    load_runtime_settings,
    validate_graph_structure,
    validate_route_targets,
    validate_runtime_plugins,
    validate_schema_compatibility,
    validate_value_source_compliance,
)
from elspeth.web.execution.preflight import (
    RUNTIME_CHECK_GRAPH_STRUCTURE,
    RUNTIME_CHECK_PLUGIN_INSTANTIATION,
    RUNTIME_CHECK_SCHEMA_COMPATIBILITY,
    RUNTIME_GRAPH_VALIDATION_CHECKS,
    _audit_safe_plugin_configs,
    _profiled_plugin_ids,
    build_runtime_graph,
    instantiate_runtime_plugins,
    resolve_runtime_yaml_paths,
)
from elspeth.web.execution.protocol import ValidationSettings, YamlGenerator
from elspeth.web.execution.schemas import (
    CHECK_OUTCOME_SKIPPED_AFTER_FAILURE,
    CHECK_SETTINGS,
    CHECK_VALUE_SOURCE_COMPLIANCE,
    VALIDATION_BLOCKING_CHECK_NAMES,
    ValidationCheck,
    ValidationError,
    ValidationReadiness,
    ValidationResult,
)
from elspeth.web.interpretation_state import (
    InterpretationReviewPending,
    materialize_state_for_authoring,
    materialize_state_for_execution,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginSnapshotAuthority
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry
from elspeth.web.secrets.wiring_policy import SecretWiringPolicy

_CHECK_SETTINGS = CHECK_SETTINGS
_CHECK_PLUGINS = RUNTIME_CHECK_PLUGIN_INSTANTIATION
_CHECK_VALUE_SOURCE_COMPLIANCE = CHECK_VALUE_SOURCE_COMPLIANCE
_CHECK_GRAPH = RUNTIME_CHECK_GRAPH_STRUCTURE
_CHECK_SCHEMA = RUNTIME_CHECK_SCHEMA_COMPATIBILITY
# The local aliases must stay the canonical runtime-check tuple, in order: the
# names are positional in `RUNTIME_GRAPH_VALIDATION_CHECKS`, so a reordering is
# as much a drift as a substitution and neither shows up at the use sites.
#
# The message names BOTH SIDES rather than leaving a bare comparison
# (elspeth-37941f1731). Without it this read `assert A == B` and a failure said
# only that two tuples differed — not which alias moved, nor what it moved to,
# which is the whole of what the reader needs. Deliberately still an `assert`:
# the sibling constant in `preflight.py` is re-derived independently by
# `test_validation.py`, so unlike the two Tier-1 guards this is
# defence-in-depth, and converting it is a separate decision.
assert RUNTIME_GRAPH_VALIDATION_CHECKS == (_CHECK_PLUGINS, _CHECK_GRAPH, _CHECK_SCHEMA), (
    "RUNTIME_GRAPH_VALIDATION_CHECKS drifted from this module's aliases. "
    f"canonical={RUNTIME_GRAPH_VALIDATION_CHECKS!r}, "
    f"aliases=({_CHECK_PLUGINS!r}, {_CHECK_GRAPH!r}, {_CHECK_SCHEMA!r}); "
    f"differing positions: {[i for i, (a, b) in enumerate(zip(RUNTIME_GRAPH_VALIDATION_CHECKS, (_CHECK_PLUGINS, _CHECK_GRAPH, _CHECK_SCHEMA), strict=False)) if a != b]}. "
    "Realign the aliases with preflight.RUNTIME_GRAPH_VALIDATION_CHECKS, or update both together."
)


def _execution_ready() -> ValidationReadiness:
    return ValidationReadiness(
        authoring_valid=True,
        execution_ready=True,
        completion_ready=True,
        blockers=[],
    )


# _CHECK_VALUE_SOURCE_COMPLIANCE slots between _CHECK_PLUGINS (typed configs
# now exist) and _CHECK_GRAPH (so a hallucinated model fails before any DAG
# work). A focused regression pins this canonical position.
_ALL_CHECKS = list(VALIDATION_BLOCKING_CHECK_NAMES)

_DEFAULT_PLUGIN_POLICY_SUGGESTION = _AUTHORING_DEFAULT_PLUGIN_POLICY_SUGGESTION


def _apply_phase[T](ledger: ValidationLedger, outcome: PhaseReport[T] | PhaseFailure) -> T:
    """Apply one typed outcome; failures terminate through ``PhaseTermination``."""
    return outcome.apply(ledger)


def _build_edge_contract_suggestion(
    exc: EdgeContractError,
    *,
    state: CompositionState | None = None,
    graph: ExecutionGraph | None = None,
) -> str:
    """Compatibility wrapper preserving the facade's live patch target."""
    return _build_edge_contract_suggestion_with_resolver(
        exc,
        state=state,
        graph=graph,
        resolve_target=_edge_patch_target_for_node_id,
    )


def _format_edge_contract_failure(
    exc: EdgeContractError,
    *,
    state: CompositionState | None = None,
    graph: ExecutionGraph | None = None,
) -> tuple[str, str]:
    """Preserve the live patch seam and the LLM-actionable diagnostic contract.

    Messages ground both ends by node ID. Suggestions expose concrete patch
    tool calls and lead with the consumer-side repair before the narrower
    producer-side alternative.
    """
    return (
        _format_edge_contract_message(exc, state=state, graph=graph),
        _build_edge_contract_suggestion(exc, state=state, graph=graph),
    )


def _skipped_checks(from_check: str, *, already_emitted: frozenset[str] = frozenset()) -> list[ValidationCheck]:
    """Generate skipped check records for all checks after from_check."""
    skipping = False
    result: list[ValidationCheck] = []
    for name in _ALL_CHECKS:
        if name == from_check:
            skipping = True
            continue
        if skipping and name not in already_emitted:
            result.append(
                ValidationCheck(
                    name=name,
                    passed=False,
                    detail=f"Skipped: {from_check} failed",
                    affected_nodes=(),
                    outcome_code=CHECK_OUTCOME_SKIPPED_AFTER_FAILURE,
                )
            )
    return result


def _identity_state_for_compiled_ids(authored_state: CompositionState) -> CompositionState:
    """Select the state whose bytes feed compiled node identity.

    Strict-first regardless of the caller's tolerant/strict lane: the run path
    hashes the strictly materialized authored options, and hashing those same
    bytes from both preflight lanes is what keeps ids stable across the
    tolerant/strict seam (elspeth-ba01834a57 seam B — the tolerant materializer
    masks placeholders and omits ``resolved_prompt_template_hash``, so
    lane-local materialization would mint a different id per lane). Only when
    strict materialization is impossible on the tolerant lane does identity
    fall back to the authoring-masked state: such a state cannot execute yet,
    so there is no run id to match, and the fallback keeps tolerant recompiles
    stable with each other.
    """
    try:
        strict_state = materialize_state_for_execution(authored_state)
    except ValueError:
        # Resolved-but-drifted review evidence the strict materializer refuses;
        # the tolerant lane must keep validating the draft rather than 500.
        return materialize_state_for_authoring(authored_state)
    # Nominal discrimination of an ELSPETH-owned closed result union
    # (the same shape review_interpretations discriminates).
    if isinstance(strict_state, InterpretationReviewPending):
        return materialize_state_for_authoring(authored_state)
    return strict_state


@dataclass(frozen=True, slots=True)
class _CompiledIdentityDocument:
    """Audit-safe authored pipeline document that feeds compiled node identity.

    Owned nominal wrapper (ADR-032: type what ELSPETH owns): the loaded YAML
    mapping exists solely to be handed to ``_audit_safe_plugin_configs`` as its
    ``audit_safe_settings``; naming that contract here keeps the identity
    document from travelling as an anonymous ``dict[str, Any]``.
    """

    config: Mapping[str, Any]


def _compiled_identity_settings(
    authored_state: CompositionState,
    *,
    yaml_generator: YamlGenerator,
    data_dir: Path,
    session_id: str | None,
) -> _CompiledIdentityDocument:
    """Mirror the execution service's audit-safe settings for node identity.

    ``build_validated_runtime_graph`` mints compiled node ids for profiled
    plugins from the AUTHORED options — ``_audit_safe_plugin_configs`` swaps
    ``plugin.config`` back for the duration of the build — while this module's
    dry-run used to hash the profile-lowered, secret-resolved config, so
    preflight and run minted different ids for the same node on every compile
    (elspeth-ba01834a57, seam A). This helper reproduces the service's exact
    audit-safe construction (strict materialize -> generate_yaml -> resolve
    runtime paths -> bounded load) so the preflight build can run under the
    same swap and mint run-identical ids.

    ``generate_yaml`` is deliberately unguarded, mirroring the execution
    service's call on this same authored state: every ``PipelineLoweringError``
    site is a spec-shape defect independent of option payloads, and the
    caller only reaches this helper after ``materialize_validation_yaml``
    generated the same spec shapes successfully — a raise here is a
    yaml-generator bug that must surface (W18), not a state to tolerate.
    """
    identity_yaml = yaml_generator.generate_yaml(_identity_state_for_compiled_ids(authored_state))
    identity_yaml = resolve_runtime_yaml_paths(identity_yaml, str(data_dir), session_id=session_id)
    loaded = load_bounded_pipeline_yaml(identity_yaml)
    if type(loaded) is not dict:
        raise TypeError(f"generate_yaml() produced non-dict YAML (got {type(loaded).__name__}) — this is a bug in the YAML generator")
    return _CompiledIdentityDocument(config=cast(dict[str, Any], loaded))


@observation_boundary(
    tier=3,
    source="composer-authored CompositionState (pipeline nodes/options) re-read at dry-run validation",
    source_param="state",
    suppresses=("R1",),
    invariant=(
        "converts expected user/config validation failures into a ValidationResult and returns it; never raises "
        "on the shape of state (Tier-1 invariant crashes on the analyzer's own generated YAML still propagate)"
    ),
)
def validate_pipeline(
    state: CompositionState,
    settings: ValidationSettings,
    yaml_generator: YamlGenerator,
    *,
    plugin_snapshot: PluginAvailabilitySnapshot,
    profile_registry: OperatorProfileRegistry | None,
    catalog: CatalogService,
    secret_service: WebSecretResolver | None = None,
    secret_wiring_policy: SecretWiringPolicy | None = None,
    user_id: str | None = None,
    blob_get_metadata: Callable[[UUID], BlobRecord | None] | None = None,
    allow_pending_interpretation_placeholders: bool = False,
    session_id: str | None = None,
) -> ValidationResult:
    """Compatibility facade that captures patchable runtime dependencies per call."""
    dependencies = ValidationDependencies(
        load_yaml=load_bounded_pipeline_yaml,
        load_settings_yaml=load_settings_from_yaml_string,
        load_settings_dict=load_settings_from_config_dict,
        instantiate_plugins=instantiate_runtime_plugins,
        build_graph=build_runtime_graph,
        validate_routes=assemble_and_validate_pipeline_config,
    )
    return ValidationPipeline(dependencies, run_impl=_validate_pipeline_impl).run(
        state,
        settings,
        yaml_generator,
        plugin_snapshot=plugin_snapshot,
        profile_registry=profile_registry,
        catalog=catalog,
        secret_service=secret_service,
        secret_wiring_policy=secret_wiring_policy,
        user_id=user_id,
        blob_get_metadata=blob_get_metadata,
        allow_pending_interpretation_placeholders=allow_pending_interpretation_placeholders,
        session_id=session_id,
    )


def _validate_pipeline_impl(
    state: CompositionState,
    settings: ValidationSettings,
    yaml_generator: YamlGenerator,
    *,
    plugin_snapshot: PluginAvailabilitySnapshot,
    profile_registry: OperatorProfileRegistry | None,
    catalog: CatalogService,
    secret_service: WebSecretResolver | None = None,
    secret_wiring_policy: SecretWiringPolicy | None = None,
    user_id: str | None = None,
    blob_get_metadata: Callable[[UUID], BlobRecord | None] | None = None,
    allow_pending_interpretation_placeholders: bool = False,
    session_id: str | None = None,
    dependencies: ValidationDependencies,
) -> ValidationResult:
    """Dry-run validation through the real engine code path.

    ``session_id`` scopes the sink-path allowlist to the caller's own
    ``blobs/<session>/`` subtree (elspeth-bdc17cfdb1). ``None`` fails
    closed: blob-targeted sink paths are rejected outright.

    Steps:
    1. Source path allowlist check (C3/S2 defense-in-depth)
    1b. Secret ref validation (all referenced secrets exist)
    2. Generate YAML from CompositionState
    3. Load settings via load_settings_from_yaml_string() — resolve secret
       refs first if present, matching the execution service path exactly
    4. instantiate_runtime_plugins(settings, plugin_snapshot=plugin_snapshot)
    5. build_runtime_graph(settings, bundle)
    6. graph.validate() + graph.validate_edge_compatibility()

    Catches and converts to structured ``ValidationResult(is_valid=False)``:
    ``PydanticValidationError``, ``ValueError``, ``TypeError`` (settings load),
    ``PluginNotFoundError``, ``PluginConfigError`` (plugin instantiation),
    ``FileExistsError`` (file-sink path collision under ``fail_if_exists`` /
    exhausted ``auto_increment`` — Tier 3 fs-boundary condition),
    ``GraphValidationError`` (structural), ``RouteValidationError`` (route
    target resolution). All other exceptions propagate (W18) — those are
    Tier 1 invariant breaks that must surface as a 500 to the composer
    failure-handling path.

    Args:
        state: CompositionState from the session.
        settings: ValidationSettings — exposes data_dir for path resolution and allowlist check.
        yaml_generator: YamlGenerator module/object with generate_yaml() method.
        secret_service: Optional secret resolver for validating secret refs.
        user_id: User ID for scoped secret resolution (required if secret_service is set).
        blob_get_metadata: Optional sync metadata lookup for validate-time
            inline-content blob checks. Runtime content reads stay in the
            execution preflight; validate checks metadata only.
        allow_pending_interpretation_placeholders: When true, composer
            authoring preflight masks unresolved ``{{interpretation:<term>}}``
            tokens before YAML generation. Runtime execution leaves this false.
        plugin_snapshot: Frozen request policy/availability snapshot. Local
            trained-operator callers may omit it; web callers must pass one.
        profile_registry: Frozen operator-profile resolver registry. Required
            when the snapshot exposes operator-profiled plugin aliases.
    """
    # Empty compositions have a deliberate legacy producer shape rather than
    # entering the ordered core ledger: no authored phase can make them
    # executable, and engine settings diagnostics would leak internal names.
    #
    # This is the ONE result exempt from the complete-failure-ledger
    # invariant every other path satisfies (a full VALIDATION_BLOCKING_CHECK
    # sequence with a single failure and a skipped tail): the 18 checks that
    # canonically precede settings_load never ran here, and recording them
    # as passed would fabricate evidence. Consumers indexing by canonical
    # rank must tolerate this shape; pinned by
    # test_empty_pipeline_is_the_only_partial_ledger_shape.
    if not state.sources and not state.nodes and not state.outputs:
        return ValidationResult(
            is_valid=False,
            checks=[
                ValidationCheck(
                    name=_CHECK_SETTINGS,
                    passed=False,
                    detail="Pipeline has no source, transforms, or outputs.",
                    affected_nodes=(),
                    outcome_code=None,
                ),
                *_skipped_checks(_CHECK_SETTINGS),
            ],
            errors=[
                ValidationError(
                    component_id=None,
                    component_type=None,
                    message="Pipeline is empty. Add a data source and an output step to begin building.",
                    suggestion="Pick a data source like a CSV file or text input, and an output like CSV or JSON, then validate again.",
                    error_code="empty_pipeline",
                ),
            ],
            readiness=_blocked_readiness(code="empty_pipeline", detail="Pipeline is empty."),
        )

    ledger = ValidationLedger()

    policy = _apply_phase(
        ledger,
        lower_plugin_policy(
            state,
            plugin_snapshot=plugin_snapshot,
            profile_registry=profile_registry,
            catalog=catalog,
        ),
    )
    path_validated = _apply_phase(
        ledger,
        validate_path_policy(
            policy,
            data_dir=settings.data_dir,
            session_id=session_id,
        ),
    )
    network_validated = _apply_phase(
        ledger,
        validate_web_network_policy(path_validated, plugin_snapshot=plugin_snapshot),
    )
    resource_validated = _apply_phase(
        ledger,
        validate_web_resource_policy(network_validated, plugin_snapshot=plugin_snapshot),
    )
    secret_validated = _apply_phase(
        ledger,
        validate_secret_evidence(
            resource_validated,
            secret_service=secret_service,
            user_id=user_id,
            secret_wiring_policy=secret_wiring_policy,
        ),
    )
    semantic_validated = _apply_phase(
        ledger,
        validate_semantic_evidence(secret_validated),
    )
    batch_validated = _apply_phase(
        ledger,
        validate_batch_options(semantic_validated),
    )
    interpretation_validated = _apply_phase(
        ledger,
        review_interpretations(
            batch_validated,
            allow_pending_placeholders=allow_pending_interpretation_placeholders,
        ),
    )
    materialized = _apply_phase(
        ledger,
        materialize_validation_yaml(
            interpretation_validated,
            yaml_generator=yaml_generator,
            data_dir=settings.data_dir,
            session_id=session_id,
            blob_get_metadata=blob_get_metadata,
            load_yaml=dependencies.load_yaml,
        ),
    )
    managed_identity_validated = _apply_phase(
        ledger,
        validate_managed_identity_policy(materialized),
    )
    retry_budget_validated = _apply_phase(
        ledger,
        validate_llm_retry_budget_policy(managed_identity_validated),
    )
    base_url_validated = _apply_phase(
        ledger,
        validate_llm_base_url_policy(retry_budget_validated),
    )
    tracing_validated = _apply_phase(
        ledger,
        validate_llm_tracing_policy(base_url_validated),
    )
    endpoint_validated = _apply_phase(
        ledger,
        validate_aws_s3_endpoint_url_policy(
            tracing_validated,
            plugin_snapshot=plugin_snapshot,
        ),
    )
    provider_validated = _apply_phase(
        ledger,
        validate_aws_s3_source_policy(
            endpoint_validated,
            plugin_snapshot=plugin_snapshot,
        ),
    )
    loaded = _apply_phase(
        ledger,
        load_runtime_settings(
            provider_validated,
            secret_service=secret_service,
            user_id=user_id,
            load_yaml=dependencies.load_yaml,
            load_settings_yaml=dependencies.load_settings_yaml,
            load_settings_dict=dependencies.load_settings_dict,
            reframe_missing_parts=_reframe_settings_missing_parts,
        ),
    )
    instantiated = _apply_phase(
        ledger,
        validate_runtime_plugins(
            loaded,
            plugin_snapshot=plugin_snapshot,
            instantiate_plugins=dependencies.instantiate_plugins,
        ),
    )
    value_source_validated = _apply_phase(ledger, validate_value_source_compliance(instantiated))
    # Compiled node identity must match what the run path will mint
    # (elspeth-ba01834a57): the execution service builds its graph under
    # _audit_safe_plugin_configs, which hashes AUTHORED options for every
    # operator-profiled plugin, while this dry-run built bare and hashed the
    # lowered, secret-resolved config — a different id for the same node on
    # every compile. Reproduce the service's swap here so preflight-minted ids
    # join against persisted run/diagnostic ids. The validation itself still
    # runs against the lowered/materialized runtime config — only the identity
    # input changes. Trained-operator execution never builds with audit-safe
    # settings (service.py leaves audit_safe_config None in that mode), so its
    # preflight stays unwrapped to keep matching its own run path.
    build_graph: _GraphBuilder = dependencies.build_graph
    if not plugin_snapshot.is_trained_operator and _profiled_plugin_ids(plugin_snapshot):
        identity_settings = _compiled_identity_settings(
            value_source_validated.loaded.materialized.authored.policy.authored_state,
            yaml_generator=yaml_generator,
            data_dir=settings.data_dir,
            session_id=session_id,
        )

        def _build_graph_under_authored_identity(settings: ElspethSettings, bundle: PluginBundle) -> ExecutionGraph:
            with _audit_safe_plugin_configs(
                bundle,
                audit_safe_settings=identity_settings.config,
                plugin_snapshot=plugin_snapshot,
            ):
                return dependencies.build_graph(settings, bundle)

        build_graph = _build_graph_under_authored_identity
    graphed = _apply_phase(
        ledger,
        validate_graph_structure(
            value_source_validated,
            build_graph=build_graph,
            warning_to_validation_warning=_graph_warning_to_validation_warning,
            edge_patch_target_for_node_id=_edge_patch_target_for_node_id,
            format_edge_contract_failure=_format_edge_contract_failure,
        ),
    )
    routes_validated = _apply_phase(
        ledger,
        validate_route_targets(graphed, validate_routes=dependencies.validate_routes),
    )
    schema_validated = _apply_phase(
        ledger,
        validate_schema_compatibility(
            routes_validated,
            edge_patch_target_for_node_id=_edge_patch_target_for_node_id,
            format_edge_contract_failure=_format_edge_contract_failure,
        ),
    )
    for advisory in build_identity_advisory_checks(
        schema_validated,
        find_identity_node_advisories=_find_identity_node_advisories,
    ):
        ledger.record_advisory(advisory)
    for advisory in build_gate_fan_out_advisory_checks(
        schema_validated,
        find_gate_fan_out_advisories=_find_gate_fan_out_advisories,
    ):
        ledger.record_advisory(advisory)
    for advisory in build_static_llm_prompt_advisory_checks(
        schema_validated,
        find_static_llm_prompt_advisories=_find_static_llm_prompt_advisories,
    ):
        ledger.record_advisory(advisory)

    authored = schema_validated.instantiated.loaded.materialized.authored
    return ledger.finish_success(
        readiness=_execution_ready(),
        warnings=schema_validated.graph_warnings,
        semantic_contracts=authored.semantic_contracts,
    )


def validate_pipeline_for_trained_operator(
    state: CompositionState,
    settings: ValidationSettings,
    yaml_generator: YamlGenerator,
    *,
    plugin_snapshot: PluginAvailabilitySnapshot | None = None,
    profile_registry: OperatorProfileRegistry | None = None,
    catalog: CatalogService | None = None,
    **kwargs: Any,
) -> ValidationResult:
    """Explicit non-web validation root preserving CLI and local-tool neutrality.

    The three routing options are explicit keyword parameters rather than
    ``kwargs`` mining: at base this used ``kwargs.pop(key, default)`` (two
    active R9 findings), which a mid-refactor rewrite turned into
    membership ternaries — semantically identical but invisible to the R9
    detector. Naming the parameters removes the dict-mining pattern itself
    instead of laundering it.
    """
    from elspeth.web.dependencies import create_catalog_service

    if catalog is None:
        catalog = create_catalog_service()
    catalog, plugin_snapshot = _trained_operator_validation_context(catalog, plugin_snapshot)
    return validate_pipeline(
        state,
        settings,
        yaml_generator,
        plugin_snapshot=plugin_snapshot,
        profile_registry=profile_registry,
        catalog=catalog,
        **kwargs,
    )


if TYPE_CHECKING:
    from elspeth.web.catalog.protocol import CatalogService


def _trained_operator_validation_context(
    catalog: CatalogService,
    plugin_snapshot: PluginAvailabilitySnapshot | None,
) -> tuple[CatalogService, PluginAvailabilitySnapshot]:
    """Normalize a caller-supplied snapshot without mutating forwarded options."""
    if plugin_snapshot is None:
        return catalog, PluginAvailabilitySnapshot.for_trained_operator(catalog)
    if plugin_snapshot.is_trained_operator:
        return catalog, plugin_snapshot
    return catalog, PluginAvailabilitySnapshot.create(
        policy_hash=plugin_snapshot.policy_hash,
        principal_scope="local:trained-operator",
        available=plugin_snapshot.available,
        unavailable=plugin_snapshot.unavailable,
        selected=plugin_snapshot.selected,
        usable_profile_aliases=plugin_snapshot.usable_profile_aliases,
        selected_profile_aliases=plugin_snapshot.selected_profile_aliases,
        control_modes=plugin_snapshot.control_modes,
        binding_generation_fingerprint=plugin_snapshot.binding_generation_fingerprint,
        authority=PluginSnapshotAuthority.TRAINED_OPERATOR,
    )
