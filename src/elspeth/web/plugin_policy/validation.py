"""Pure validation and lowering for one frozen plugin-policy snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Literal

from jsonschema import Draft202012Validator

from elspeth.contracts.freeze import deep_thaw, freeze_fields
from elspeth.contracts.plugin_capabilities import ControlMode, WebConfigAuthority
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.catalog.schemas import PluginSchemaInfo
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, SourceSpec, ValidationEntry, ValidationSummary
from elspeth.web.interpretation_state import AUTHORING_METADATA_OPTION_KEYS
from elspeth.web.plugin_policy.coverage import ControlCoverageFinding, control_coverage_findings
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId, PluginUnavailableReason
from elspeth.web.plugin_policy.profiles import LoweredPluginConfig, OperatorProfileRegistry

PolicyValidationStage = Literal[
    "plugin_enablement",
    "operator_profile_options",
    "required_control_availability",
    "required_control_coverage",
]

_PROFILE_LOWERING_METADATA_OPTION_KEYS = AUTHORING_METADATA_OPTION_KEYS | {"resolved_prompt_template_hash"}


@dataclass(frozen=True, slots=True)
class PluginPolicyFinding:
    stage: PolicyValidationStage
    component_id: str | None
    component_type: str | None
    error_code: str
    message: str
    # Per-finding remediation, set when the producer can diagnose the repair
    # more precisely than the stage-level default (currently only
    # ``_control_coverage_finding``, keyed on the coverage reason). ``None``
    # falls back to the consumer's stage default suggestion.
    suggestion: str | None = None


@dataclass(frozen=True, slots=True)
class PluginPolicyValidationResult:
    executable_state: CompositionState = field(repr=False)
    findings: tuple[PluginPolicyFinding, ...]

    def findings_for(self, stage: PolicyValidationStage) -> tuple[PluginPolicyFinding, ...]:
        return tuple(finding for finding in self.findings if finding.stage == stage)


@dataclass(frozen=True, slots=True)
class _Component:
    component_id: str
    component_type: Literal["source", "transform", "sink"]
    plugin_id: PluginId | None
    options: Mapping[str, object]

    def __post_init__(self) -> None:
        freeze_fields(self, "options")


def validate_plugin_policy(
    state: CompositionState,
    *,
    snapshot: PluginAvailabilitySnapshot,
    profile_registry: OperatorProfileRegistry | None,
    catalog: CatalogService,
) -> PluginPolicyValidationResult:
    """Validate identities/controls and lower public profile options in memory.

    The returned state is executable-only. ``state`` remains the audit-safe
    authored value and is never mutated.
    """
    findings: list[PluginPolicyFinding] = []
    components = _components(state)
    unavailable = {item.plugin_id: item.reason for item in snapshot.unavailable}

    for component in components:
        if component.plugin_id is None:
            findings.append(
                PluginPolicyFinding(
                    stage="plugin_enablement",
                    component_id=component.component_id,
                    component_type=component.component_type,
                    error_code="plugin_unavailable",
                    message="The configured plugin identity is invalid or unavailable.",
                )
            )
            continue
        if component.plugin_id in snapshot.available:
            continue
        reason = unavailable.get(component.plugin_id, PluginUnavailableReason.NOT_AUTHORIZED)
        findings.append(
            PluginPolicyFinding(
                stage="plugin_enablement",
                component_id=component.component_id,
                component_type=component.component_type,
                error_code=reason.value,
                message=f"Plugin '{component.plugin_id}' is not available for this request.",
            )
        )

    executable_state = state
    if not findings and not snapshot.is_trained_operator:
        executable_state, profile_findings = _lower_profiled_components(
            state,
            snapshot=snapshot,
            profile_registry=profile_registry,
            catalog=catalog,
        )
        findings.extend(profile_findings)

    selected = dict(snapshot.selected)
    required = tuple(capability for capability, mode in snapshot.control_modes if mode is ControlMode.REQUIRED)
    for capability in required:
        selected_plugin = selected.get(capability)
        if selected_plugin is not None and selected_plugin in snapshot.available:
            continue
        findings.append(
            PluginPolicyFinding(
                stage="required_control_availability",
                component_id=None,
                component_type=None,
                error_code="required_control_unavailable",
                message=f"Required control '{capability.value}' has no available implementation.",
            )
        )

    for capability in required:
        for coverage in control_coverage_findings(state, capability):
            findings.append(_control_coverage_finding(coverage))

    return PluginPolicyValidationResult(executable_state=executable_state, findings=tuple(findings))


# Per-diagnosis remediation for coverage findings, keyed on the finding's
# ``reason`` — never on the stage. The three diagnoses have three different
# repairs, and a stage-level string cannot be right for all of them: telling
# an input-domination (prompt_shield) author to "set on_error to 'discard'"
# names a repair that cannot address the finding, while the error-route
# conflict has exactly that one authorable repair. Total over
# ``ControlCoverageFinding.reason`` (pinned by test); a KeyError here means a
# new reason was added without deciding its remediation.
_CONTROL_COVERAGE_SUGGESTIONS: dict[str, str] = {
    "input_not_dominated": (
        "Interpose the required control transform upstream of the named node so every "
        "path carrying data into it passes the control first: route the producer (or "
        "source) through the control, then connect the control's output to this node. "
        "Then validate again. If that layout is not possible, ask the operator to relax "
        "the control mode to 'recommend' — it is not an authoring change."
    ),
    "input_fields_unprovable": (
        "The wiring is already right — do not move the control. Set the control's "
        "'fields' to 'all' so it scans every string field, or rewrite the node's "
        "prompt template so every row access is static ('{{ row.field }}', never "
        "'{{ row[key] }}') and list those exact fields in the control's 'fields'. "
        "Then validate again."
    ),
    "output_not_post_dominated": (
        "Wire the required control transform so it sits on every path that carries the "
        "named node's output before any sink, then validate again. If a conforming "
        "layout is not possible, ask the operator to relax the control mode to "
        "'recommend' — it is not an authoring change."
    ),
    "output_error_route_not_post_dominated": (
        "Set the named node's on_error to 'discard' — an on_error route is a separate "
        "write path and no control can sit on it (on_error may only name a sink or "
        "'discard'). Then validate again. If failed rows must be kept in a quarantine "
        "sink, ask the operator to relax the control mode to 'recommend' — it is not an "
        "authoring change."
    ),
}


def _field_set(fields: tuple[str, ...]) -> str:
    """Render a coverage field set for an author-facing message."""
    return f"[{', '.join(fields)}]" if fields else "none provable"


def _control_coverage_finding(coverage: ControlCoverageFinding) -> PluginPolicyFinding:
    """Name the on_error conflict and its one authorable repair.

    The bare "not covered" message told authors nothing about WHY an
    otherwise-correct pipeline was rejected, and the on_error case is the one
    that reads as a contradiction: the planner is taught to route failures to a
    quarantine sink, then a required output control rejects the pipeline for
    doing exactly that. The conflict is real and the rejection is correct — an
    on_error edge writes rows to a sink without passing the control, so it is
    an independent output path.

    There is exactly ONE authoring repair. A transform's ``on_error`` may only
    name a sink or the literal ``discard`` (``core/dag/builder.py:1108``,
    mirrored at ``web/composer/state.py:1060``), and ``on_error`` is mandatory
    (``transform_missing_on_error``) — so no control transform can sit on an
    error branch. Do NOT offer interposing the control on the error branch: the
    graph rejects that edge (``transform_on_error_unknown_sink`` at the composer
    surface, ``No producer for connection`` when built), so a planner that
    followed the advice would ping-pong between two rejections.

    Preserving the failed rows is a real need with a real answer, but it lives
    with the operator, not the author: ``on_error: <quarantine sink>`` builds
    and runs fine, and only the *required* control mode rejects it. Naming that
    escape hatch keeps the author from concluding the requirement is a bug. See
    ``docs/reference/configuration.md`` — "Required controls and error routing".
    """
    if coverage.reason == "output_error_route_not_post_dominated" and coverage.uncovered_stream is not None:
        message = (
            f"Node '{coverage.component_id}' routes its on_error rows to the "
            f"'{coverage.uncovered_stream}' sink, which is an independent output path: those rows "
            f"are written without passing the required '{coverage.capability.value}' "
            f"{coverage.role.value} control, so this node is not covered. A transform's on_error "
            "may only name a sink or 'discard', so no control can be interposed on an error "
            "branch — set this node's on_error to 'discard'. That drops the failed row's content "
            "(nothing reaches a sink to inspect later) while the audit trail still records its "
            "terminal outcome and content hash. Preserving failed rows in a quarantine sink is an "
            f"operator decision, not an authoring workaround: it needs the "
            f"'{coverage.capability.value}' control mode relaxed to 'recommend', or the pipeline "
            "run under the CLI/batch runtime where web plugin policy does not apply."
        )
    elif coverage.reason == "input_fields_unprovable":
        message = (
            f"Node '{coverage.component_id}' has a required '{coverage.capability.value}' "
            f"{coverage.role.value} control upstream, but its own protected field set could not "
            "be proven from its prompt template, so a control scoped to specific fields cannot be "
            f"credited: protected fields {_field_set(coverage.protected_fields)}, control scans "
            f"{_field_set(coverage.scanned_fields)}. An empty protected set is not proof that no "
            "field needs protecting — a dynamic access such as row[key] extracts nothing either — "
            "so only a control with fields: 'all' covers it. The wiring itself is correct; this is "
            "a control-scope repair, not a layout repair."
        )
    else:
        message = (
            f"Node '{coverage.component_id}' is not covered by the required '{coverage.capability.value}' {coverage.role.value} control."
        )
        if coverage.protected_fields and coverage.scanned_fields:
            message += (
                f" It reads row fields {_field_set(coverage.protected_fields)}, while the nearest "
                f"upstream control scans {_field_set(coverage.scanned_fields)}."
            )
    return PluginPolicyFinding(
        stage="required_control_coverage",
        component_id=coverage.component_id,
        component_type="transform",
        error_code="required_control_coverage",
        message=message,
        suggestion=_CONTROL_COVERAGE_SUGGESTIONS[coverage.reason],
    )


def _plugin_id(kind: Literal["source", "transform", "sink"], name: str) -> PluginId | None:
    try:
        return PluginId(kind, name)
    except ValueError:
        return None


def _components(state: CompositionState) -> tuple[_Component, ...]:
    result: list[_Component] = []
    for source_name, source in sorted(state.sources.items()):
        component_id = "source" if source_name == "source" else f"source:{source_name}"
        result.append(
            _Component(
                component_id=component_id,
                component_type="source",
                plugin_id=_plugin_id("source", source.plugin),
                options=deep_thaw(source.options),
            )
        )
    for node in state.nodes:
        if node.plugin is None:
            continue
        result.append(
            _Component(
                component_id=node.id,
                component_type="transform",
                plugin_id=_plugin_id("transform", node.plugin),
                options=deep_thaw(node.options),
            )
        )
    for output in state.outputs:
        result.append(
            _Component(
                component_id=output.name,
                component_type="sink",
                plugin_id=_plugin_id("sink", output.plugin),
                options=deep_thaw(output.options),
            )
        )
    return tuple(result)


def _lower_profiled_components(
    state: CompositionState,
    *,
    snapshot: PluginAvailabilitySnapshot,
    profile_registry: OperatorProfileRegistry | None,
    catalog: CatalogService,
) -> tuple[CompositionState, tuple[PluginPolicyFinding, ...]]:
    aliases_by_plugin = dict(snapshot.usable_profile_aliases)
    components = _components(state)
    lowered_options: dict[tuple[str, str], dict[str, object]] = {}
    findings: list[PluginPolicyFinding] = []
    lowering_registry = profile_registry
    lowering_catalog = catalog
    for component in components:
        profile_context = _profile_lowering_context(
            component,
            aliases_by_plugin=aliases_by_plugin,
            profile_registry=lowering_registry,
            catalog=lowering_catalog,
            findings=findings,
        )
        if profile_context is None:
            continue
        plugin_id, aliases, resolved_public_schema = profile_context
        authored_options = {
            name: deep_thaw(value) for name, value in component.options.items() if name not in _PROFILE_LOWERING_METADATA_OPTION_KEYS
        }
        authoring_metadata = {
            name: deep_thaw(value) for name, value in component.options.items() if name in _PROFILE_LOWERING_METADATA_OPTION_KEYS
        }
        alias = authored_options.pop("profile", None)
        if not isinstance(alias, str) or alias not in aliases:
            findings.append(_profile_unavailable_finding(component, plugin_id, available_aliases=tuple(aliases)))
            continue
        public_schema = resolved_public_schema.json_schema
        public_options = {"profile": alias, **authored_options}
        schema_errors = sorted(
            Draft202012Validator(public_schema).iter_errors(public_options),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if schema_errors:
            # Name the defect value-free (pack pressure-suite run 3, P2): the
            # bare "do not match" message hid WHICH option broke profile
            # authoring — a profile-bound node carrying an operator-private
            # option (e.g. max_capacity_retry_seconds) read as "profile gone"
            # and no planner could repair it. Unexpected option NAMES are
            # author-authored keys; offending VALUES are never echoed.
            allowed_properties = set(public_schema.get("properties") or ())
            unexpected = sorted(set(public_options) - allowed_properties)
            if unexpected:
                detail = (
                    f"option(s) not authorable on a profile-bound node: {unexpected}. "
                    "The operator profile supplies provider/model/credential/pacing settings — remove them."
                )
            else:
                failing = sorted({"/".join(str(part) for part in error.absolute_path) or "<options>" for error in schema_errors})
                detail = f"option(s) failing the public profile schema: {failing}."
            findings.append(
                PluginPolicyFinding(
                    stage="operator_profile_options",
                    component_id=component.component_id,
                    component_type=component.component_type,
                    error_code="profile_unavailable",
                    message=f"Plugin '{plugin_id}' options do not match its public operator-profile schema; {detail}",
                )
            )
            continue
        try:
            assert lowering_registry is not None
            lowered = _lower_profile_options(
                lowering_registry,
                plugin_id,
                alias=alias,
                safe_options=authored_options,
            )
        except ValueError as exc:
            # Discriminate the two lowering rejections (pack pressure-suite
            # run 3, P2): collapsing private_profile_option into the
            # availability message told authors the profile was gone when the
            # real defect was an operator-private option in their node — a
            # message no planner can repair from.
            if str(exc) == "private_profile_option":
                message = (
                    f"Plugin '{plugin_id}' profile-bound options include operator-private option(s) "
                    "(provider/model/credential/pacing settings such as pool_size or "
                    "max_capacity_retry_seconds). Remove them — the operator profile supplies these."
                )
            else:
                message = f"Plugin '{plugin_id}' operator profile is no longer available."
            findings.append(
                PluginPolicyFinding(
                    stage="operator_profile_options",
                    component_id=component.component_id,
                    component_type=component.component_type,
                    error_code="plugin_unavailable",
                    message=message,
                )
            )
            continue
        lowered_options[(component.component_type, component.component_id)] = {
            **deep_thaw(lowered.executable_options),
            **authoring_metadata,
        }

    if findings:
        return state, _normalized_profile_findings(findings)

    sources: dict[str, SourceSpec] = {}
    for source_name, source in state.sources.items():
        component_id = "source" if source_name == "source" else f"source:{source_name}"
        options = lowered_options.get(("source", component_id))
        sources[source_name] = source if options is None else replace(source, options=options)
    nodes: list[NodeSpec] = []
    for node in state.nodes:
        options = lowered_options.get(("transform", node.id))
        nodes.append(node if options is None else replace(node, options=options))
    outputs: list[OutputSpec] = []
    for output in state.outputs:
        options = lowered_options.get(("sink", output.name))
        outputs.append(output if options is None else replace(output, options=options))
    return (
        replace(state, sources=sources, nodes=tuple(nodes), outputs=tuple(outputs)),
        (),
    )


def _profile_unavailable_finding(
    component: _Component,
    plugin_id: PluginId,
    available_aliases: tuple[str, ...] = (),
) -> PluginPolicyFinding:
    # Two distinct realities share this error code: profiles exist but the
    # options failed to select one (author-fixable — name the aliases), and
    # no profile is configured or available at all (operator-fixable).
    # Conflating them told authors "an operator must enable a profile" while
    # a valid alias was sitting in the enum, causing false honest declines.
    if available_aliases:
        message = (
            f"Plugin '{plugin_id}' requires an operator profile and the options did not select "
            f"one: set the 'profile' option to one of the operator-approved aliases "
            f"{sorted(available_aliases)!r}."
        )
    else:
        message = (
            f"Plugin '{plugin_id}' is installed but not turned on in this deployment: "
            "it requires an operator profile and none is configured or available. "
            "An operator must enable a profile before this plugin can be used."
        )
    return PluginPolicyFinding(
        stage="operator_profile_options",
        component_id=component.component_id,
        component_type=component.component_type,
        error_code="profile_unavailable",
        message=message,
    )


@dataclass(frozen=True, slots=True)
class ProfileAwareValidationResult:
    """One authoritative composer validation result for authored web state."""

    authored_state: CompositionState
    executable_state: CompositionState = field(repr=False)
    policy_findings: tuple[PluginPolicyFinding, ...]
    validation: ValidationSummary


_IMMEDIATE_BLOCKING_STAGES: frozenset[PolicyValidationStage] = frozenset({"plugin_enablement", "operator_profile_options"})


def validate_authored_composition_state(
    state: CompositionState,
    *,
    snapshot: PluginAvailabilitySnapshot,
    profile_registry: OperatorProfileRegistry | None,
    catalog: CatalogService,
) -> ProfileAwareValidationResult:
    """Validate authored state once through the principal's policy boundary."""
    if snapshot.is_trained_operator:
        return ProfileAwareValidationResult(
            authored_state=state,
            executable_state=state,
            policy_findings=(),
            validation=state.validate(),
        )

    policy = validate_plugin_policy(
        state,
        snapshot=snapshot,
        profile_registry=profile_registry,
        catalog=catalog,
    )
    blocking = tuple(finding for finding in policy.findings if finding.stage in _IMMEDIATE_BLOCKING_STAGES)
    evidence = tuple(finding for finding in policy.findings if finding.stage not in _IMMEDIATE_BLOCKING_STAGES)
    blocking_entries = tuple(_validation_entry(finding, severity="high") for finding in blocking)
    evidence_entries = tuple(_validation_entry(finding, severity="medium") for finding in evidence)

    if blocking_entries:
        summary = ValidationSummary(
            is_valid=False,
            errors=blocking_entries,
            warnings=evidence_entries,
        )
    else:
        executable_summary = policy.executable_state.validate()
        summary = replace(
            executable_summary,
            warnings=(*evidence_entries, *executable_summary.warnings),
        )

    return ProfileAwareValidationResult(
        authored_state=state,
        executable_state=policy.executable_state,
        policy_findings=policy.findings,
        validation=summary,
    )


