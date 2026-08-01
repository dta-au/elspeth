"""Authored-state phases for execution validation.

Every function returns immutable evidence.  The validation runner is the sole
owner of check ordering and ledger mutation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from elspeth.contracts.secrets import SecretRefPlacementViolation, SecretScope, WebSecretResolver
from elspeth.contracts.trust_boundary import observation_boundary
from elspeth.core.secrets import (
    collect_credential_field_violations,
    collect_disallowed_secret_ref_markers,
    parse_secret_ref_marker,
    secret_env_ref_name,
)
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer._semantic_validator import validate_semantic_contracts
from elspeth.web.composer.state import (
    CompositionState,
    _batch_aware_placement_error,
    _batch_aware_required_input_fields_error,
    _batch_distribution_profile_value_field_entries,
)
from elspeth.web.execution._semantic_helpers import assistance_suggestion_for, serialize_semantic_contracts
from elspeth.web.execution._validation_model import (
    AuthoredValidatedState,
    InterpretationValidatedState,
    PhaseFailure,
    PhaseReport,
    PolicyLoweredState,
)
from elspeth.web.execution.schemas import (
    CHECK_BATCH_TRANSFORM_OPTIONS,
    CHECK_INTERPRETATION_REVIEW,
    CHECK_OPERATOR_PROFILE_OPTIONS,
    CHECK_OUTCOME_SECRET_REFS_NO_REFS,
    CHECK_OUTCOME_SECRET_REFS_RESOLVED,
    CHECK_OUTCOME_SECRET_REFS_SKIPPED_NO_SERVICE,
    CHECK_OUTCOME_SECRET_REFS_UNRESOLVED,
    CHECK_PATH_ALLOWLIST,
    CHECK_PLUGIN_ENABLEMENT,
    CHECK_REQUIRED_CONTROL_AVAILABILITY,
    CHECK_REQUIRED_CONTROL_COVERAGE,
    CHECK_SECRET_REFS,
    CHECK_SEMANTIC_CONTRACTS,
    CHECK_WEB_FETCH_RESOURCE_POLICY,
    CHECK_WEB_SCRAPE_NETWORK_POLICY,
    ValidationCheck,
    ValidationCheckName,
    ValidationError,
    ValidationReadiness,
    ValidationReadinessBlocker,
)
from elspeth.web.interpretation_state import (
    INTERPRETATION_REVIEW_PENDING_CODE,
    InterpretationReviewPending,
    InterpretationReviewSite,
    materialize_state_for_authoring,
    materialize_state_for_execution,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry
from elspeth.web.plugin_policy.validation import PolicyValidationStage, validate_plugin_policy
from elspeth.web.secrets.ref_policy import allowed_secret_ref_fields, allowed_secret_ref_fields_text
from elspeth.web.secrets.service import ScopedSecretResolver

_PLUGIN_POLICY_CHECKS: tuple[tuple[PolicyValidationStage, ValidationCheckName], ...] = (
    ("plugin_enablement", CHECK_PLUGIN_ENABLEMENT),
    ("operator_profile_options", CHECK_OPERATOR_PROFILE_OPTIONS),
    ("required_control_availability", CHECK_REQUIRED_CONTROL_AVAILABILITY),
    ("required_control_coverage", CHECK_REQUIRED_CONTROL_COVERAGE),
)
_DEFAULT_PLUGIN_POLICY_SUGGESTION = "Choose an available plugin or repair the required control path, then validate again."

_WEB_FETCH_TRANSFORMS = frozenset({"blob_fetch", "web_scrape"})
_WEB_BLOB_FETCH_MAX_TIMEOUT_SECONDS = 30
_WEB_BLOB_FETCH_MAX_BODY_BYTES = 10 * 1024 * 1024
_WEB_BLOB_FETCH_INT_ADAPTER: TypeAdapter[int] = TypeAdapter(int)


@dataclass(frozen=True, slots=True)
class SecretValidatedState:
    """Policy-lowered state plus secret evidence collected at its boundary."""

    policy: PolicyLoweredState
    all_secret_refs: tuple[tuple[str, SecretScope | None], ...]
    env_ref_names: frozenset[str]


@dataclass(frozen=True, slots=True)
class _ParsedResourceLimit:
    value: int | None
    error: ValidationError | None

    def __post_init__(self) -> None:
        if (self.value is None) == (self.error is None):
            raise ValueError("_ParsedResourceLimit requires exactly one of value or error")


def _parse_resource_limit(
    raw_value: object,
    *,
    component_id: str,
    option_name: str,
    maximum: int,
) -> _ParsedResourceLimit:
    """Parse one Pydantic-derived web limit into an explicit typed outcome."""
    try:
        value = _WEB_BLOB_FETCH_INT_ADAPTER.validate_python(raw_value)
    except PydanticValidationError:
        return _ParsedResourceLimit(
            value=None,
            error=ValidationError(
                component_id=component_id,
                component_type="transform",
                message=f"blob_fetch.http.{option_name} must be an integer; got {type(raw_value).__name__}.",
                suggestion=f"Set blob_fetch.http.{option_name} to an integer from 0 to {maximum}.",
                error_code="web_fetch_resource_config_invalid",
            ),
        )
    return _ParsedResourceLimit(value=value, error=None)


def _blocked_readiness(
    *,
    code: str,
    detail: str,
    component_id: str | None = None,
    component_type: str | None = None,
    authoring_valid: bool = False,
    completion_ready: bool = False,
) -> ValidationReadiness:
    return ValidationReadiness(
        authoring_valid=authoring_valid,
        execution_ready=False,
        completion_ready=completion_ready,
        blockers=[
            ValidationReadinessBlocker(
                code=code,
                component_id=component_id,
                component_type=component_type,
                detail=detail,
            )
        ],
    )


def _path_value_failure(
    *,
    component_id: str,
    component_type: str,
    option_name: str,
    detail: str,
) -> PhaseFailure:
    """Return the canonical typed failure for one malformed authored path."""
    return PhaseFailure(
        passed_checks=(),
        failed_check=ValidationCheck(
            name=CHECK_PATH_ALLOWLIST,
            passed=False,
            detail=detail,
            affected_nodes=(),
            outcome_code=None,
        ),
        errors=(
            ValidationError(
                component_id=component_id,
                component_type=component_type,
                message=detail,
                suggestion=f"Set {option_name} to a valid path within this session's allowed directories.",
                error_code=None,
            ),
        ),
        readiness=_blocked_readiness(
            code="path_allowlist",
            detail=detail,
            component_id=component_id,
            component_type=component_type,
        ),
    )


@observation_boundary(
    tier=3,
    source="composer-authored nodes and options entering plugin-policy validation",
    source_param="state",
    suppresses=("R1", "R5"),
    invariant="returns typed policy evidence or a typed validation failure for unsupported authored values",
)
def lower_plugin_policy(
    state: CompositionState,
    *,
    plugin_snapshot: PluginAvailabilitySnapshot,
    profile_registry: OperatorProfileRegistry | None,
    catalog: CatalogService,
) -> PhaseReport[PolicyLoweredState] | PhaseFailure:
    """Run the four plugin-policy stages and return the executable state."""
    operator_resolved_model_node_ids = frozenset(
        node.id for node in state.nodes if node.plugin == "llm" and isinstance(node.options.get("profile"), str)
    )
    policy_result = validate_plugin_policy(
        state,
        snapshot=plugin_snapshot,
        profile_registry=profile_registry,
        catalog=catalog,
    )
    passed_checks: list[ValidationCheck] = []
    for stage, check_name in _PLUGIN_POLICY_CHECKS:
        stage_findings = policy_result.findings_for(stage)
        check = ValidationCheck(
            name=check_name,
            passed=not stage_findings,
            detail=f"{len(stage_findings)} plugin policy finding(s)." if stage_findings else f"{check_name} passed.",
            affected_nodes=tuple(dict.fromkeys(finding.component_id for finding in stage_findings if finding.component_id is not None)),
            outcome_code=None,
        )
        if stage_findings:
            first_finding = stage_findings[0]
            return PhaseFailure(
                passed_checks=tuple(passed_checks),
                failed_check=check,
                errors=tuple(
                    ValidationError(
                        component_id=item.component_id,
                        component_type=item.component_type,
                        message=item.message,
                        suggestion=item.suggestion if item.suggestion is not None else _DEFAULT_PLUGIN_POLICY_SUGGESTION,
                        error_code=item.error_code,
                    )
                    for item in stage_findings
                ),
                readiness=_blocked_readiness(
                    code=first_finding.error_code,
                    detail=first_finding.message,
                    component_id=first_finding.component_id,
                    component_type=first_finding.component_type,
                ),
            )
        passed_checks.append(check)
    return PhaseReport(
        artifact=PolicyLoweredState(
            state=policy_result.executable_state,
            operator_resolved_model_node_ids=operator_resolved_model_node_ids,
        ),
        checks=tuple(passed_checks),
    )


@observation_boundary(
    tier=3,
    source="composer-authored source, sink, and provider path options",
    source_param="policy",
    suppresses=("R1", "R5"),
    invariant="returns typed path-policy evidence or a typed validation failure; external path values do not escape unvalidated",
)
def validate_path_policy(
    policy: PolicyLoweredState,
    *,
    data_dir: Path,
    session_id: str | None,
) -> PhaseReport[PolicyLoweredState] | PhaseFailure:
    """Validate authored source, sink, and nested provider paths."""
    from elspeth.web.paths import (
        NESTED_LOCAL_PATH_OPTION_KEYS,
        SINK_LOCAL_PATH_OPTION_KEYS,
        SOURCE_LOCAL_PATH_OPTION_KEYS,
        allowed_sink_directories,
        allowed_source_directories,
        resolve_data_path,
        resolve_sink_data_path,
    )

    state = policy.state
    data_dir_text = str(data_dir)
    allowed_source_dirs = allowed_source_directories(data_dir_text, session_id=session_id)
    allowed_sink_dirs = allowed_sink_directories(data_dir_text, session_id=session_id)
    path_checked = False

    for source_name, source in state.sources.items():
        source_options = dict(source.options)
        source_component = "source" if source_name == "source" else f"source:{source_name}"
        for key in SOURCE_LOCAL_PATH_OPTION_KEYS:
            value = source_options.get(key)
            if value is None:
                continue
            if type(value) is not str:
                return _path_value_failure(
                    component_id=source_component,
                    component_type="source",
                    option_name=key,
                    detail=f"Source '{source_name}' {key} must be a string path; got {type(value).__name__}",
                )
            path_checked = True
            try:
                resolved = resolve_data_path(value, data_dir_text)
            except (OSError, TypeError, ValueError):
                return _path_value_failure(
                    component_id=source_component,
                    component_type="source",
                    option_name=key,
                    detail=f"Source '{source_name}' {key} is not a valid path",
                )
            if not any(resolved.is_relative_to(directory) for directory in allowed_source_dirs):
                return PhaseFailure(
                    passed_checks=(),
                    failed_check=ValidationCheck(
                        name=CHECK_PATH_ALLOWLIST,
                        passed=False,
                        detail=f"Source '{source_name}' {key} '{value}' is outside allowed source directories",
                        affected_nodes=(source_component,),
                        outcome_code=None,
                    ),
                    errors=(
                        ValidationError(
                            component_id=source_component,
                            component_type="source",
                            message=f"Path traversal blocked: source '{source_name}' {key}='{value}' resolves outside allowed directories",
                            suggestion="Use a file within the blobs directory.",
                            error_code=None,
                        ),
                    ),
                    readiness=_blocked_readiness(
                        code="path_allowlist",
                        detail=f"source '{source_name}' {key} resolves outside allowed source directories",
                        component_id=source_component,
                        component_type="source",
                    ),
                )

    for output in state.outputs:
        for key in SINK_LOCAL_PATH_OPTION_KEYS:
            value = output.options[key] if key in output.options else None
            if value is None:
                continue
            if type(value) is not str:
                return _path_value_failure(
                    component_id=output.name,
                    component_type="sink",
                    option_name=key,
                    detail=f"Sink '{output.name}' {key} must be a string path; got {type(value).__name__}",
                )
            path_checked = True
            try:
                resolved = resolve_sink_data_path(value, data_dir_text, session_id=session_id)
            except (OSError, TypeError, ValueError):
                return _path_value_failure(
                    component_id=output.name,
                    component_type="sink",
                    option_name=key,
                    detail=f"Sink '{output.name}' {key} is not a valid path",
                )
            if not any(resolved.is_relative_to(directory) for directory in allowed_sink_dirs):
                return PhaseFailure(
                    passed_checks=(),
                    failed_check=ValidationCheck(
                        name=CHECK_PATH_ALLOWLIST,
                        passed=False,
                        detail=f"Sink '{output.name}' {key} '{value}' is outside allowed output directories",
                        affected_nodes=(),
                        outcome_code=None,
                    ),
                    errors=(
                        ValidationError(
                            component_id=output.name,
                            component_type="sink",
                            message=f"Path traversal blocked: sink '{output.name}' {key}='{value}' resolves outside allowed directories",
                            suggestion="Use a path within this session's output or blob subtree.",
                            error_code=None,
                        ),
                    ),
                    readiness=_blocked_readiness(
                        code="path_allowlist",
                        detail=f"sink {output.name} {key} resolves outside allowed output directories",
                        component_id=output.name,
                        component_type="sink",
                    ),
                )

    for node in state.nodes:
        if node.node_type != "transform":
            continue
        provider_config = node.options["provider_config"] if "provider_config" in node.options else None
        if not isinstance(provider_config, Mapping):
            continue
        for key in NESTED_LOCAL_PATH_OPTION_KEYS:
            value = provider_config.get(key)
            if value is None:
                continue
            if type(value) is not str:
                return _path_value_failure(
                    component_id=node.id,
                    component_type="transform",
                    option_name=key,
                    detail=f"Transform '{node.id}' {key} must be a string path; got {type(value).__name__}",
                )
            path_checked = True
            try:
                resolved = resolve_sink_data_path(value, data_dir_text, session_id=session_id)
            except (OSError, TypeError, ValueError):
                return _path_value_failure(
                    component_id=node.id,
                    component_type="transform",
                    option_name=key,
                    detail=f"Transform '{node.id}' {key} is not a valid path",
                )
            if not any(resolved.is_relative_to(directory) for directory in allowed_sink_dirs):
                return PhaseFailure(
                    passed_checks=(),
                    failed_check=ValidationCheck(
                        name=CHECK_PATH_ALLOWLIST,
                        passed=False,
                        detail=f"Transform '{node.id}' {key} '{value}' is outside allowed output directories",
                        affected_nodes=(),
                        outcome_code=None,
                    ),
                    errors=(
                        ValidationError(
                            component_id=node.id,
                            component_type="transform",
                            message=f"Path traversal blocked: transform '{node.id}' {key}='{value}' resolves outside allowed directories",
                            suggestion="Use a path within this session's output or blob subtree.",
                            error_code=None,
                        ),
                    ),
                    readiness=_blocked_readiness(
                        code="path_allowlist",
                        detail=f"transform {node.id} {key} resolves outside allowed output directories",
                        component_id=node.id,
                        component_type="transform",
                    ),
                )

    detail = "All paths within allowed directories" if path_checked else "No path option — check skipped"
    return PhaseReport(
        artifact=policy,
        checks=(ValidationCheck(name=CHECK_PATH_ALLOWLIST, passed=True, detail=detail, affected_nodes=(), outcome_code=None),),
    )


@observation_boundary(
    tier=3,
    source="composer-authored web fetch network options",
    source_param="policy",
    suppresses=("R5",),
    invariant="returns typed policy evidence or a typed validation failure for every disallowed network shape",
)
def validate_web_network_policy(
    policy: PolicyLoweredState,
    *,
    plugin_snapshot: PluginAvailabilitySnapshot,
) -> PhaseReport[PolicyLoweredState] | PhaseFailure:
    """Reject web-authored private-network fetch configuration."""
    errors: list[ValidationError] = []
    for node in policy.state.nodes:
        if plugin_snapshot.is_trained_operator or node.plugin not in _WEB_FETCH_TRANSFORMS:
            continue
        http_options = node.options["http"] if "http" in node.options else None
        if not isinstance(http_options, Mapping) or "allowed_hosts" not in http_options:
            continue
        allowed_hosts = http_options["allowed_hosts"]
        if allowed_hosts == "allow_private":
            message = (
                f"{node.plugin}.http.allowed_hosts='allow_private' is not permitted in web execution. "
                "Web-authored pipelines may only use public SSRF policy; private-network fetching requires "
                "an operator-owned runtime outside the web composer."
            )
        elif isinstance(allowed_hosts, Sequence) and not isinstance(allowed_hosts, str):
            message = (
                f"{node.plugin}.http.allowed_hosts CIDR allowlists are not permitted in web execution. "
                "Web-authored pipelines may only use allowed_hosts='public_only'; private-network fetching "
                "requires an operator-owned runtime outside the web composer."
            )
        else:
            continue
        error_code = "web_scrape_private_network_not_allowed" if node.plugin == "web_scrape" else "web_fetch_private_network_not_allowed"
        errors.append(
            ValidationError(
                component_id=node.id,
                component_type="transform",
                message=message,
                suggestion=f"Set {node.plugin}.http.allowed_hosts to 'public_only' or remove the option.",
                error_code=error_code,
            )
        )
    if errors:
        return PhaseFailure(
            passed_checks=(),
            failed_check=ValidationCheck(
                name=CHECK_WEB_SCRAPE_NETWORK_POLICY,
                passed=False,
                detail="web fetch transform private-network allowlists are not permitted in web execution",
                affected_nodes=tuple(error.component_id for error in errors if error.component_id is not None),
                outcome_code=None,
            ),
            errors=tuple(errors),
            readiness=_blocked_readiness(
                code="web_scrape_network_policy",
                detail="web fetch transform private-network allowlists are not permitted in web execution.",
                component_id=errors[0].component_id,
                component_type="transform",
            ),
        )
    detail = (
        "Local trained-operator validation is exempt from the web fetch private-network policy"
        if plugin_snapshot.is_trained_operator
        else "No web fetch transform private-network allowlists found"
    )
    return PhaseReport(
        artifact=policy,
        checks=(ValidationCheck(name=CHECK_WEB_SCRAPE_NETWORK_POLICY, passed=True, detail=detail, affected_nodes=(), outcome_code=None),),
    )


@observation_boundary(
    tier=3,
    source="composer-authored web fetch resource options",
    source_param="policy",
    suppresses=("R5",),
    invariant="returns typed policy evidence or explicit typed conversion errors for malformed resource limits",
)
def validate_web_resource_policy(
    policy: PolicyLoweredState,
    *,
    plugin_snapshot: PluginAvailabilitySnapshot,
) -> PhaseReport[PolicyLoweredState] | PhaseFailure:
    """Bound blob-fetch resource use for web-authored pipelines."""
    errors: list[ValidationError] = []
    if not plugin_snapshot.is_trained_operator:
        for node in policy.state.nodes:
            if node.plugin != "blob_fetch" or "http" not in node.options:
                continue
            http_options = node.options["http"]
            if not isinstance(http_options, Mapping):
                errors.append(
                    ValidationError(
                        component_id=node.id,
                        component_type="transform",
                        message=f"blob_fetch.http must be a mapping; got {type(http_options).__name__}.",
                        suggestion="Set blob_fetch.http to a mapping of HTTP resource options.",
                        error_code="web_fetch_resource_config_invalid",
                    )
                )
                continue
            if "timeout" in http_options:
                raw_timeout = http_options["timeout"]
                parsed_timeout = _parse_resource_limit(
                    raw_timeout,
                    component_id=node.id,
                    option_name="timeout",
                    maximum=_WEB_BLOB_FETCH_MAX_TIMEOUT_SECONDS,
                )
                if parsed_timeout.error is not None:
                    errors.append(parsed_timeout.error)
                elif parsed_timeout.value is not None and parsed_timeout.value > _WEB_BLOB_FETCH_MAX_TIMEOUT_SECONDS:
                    errors.append(
                        ValidationError(
                            component_id=node.id,
                            component_type="transform",
                            message=(
                                f"blob_fetch.http.timeout={parsed_timeout.value} exceeds the web execution limit of "
                                f"{_WEB_BLOB_FETCH_MAX_TIMEOUT_SECONDS} seconds."
                            ),
                            suggestion=f"Set blob_fetch.http.timeout to {_WEB_BLOB_FETCH_MAX_TIMEOUT_SECONDS} or less.",
                            error_code="web_fetch_resource_limit_exceeded",
                        )
                    )
            if "max_body_bytes" in http_options:
                raw_max_body_bytes = http_options["max_body_bytes"]
                parsed_max_body_bytes = _parse_resource_limit(
                    raw_max_body_bytes,
                    component_id=node.id,
                    option_name="max_body_bytes",
                    maximum=_WEB_BLOB_FETCH_MAX_BODY_BYTES,
                )
                if parsed_max_body_bytes.error is not None:
                    errors.append(parsed_max_body_bytes.error)
                elif parsed_max_body_bytes.value is not None and parsed_max_body_bytes.value > _WEB_BLOB_FETCH_MAX_BODY_BYTES:
                    errors.append(
                        ValidationError(
                            component_id=node.id,
                            component_type="transform",
                            message=(
                                f"blob_fetch.http.max_body_bytes={parsed_max_body_bytes.value} exceeds the web execution limit of "
                                f"{_WEB_BLOB_FETCH_MAX_BODY_BYTES} bytes."
                            ),
                            suggestion=f"Set blob_fetch.http.max_body_bytes to {_WEB_BLOB_FETCH_MAX_BODY_BYTES} or less.",
                            error_code="web_fetch_resource_limit_exceeded",
                        )
                    )
    if errors:
        return PhaseFailure(
            passed_checks=(),
            failed_check=ValidationCheck(
                name=CHECK_WEB_FETCH_RESOURCE_POLICY,
                passed=False,
                detail="blob_fetch resource configuration violates the web execution policy",
                affected_nodes=tuple(error.component_id for error in errors if error.component_id is not None),
                outcome_code=None,
            ),
            errors=tuple(errors),
            readiness=_blocked_readiness(
                code=CHECK_WEB_FETCH_RESOURCE_POLICY,
                detail="blob_fetch resource configuration violates the web execution policy.",
                component_id=errors[0].component_id,
                component_type="transform",
            ),
        )
    detail = (
        "Local trained-operator validation is exempt from the web blob_fetch resource policy"
        if plugin_snapshot.is_trained_operator
        else "No blob_fetch resource limits exceed the web execution policy"
    )
    return PhaseReport(
        artifact=policy,
        checks=(ValidationCheck(name=CHECK_WEB_FETCH_RESOURCE_POLICY, passed=True, detail=detail, affected_nodes=(), outcome_code=None),),
    )


@observation_boundary(
    tier=3,
    source="composer-authored nested plugin options and secret markers",
    source_param="obj",
    suppresses=("R5",),
    invariant="returns only normalized secret names/scopes and ignores non-marker leaves",
)
def _collect_secret_refs(obj: object, env_ref_names: set[str] | frozenset[str] | None = None) -> list[tuple[str, SecretScope | None]]:
    """Collect deferred-secret names together with their requested scope."""
    refs: list[tuple[str, SecretScope | None]] = []
    if isinstance(obj, Mapping):
        marker = parse_secret_ref_marker(obj)
        if marker is not None:
            refs.append(marker)
            return refs
        for value in obj.values():
            refs.extend(_collect_secret_refs(value, env_ref_names))
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            refs.extend(_collect_secret_refs(item, env_ref_names))
    else:
        ref = secret_env_ref_name(obj, env_ref_names or frozenset())
        if ref is not None:
            refs.append((ref, None))
    return refs


def _secret_ref_exists(
    secret_service: WebSecretResolver,
    user_id: str,
    secret_ref: tuple[str, SecretScope | None],
) -> bool:
    name, scope = secret_ref
    if scope is None:
        return secret_service.has_ref(user_id, name)
    # Adjudication candidate: nominal admission of an owned dependency; the
    # runtime-checkable Protocol is intentionally not an authority check.
    if not isinstance(secret_service, ScopedSecretResolver):
        raise TypeError("Scoped secret validation requires the owned ScopedSecretResolver.resolve_scoped dependency")
    return secret_service.resolve_scoped(user_id, name, scope) is not None


def validate_secret_evidence(
    policy: PolicyLoweredState,
    *,
    secret_service: WebSecretResolver | None,
    user_id: str | None,
) -> PhaseReport[SecretValidatedState] | PhaseFailure:
    """Validate secret markers and retain the evidence needed for loading."""
    state = policy.state
    all_refs: list[tuple[str, SecretScope | None]] = []
    env_ref_names: set[str] = set()
    fabricated_components: list[tuple[str | None, str | None, list[str]]] = []
    disallowed_components: list[tuple[str | None, str, str, list[SecretRefPlacementViolation]]] = []
    if secret_service is not None and user_id is not None:
        env_ref_names = {item.name for item in secret_service.list_refs(user_id)}
        for source_name, source in state.sources.items():
            source_component = "source" if source_name == "source" else f"source:{source_name}"
            all_refs.extend(_collect_secret_refs(source.options, env_ref_names))
            fabricated = collect_credential_field_violations(source.options, env_ref_names)
            if fabricated:
                fabricated_components.append((source_component, "source", fabricated))
            disallowed = collect_disallowed_secret_ref_markers(
                source.options,
                env_ref_names,
                additional_allowed_fields=allowed_secret_ref_fields("source", source.plugin),
            )
            if disallowed:
                disallowed_components.append((source_component, "source", source.plugin, disallowed))
        for node in state.nodes:
            all_refs.extend(_collect_secret_refs(node.options, env_ref_names))
            fabricated = collect_credential_field_violations(node.options, env_ref_names)
            if fabricated:
                fabricated_components.append((node.id, "transform", fabricated))
            node_plugin = node.plugin or "<unset>"
            disallowed = collect_disallowed_secret_ref_markers(
                node.options,
                env_ref_names,
                additional_allowed_fields=allowed_secret_ref_fields("transform", node_plugin),
            )
            if disallowed:
                disallowed_components.append((node.id, "transform", node_plugin, disallowed))
        for output in state.outputs:
            all_refs.extend(_collect_secret_refs(output.options, env_ref_names))
            fabricated = collect_credential_field_violations(output.options, env_ref_names)
            if fabricated:
                fabricated_components.append((output.name, "sink", fabricated))
            disallowed = collect_disallowed_secret_ref_markers(
                output.options,
                env_ref_names,
                additional_allowed_fields=allowed_secret_ref_fields("sink", output.plugin),
            )
            if disallowed:
                disallowed_components.append((output.name, "sink", output.plugin, disallowed))

        missing_refs = [ref[0] for ref in all_refs if not _secret_ref_exists(secret_service, user_id, ref)]
        if missing_refs or fabricated_components or disallowed_components:
            detail_parts: list[str] = []
            errors: list[ValidationError] = []
            if missing_refs:
                names = ", ".join(missing_refs)
                detail_parts.append(f"Missing secret references: {names}")
                errors.append(
                    ValidationError(
                        component_id=None,
                        component_type=None,
                        message=f"Cannot resolve secret references: {names}",
                        suggestion="Add the missing secrets via the Secrets panel before executing.",
                        error_code="missing_secret_ref",
                    )
                )
            if fabricated_components:
                fabricated_summary = ", ".join(
                    f"{component_id}:{','.join(fields)}" for component_id, _type, fields in fabricated_components
                )
                detail_parts.append("Literal value in credential field(s): " + fabricated_summary)
                for component_id, component_type, fields in fabricated_components:
                    fields_text = ", ".join(fields)
                    errors.append(
                        ValidationError(
                            component_id=component_id,
                            component_type=component_type,
                            message=f"Credential field(s) {fields_text} contain a literal value; expected a wired secret reference.",
                            suggestion=(
                                "Wire each credential field through the Secrets panel "
                                "(produces a {secret_ref: NAME} marker) instead of typing "
                                "the value directly."
                            ),
                            error_code="fabricated_secret",
                        )
                    )
            for component_id, component_type, plugin_name, violations in disallowed_components:
                violation_text = ", ".join(f"{violation.field_path} -> {violation.secret_name}" for violation in violations)
                allowed_text = allowed_secret_ref_fields_text(component_type, plugin_name)
                detail_parts.append(f"Disallowed secret_ref for {plugin_name} {component_id}: {violation_text}")
                errors.extend(
                    ValidationError(
                        component_id=component_id,
                        component_type=component_type,
                        message=(
                            f"Plugin '{plugin_name}' field '{violation.field_path}' contains secret_ref "
                            f"'{violation.secret_name}', but only credential-bearing fields may carry secret_ref "
                            f"markers. Allowed credential-bearing fields for this plugin: {allowed_text}."
                        ),
                        suggestion=(
                            "Move the secret_ref marker to an actual credential field, or use a literal "
                            "non-secret value for wire-visible identity/configuration fields."
                        ),
                        error_code="disallowed_secret_ref",
                    )
                    for violation in violations
                )
            return PhaseFailure(
                passed_checks=(),
                failed_check=ValidationCheck(
                    name=CHECK_SECRET_REFS,
                    passed=False,
                    detail="; ".join(detail_parts),
                    affected_nodes=(),
                    outcome_code=CHECK_OUTCOME_SECRET_REFS_UNRESOLVED,
                ),
                errors=tuple(errors),
                readiness=_blocked_readiness(code="secret_refs", detail="Secret reference validation failed."),
            )
        outcome_code = CHECK_OUTCOME_SECRET_REFS_RESOLVED if all_refs else CHECK_OUTCOME_SECRET_REFS_NO_REFS
        detail = f"All {len(all_refs)} secret reference(s) resolved" if all_refs else "No secret references found"
    else:
        outcome_code = CHECK_OUTCOME_SECRET_REFS_SKIPPED_NO_SERVICE
        detail = "No secret service — check skipped"
    return PhaseReport(
        artifact=SecretValidatedState(
            policy=policy,
            all_secret_refs=tuple(all_refs),
            env_ref_names=frozenset(env_ref_names),
        ),
        checks=(ValidationCheck(name=CHECK_SECRET_REFS, passed=True, detail=detail, affected_nodes=(), outcome_code=outcome_code),),
    )


def validate_semantic_evidence(
    secret: SecretValidatedState,
) -> PhaseReport[AuthoredValidatedState] | PhaseFailure:
    """Validate and serialize authored semantic edge contracts."""
    semantic_errors, semantic_contracts = validate_semantic_contracts(secret.policy.state)
    responses = tuple(serialize_semantic_contracts(semantic_contracts))
    if semantic_errors:
        return PhaseFailure(
            passed_checks=(),
            failed_check=ValidationCheck(
                name=CHECK_SEMANTIC_CONTRACTS,
                passed=False,
                detail="Semantic contract check failed",
                affected_nodes=(),
                outcome_code=None,
            ),
            errors=tuple(
                ValidationError(
                    component_id=entry.component.removeprefix("node:"),
                    component_type="transform",
                    message=entry.message,
                    suggestion=assistance_suggestion_for(entry, semantic_contracts),
                    error_code=None,
                )
                for entry in semantic_errors
            ),
            readiness=_blocked_readiness(code="semantic_contracts", detail="Semantic contract check failed."),
            semantic_contracts=responses,
        )
    detail = f"All {len(semantic_contracts)} semantic contract(s) satisfied" if semantic_contracts else "No semantic contracts to check"
    return PhaseReport(
        artifact=AuthoredValidatedState(
            policy=secret.policy,
            all_secret_refs=secret.all_secret_refs,
            env_ref_names=secret.env_ref_names,
            semantic_contracts=responses,
        ),
        checks=(ValidationCheck(name=CHECK_SEMANTIC_CONTRACTS, passed=True, detail=detail, affected_nodes=(), outcome_code=None),),
    )


def validate_batch_options(
    authored: AuthoredValidatedState,
) -> PhaseReport[AuthoredValidatedState] | PhaseFailure:
    """Validate ADR-013 batch-aware transform authoring options."""
    state = authored.policy.state
    batch_option_errors: list[tuple[str, str]] = []
    for node in state.nodes:
        placement_error = _batch_aware_placement_error(node.id, node.node_type, node.plugin, node.output_mode)
        if placement_error is not None:
            batch_option_errors.append((node.id, placement_error))
        required_error = _batch_aware_required_input_fields_error(node.id, node.plugin, node.options)
        if required_error is not None:
            batch_option_errors.append((node.id, required_error))
    numeric_errors, _numeric_warnings = _batch_distribution_profile_value_field_entries(state.sources, state.nodes)
    batch_option_errors.extend((entry.component.removeprefix("node:"), entry.message) for entry in numeric_errors)
    if batch_option_errors:
        return PhaseFailure(
            passed_checks=(),
            failed_check=ValidationCheck(
                name=CHECK_BATCH_TRANSFORM_OPTIONS,
                passed=False,
                detail="Batch-aware transform option check failed",
                affected_nodes=(),
                outcome_code=None,
            ),
            errors=tuple(
                ValidationError(
                    component_id=node_id,
                    component_type="transform",
                    message=message,
                    suggestion=(
                        "Use node_type='aggregation' for batch-aware plugins; remove required_input_fields from batch-aware transform "
                        "options and use schema.required_fields for batch input validation."
                    ),
                    error_code=None,
                )
                for node_id, message in batch_option_errors
            ),
            readiness=_blocked_readiness(code="batch_transform_options", detail="Batch-aware transform option check failed."),
            semantic_contracts=authored.semantic_contracts,
        )
    return PhaseReport(
        artifact=authored,
        checks=(
            ValidationCheck(
                name=CHECK_BATCH_TRANSFORM_OPTIONS,
                passed=True,
                detail="Batch-aware transform options are compatible with ADR-013",
                affected_nodes=(),
                outcome_code=None,
            ),
        ),
    )


def _format_interpretation_site(site: InterpretationReviewSite) -> str:
    return f"{site.kind.value} review pending for {site.component_type} {site.component_id!r}: {site.user_term}"


def _format_interpretation_sites(sites: Sequence[InterpretationReviewSite]) -> str:
    return ", ".join(_format_interpretation_site(site) for site in sites)


def review_interpretations(
    authored: AuthoredValidatedState,
    *,
    allow_pending_placeholders: bool,
) -> PhaseReport[InterpretationValidatedState] | PhaseFailure:
    """Select the execution-strict or authoring-masked interpretation state."""
    state = authored.policy.state
    materialized_state = (
        materialize_state_for_authoring(state)
        if allow_pending_placeholders
        else materialize_state_for_execution(
            state,
            operator_resolved_model_node_ids=authored.policy.operator_resolved_model_node_ids,
        )
    )
    # Adjudication candidate: this is nominal discrimination of an ELSPETH-owned
    # closed result union, not defensive shape checking at a trust boundary.
    if isinstance(materialized_state, InterpretationReviewPending):
        site_detail = _format_interpretation_sites(materialized_state.sites)
        affected_nodes = tuple(dict.fromkeys(site.component_id for site in materialized_state.sites if site.component_type == "transform"))
        single_site = materialized_state.sites[0] if len(materialized_state.sites) == 1 else None
        return PhaseFailure(
            passed_checks=(),
            failed_check=ValidationCheck(
                name=CHECK_INTERPRETATION_REVIEW,
                passed=False,
                detail=f"Interpretation review is pending for {site_detail}.",
                affected_nodes=affected_nodes,
                outcome_code=None,
            ),
            errors=tuple(
                ValidationError(
                    component_id=site.component_id,
                    component_type=site.component_type,
                    message=_format_interpretation_site(site),
                    suggestion="Resolve the pending interpretation review before running.",
                    error_code=INTERPRETATION_REVIEW_PENDING_CODE,
                )
                for site in materialized_state.sites
            ),
            readiness=_blocked_readiness(
                code=INTERPRETATION_REVIEW_PENDING_CODE,
                detail=site_detail,
                component_id=single_site.component_id if single_site is not None else None,
                component_type=single_site.component_type if single_site is not None else None,
                authoring_valid=True,
                completion_ready=True,
            ),
            semantic_contracts=authored.semantic_contracts,
        )
    return PhaseReport(
        artifact=InterpretationValidatedState(authored=authored, materialized_state=materialized_state),
        checks=(
            ValidationCheck(
                name=CHECK_INTERPRETATION_REVIEW,
                passed=True,
                detail="No pending interpretation review",
                affected_nodes=(),
                outcome_code=None,
            ),
        ),
    )


__all__ = [
    "SecretValidatedState",
    "_collect_secret_refs",
    "lower_plugin_policy",
    "review_interpretations",
    "validate_batch_options",
    "validate_path_policy",
    "validate_secret_evidence",
    "validate_semantic_evidence",
    "validate_web_network_policy",
    "validate_web_resource_policy",
]