def _validation_entry(finding: PluginPolicyFinding, *, severity: Literal["high", "medium"]) -> ValidationEntry:
    component = "pipeline"
    if finding.component_id is not None:
        if finding.component_type == "transform":
            component = f"node:{finding.component_id}"
        elif finding.component_type == "sink":
            component = f"output:{finding.component_id}"
        else:
            component = finding.component_id
    return ValidationEntry(
        component=component,
        message=finding.message,
        severity=severity,
        error_code=finding.error_code,
    )


def _profile_lowering_context(
    component: _Component,
    *,
    aliases_by_plugin: Mapping[PluginId, tuple[str, ...]],
    profile_registry: OperatorProfileRegistry | None,
    catalog: CatalogService,
    findings: list[PluginPolicyFinding],
) -> tuple[PluginId, tuple[str, ...], PluginSchemaInfo] | None:
    """Resolve the public profile schema or record one closed failure."""
    plugin_id = component.plugin_id
    if plugin_id is None:
        return None
    full_schema = catalog.get_schema(plugin_id.kind, plugin_id.name)
    requires_profile = full_schema.web_config_authority is WebConfigAuthority.OPERATOR_PROFILED
    if not requires_profile and "profile" not in component.options:
        return None
    aliases = aliases_by_plugin[plugin_id] if plugin_id in aliases_by_plugin else ()
    if profile_registry is None or not aliases:
        findings.append(_profile_unavailable_finding(component, plugin_id))
        return None
    public_schema = profile_registry.public_schema(
        plugin_id,
        full_schema,
        available_aliases=aliases,
    )
    return plugin_id, aliases, public_schema


def _normalized_profile_findings(findings: list[PluginPolicyFinding]) -> tuple[PluginPolicyFinding, ...]:
    """Expose the single public profile failure code at the web boundary."""
    return tuple(
        replace(finding, error_code="profile_unavailable")
        if finding.stage == "operator_profile_options" and finding.error_code == "plugin_unavailable"
        else finding
        for finding in findings
    )


def _lower_profile_options(
    profile_registry: OperatorProfileRegistry,
    plugin_id: PluginId,
    *,
    alias: str,
    safe_options: dict[str, object],
) -> LoweredPluginConfig:
    """Collapse expected resolver outages into the established ValueError path."""
    try:
        return profile_registry.lower_options(plugin_id, alias=alias, safe_options=safe_options)
    except RuntimeError as exc:
        raise ValueError("profile_unavailable") from exc
