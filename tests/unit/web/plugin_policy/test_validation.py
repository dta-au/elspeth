def test_profile_unavailable_finding_enumerates_available_aliases() -> None:
    """When operator profiles EXIST and the options simply failed to select
    one, the finding must say so and name the aliases — telling the model
    'an operator must enable a profile' when 'sonnet' is sitting right there
    caused a false honest decline and blind repairs (live sessions 6b9da203 +
    e47fd8df)."""
    from elspeth.web.plugin_policy.validation import _profile_unavailable_finding

    class _Component:
        component_id = "node:llm_1"
        component_type = "transform"

    from elspeth.web.plugin_policy.models import PluginId

    finding = _profile_unavailable_finding(_Component(), PluginId("transform", "llm"), available_aliases=("sonnet",))
    assert "sonnet" in finding.message
    assert "operator must enable" not in finding.message

    unconfigured = _profile_unavailable_finding(_Component(), PluginId("transform", "llm"))
    assert "sonnet" not in unconfigured.message


def _textract_policy_context() -> tuple[object, object, object]:
    """Real registry + snapshot + catalog for one profiled Textract transform."""
    from elspeth.web.config import WebSettings
    from elspeth.web.dependencies import create_catalog_service
    from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
    from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId
    from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig

    settings = WebSettings.model_validate(
        {
            "composer_max_composition_turns": 4,
            "composer_max_discovery_turns": 4,
            "composer_timeout_seconds": 60,
            "composer_rate_limit_per_minute": 20,
            "shareable_link_signing_key": b"0123456789abcdef0123456789abcdef",
            "plugin_allowlist": ["transform:aws_textract_document_analysis"],
            "deployment_aws_region": "ap-southeast-1",
            "aws_textract_profiles": [
                {
                    "alias": "acceptance-docs",
                    "bucket": "operator-private-bucket-marker",
                    "key_prefix": "org/acme",
                }
            ],
        }
    )
    runtime_config = RuntimeWebPluginConfig.from_settings(settings)
    catalog = create_catalog_service()
    from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

    policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime_config)
    registry = OperatorProfileRegistry(policy=policy, settings=runtime_config)
    plugin_id = PluginId("transform", "aws_textract_document_analysis")
    unrestricted = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    snapshot = PluginAvailabilitySnapshot.create(
        policy_hash=policy.policy_hash,
        principal_scope="local:alice",
        available=unrestricted.available,
        unavailable=(),
        selected=unrestricted.selected,
        usable_profile_aliases=((plugin_id, ("acceptance-docs",)),),
        selected_profile_aliases=((plugin_id, "acceptance-docs"),),
        binding_generation_fingerprint="textract-profile-lowering-generation",
    )
    return registry, snapshot, catalog


def _textract_node_state() -> object:
    from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata

    node = NodeSpec(
        id="textract_1",
        node_type="transform",
        plugin="aws_textract_document_analysis",
        input="transform_in",
        on_success="results",
        on_error="discard",
        options={
            "profile": "acceptance-docs",
            "key_field": "document_key",
            "feature_types": ["FORMS"],
            "text_field": "textract_text",
            "schema": {"mode": "observed"},
        },
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )
    return CompositionState(source=None, nodes=(node,), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def test_validate_plugin_policy_lowers_profiled_textract_node_and_threads_identity() -> None:
    from typing import cast

    from elspeth.contracts.aws_textract import textract_profiled_binding_fingerprint
    from elspeth.web.catalog.protocol import CatalogService
    from elspeth.web.composer.state import CompositionState
    from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
    from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry
    from elspeth.web.plugin_policy.validation import validate_plugin_policy

    registry, snapshot, catalog = _textract_policy_context()
    state = _textract_node_state()

    result = validate_plugin_policy(
        cast(CompositionState, state),
        snapshot=cast(PluginAvailabilitySnapshot, snapshot),
        profile_registry=cast(OperatorProfileRegistry, registry),
        catalog=cast(CatalogService, catalog),
    )

    assert result.findings == ()
    executable_options = dict(result.executable_state.nodes[0].options)
    assert executable_options["bucket"] == "operator-private-bucket-marker"
    assert executable_options["key_prefix"] == "org/acme"
    assert executable_options["region"] == "ap-southeast-1"
    assert executable_options["auth_mode"] == "default_chain"
    assert "profile" not in executable_options
    assert dict(cast(CompositionState, state).nodes[0].options)["profile"] == "acceptance-docs"
    assert "bucket" not in dict(cast(CompositionState, state).nodes[0].options)

    assert len(result.profiled_textract_audit_identities) == 1
    node_id, identity = result.profiled_textract_audit_identities[0]
    assert node_id == "textract_1"
    assert identity.profile_alias == "acceptance-docs"
    assert identity.binding_fingerprint == textract_profiled_binding_fingerprint(
        bucket="operator-private-bucket-marker",
        region="ap-southeast-1",
        key_prefix="org/acme",
    )
    assert result.profiled_s3_audit_identities == ()


def test_validate_plugin_policy_rejects_s3_identity_from_a_non_s3_component(monkeypatch) -> None:
    from typing import cast

    import pytest as _pytest

    from elspeth.contracts.aws_s3 import S3ProfiledAuditIdentity
    from elspeth.web.catalog.protocol import CatalogService
    from elspeth.web.composer.state import CompositionState
    from elspeth.web.plugin_policy import profiles as profiles_module
    from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
    from elspeth.web.plugin_policy.profiles import LoweredPluginConfig, OperatorProfileRegistry
    from elspeth.web.plugin_policy.validation import validate_plugin_policy

    registry, snapshot, catalog = _textract_policy_context()
    state = _textract_node_state()
    impostor_identity = S3ProfiledAuditIdentity(
        profile_alias="acceptance-docs",
        relative_key="fabricated.csv",
        binding_fingerprint="0" * 64,
    )

    def lower_with_wrong_kind(self: object, alias: str, safe_options: dict[str, object]) -> LoweredPluginConfig:
        return LoweredPluginConfig(
            executable_options={"bucket": "b", **safe_options},
            audit_safe_options={"profile": alias, **safe_options},
            profiled_s3_audit_identity=impostor_identity,
        )

    monkeypatch.setattr(profiles_module._TextractProfileResolver, "lower_options", lower_with_wrong_kind)

    with _pytest.raises(TypeError, match="non-S3 source"):
        validate_plugin_policy(
            cast(CompositionState, state),
            snapshot=cast(PluginAvailabilitySnapshot, snapshot),
            profile_registry=cast(OperatorProfileRegistry, registry),
            catalog=cast(CatalogService, catalog),
        )


def test_validate_plugin_policy_rejects_textract_identity_from_a_non_textract_component(monkeypatch) -> None:
    from typing import cast

    import pytest as _pytest

    from elspeth.contracts.aws_textract import TextractProfiledAuditIdentity, textract_profiled_binding_fingerprint
    from elspeth.web.catalog.protocol import CatalogService
    from elspeth.web.composer.state import CompositionState, PipelineMetadata, SourceSpec
    from elspeth.web.config import WebSettings
    from elspeth.web.dependencies import create_catalog_service
    from elspeth.web.plugin_policy import profiles as profiles_module
    from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
    from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId
    from elspeth.web.plugin_policy.profiles import LoweredPluginConfig, OperatorProfileRegistry, RuntimeWebPluginConfig
    from elspeth.web.plugin_policy.validation import validate_plugin_policy

    settings = WebSettings.model_validate(
        {
            "composer_max_composition_turns": 4,
            "composer_max_discovery_turns": 4,
            "composer_timeout_seconds": 60,
            "composer_rate_limit_per_minute": 20,
            "shareable_link_signing_key": b"0123456789abcdef0123456789abcdef",
            "plugin_allowlist": ["source:aws_s3"],
            "deployment_aws_region": "ap-southeast-1",
            "aws_s3_source_profiles": [{"alias": "demo-input", "bucket": "operator-private-bucket-marker", "prefix": "incoming"}],
        }
    )
    runtime_config = RuntimeWebPluginConfig.from_settings(settings)
    catalog = create_catalog_service()
    from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

    policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime_config)
    registry = OperatorProfileRegistry(policy=policy, settings=runtime_config)
    source_id = PluginId("source", "aws_s3")
    unrestricted = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    snapshot = PluginAvailabilitySnapshot.create(
        policy_hash=policy.policy_hash,
        principal_scope="local:alice",
        available=unrestricted.available,
        unavailable=(),
        selected=unrestricted.selected,
        usable_profile_aliases=((source_id, ("demo-input",)),),
        selected_profile_aliases=((source_id, "demo-input"),),
        binding_generation_fingerprint="s3-profile-lowering-generation",
    )
    state = CompositionState(
        source=SourceSpec(
            plugin="aws_s3",
            on_success="unused",
            options={"profile": "demo-input", "key": "records/input.csv", "format": "csv", "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )
    impostor_identity = TextractProfiledAuditIdentity(
        profile_alias="demo-input",
        binding_fingerprint=textract_profiled_binding_fingerprint(bucket="b", region="ap-southeast-1", key_prefix=None),
    )

    def lower_with_wrong_kind(self: object, alias: str, safe_options: dict[str, object]) -> LoweredPluginConfig:
        return LoweredPluginConfig(
            executable_options={"bucket": "b", **safe_options},
            audit_safe_options={"profile": alias, **safe_options},
            profiled_textract_audit_identity=impostor_identity,
        )

    monkeypatch.setattr(profiles_module._S3SourceProfileResolver, "lower_options", lower_with_wrong_kind)

    with _pytest.raises(TypeError, match="non-Textract"):
        validate_plugin_policy(
            state,
            snapshot=cast(PluginAvailabilitySnapshot, snapshot),
            profile_registry=cast(OperatorProfileRegistry, registry),
            catalog=cast(CatalogService, catalog),
        )


def test_plugin_policy_returns_typed_finding_for_malformed_rehydrated_source() -> None:
    from typing import Any, cast

    from elspeth.web.catalog.protocol import CatalogService
    from elspeth.web.composer.state import CompositionState, PipelineMetadata, SourceSpec
    from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
    from elspeth.web.plugin_policy.validation import validate_plugin_policy

    malformed = SourceSpec(
        plugin=[],  # type: ignore[arg-type]
        on_success=[],  # type: ignore[arg-type]
        options=[],  # type: ignore[arg-type]
        on_validation_failure=[],  # type: ignore[arg-type]
    )
    state = CompositionState(
        sources={
            "rows": SourceSpec(
                plugin=[],  # type: ignore[arg-type]
                on_success="unused",
                options={},
                on_validation_failure="discard",
            ),
            7: malformed,  # type: ignore[dict-item]
        },
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )
    snapshot = PluginAvailabilitySnapshot.create(
        policy_hash="malformed-source-policy",
        principal_scope="local:test",
        available=frozenset(),
        unavailable=(),
        selected=(),
        usable_profile_aliases=(),
        selected_profile_aliases=(),
        binding_generation_fingerprint="malformed-source-generation",
    )

    result = validate_plugin_policy(
        state,
        snapshot=snapshot,
        profile_registry=None,
        catalog=cast(CatalogService, cast(Any, object())),
    )

    assert [(finding.component_id, finding.component_type, finding.stage) for finding in result.findings] == [
        ("source:rows", "source", "plugin_enablement"),
        ("source:<invalid>", "source", "plugin_enablement"),
    ]


# ── required_control_coverage messages ───────────────────────────────────────


def _coverage_message(reason: str, *, uncovered_stream: str | None, input_role: bool = False) -> str:
    from elspeth.contracts.plugin_capabilities import ControlRole, PluginCapability
    from elspeth.web.plugin_policy.coverage import ControlCoverageFinding
    from elspeth.web.plugin_policy.validation import _control_coverage_finding

    finding = _control_coverage_finding(
        ControlCoverageFinding(
            component_id="judge",
            component_type="transform",
            capability=PluginCapability.PROMPT_SHIELD if input_role else PluginCapability.CONTENT_SAFETY,
            role=ControlRole.INPUT if input_role else ControlRole.OUTPUT,
            reason=reason,  # type: ignore[arg-type]
            uncovered_stream=uncovered_stream,
        )
    )
    assert finding.stage == "required_control_coverage"
    assert finding.error_code == "required_control_coverage"
    assert finding.component_id == "judge"
    assert finding.component_type == "transform"
    return finding.message


def test_error_route_coverage_message_names_the_conflict_and_the_one_repair() -> None:
    """The on_error rejection reads as a contradiction unless it explains itself.

    The planner is taught to quarantine failed rows to a sink; a required
    output control then rejects the pipeline for doing exactly that. The
    message must name the node, the offending error route, the control, why
    that route is an independent write path, and the single authorable
    repair — on_error='discard'.
    """
    message = _coverage_message("output_error_route_not_post_dominated", uncovered_stream="quarantine")

    assert "judge" in message
    assert "quarantine" in message
    assert "on_error" in message
    assert "content_safety" in message
    assert "independent output path" in message
    assert "'discard'" in message


def test_error_route_coverage_message_never_offers_the_unauthorable_repair() -> None:
    """Interposing the control on the error branch is rejected by the graph.

    ``on_error`` may only name a sink or 'discard'
    (``core/dag/builder.py:1108``), so advising a control transform on the
    error branch would swap this rejection for
    ``transform_on_error_unknown_sink``. The message must say the interposition
    is impossible, never offer it as an option.
    """
    message = _coverage_message("output_error_route_not_post_dominated", uncovered_stream="quarantine")

    assert "no control can be interposed on an error branch" in message
    assert "may only name a sink or 'discard'" in message


def test_non_error_route_coverage_keeps_the_general_message() -> None:
    message = _coverage_message("output_not_post_dominated", uncovered_stream=None)

    assert message == "Node 'judge' is not covered by the required 'content_safety' output control."


def test_input_role_coverage_keeps_the_general_message() -> None:
    message = _coverage_message("input_not_dominated", uncovered_stream=None, input_role=True)

    assert message == "Node 'judge' is not covered by the required 'prompt_shield' input control."
    assert "on_error" not in message


def test_unprovable_input_without_dominating_control_does_not_claim_wiring_is_correct() -> None:
    from elspeth.contracts.plugin_capabilities import ControlRole, PluginCapability
    from elspeth.web.plugin_policy.coverage import ControlCoverageFinding
    from elspeth.web.plugin_policy.validation import _control_coverage_finding

    finding = _control_coverage_finding(
        ControlCoverageFinding(
            component_id="judge",
            component_type="transform",
            capability=PluginCapability.PROMPT_SHIELD,
            role=ControlRole.INPUT,
            reason="input_fields_unprovable",
        )
    )

    assert "control upstream" not in finding.message
    assert "wiring itself is correct" not in finding.message
    assert "fields: 'all'" in finding.message
    assert finding.suggestion is not None
    assert "wiring is already right" not in finding.suggestion
    assert "fields: 'all'" in finding.suggestion


def test_error_route_coverage_message_routes_retention_to_the_operator() -> None:
    """Retaining failed rows is real, but it is not an authoring change.

    ``on_error: <quarantine sink>`` builds and runs — only the *required*
    control mode rejects it. Without naming the operator-side escape hatch the
    author reads the requirement as a bug and re-attempts quarantine shapes.
    """
    message = _coverage_message("output_error_route_not_post_dominated", uncovered_stream="quarantine")

    assert "operator decision" in message
    assert "'recommend'" in message
    assert "content hash" in message


def test_source_validation_failure_coverage_preserves_source_identity_and_repair() -> None:
    from elspeth.contracts.plugin_capabilities import ControlRole, PluginCapability
    from elspeth.web.plugin_policy.coverage import ControlCoverageFinding
    from elspeth.web.plugin_policy.validation import _control_coverage_finding

    finding = _control_coverage_finding(
        ControlCoverageFinding(
            component_id="source:briefing",
            component_type="source",
            capability=PluginCapability.CONTENT_SAFETY,
            role=ControlRole.OUTPUT,
            reason="output_validation_failure_route_not_post_dominated",
            uncovered_stream="quarantine",
        )
    )

    assert finding.component_id == "source:briefing"
    assert finding.component_type == "source"
    assert "on_validation_failure" in finding.message
    assert "quarantine" in finding.message
    assert finding.suggestion is not None
    assert "'discard'" in finding.suggestion
    assert "on_error" not in finding.suggestion


# ── required_control_coverage suggestions (keyed on reason, not stage) ───────


def _coverage_suggestion(reason: str, *, uncovered_stream: str | None = None, input_role: bool = False) -> str:
    from elspeth.contracts.plugin_capabilities import ControlRole, PluginCapability
    from elspeth.web.plugin_policy.coverage import ControlCoverageFinding
    from elspeth.web.plugin_policy.validation import _control_coverage_finding

    finding = _control_coverage_finding(
        ControlCoverageFinding(
            component_id="judge",
            component_type="transform",
            capability=PluginCapability.PROMPT_SHIELD if input_role else PluginCapability.CONTENT_SAFETY,
            role=ControlRole.INPUT if input_role else ControlRole.OUTPUT,
            reason=reason,  # type: ignore[arg-type]
            uncovered_stream=uncovered_stream,
        )
    )
    assert finding.suggestion is not None
    return finding.suggestion


def test_error_route_coverage_suggestion_names_the_on_error_repair() -> None:
    """The output-role error-route diagnosis has exactly one authorable repair."""
    suggestion = _coverage_suggestion("output_error_route_not_post_dominated", uncovered_stream="quarantine")

    assert "on_error" in suggestion
    assert "'discard'" in suggestion
    assert "'recommend'" in suggestion
    # The error-route advice must never send the author upstream — that is
    # the input-domination repair, and the graph rejects a control on an
    # error branch.
    assert "upstream" not in suggestion


def test_input_domination_coverage_suggestion_interposes_the_shield_upstream() -> None:
    """M2: an input-role (prompt_shield) finding must NOT get on_error advice.

    The pre-fix stage-keyed suggestion told every coverage failure to set
    on_error to 'discard' — a repair that cannot address input domination.
    The truthful repair is interposing the shield between the producers and
    the node.
    """
    suggestion = _coverage_suggestion("input_not_dominated", input_role=True)

    assert "upstream" in suggestion
    assert "Interpose" in suggestion
    assert "'recommend'" in suggestion
    assert "on_error" not in suggestion
    assert "'discard'" not in suggestion


def test_generic_output_coverage_suggestion_wires_the_control_before_sinks() -> None:
    suggestion = _coverage_suggestion("output_not_post_dominated")

    assert "every path" in suggestion
    assert "before any sink" in suggestion
    assert "'recommend'" in suggestion
    assert "on_error" not in suggestion


def test_coverage_suggestions_are_total_over_reasons() -> None:
    """A new ControlCoverageFinding.reason must decide its remediation too."""
    from typing import get_args, get_type_hints

    from elspeth.web.plugin_policy.coverage import ControlCoverageFinding
    from elspeth.web.plugin_policy.validation import _CONTROL_COVERAGE_SUGGESTIONS

    reasons = get_args(get_type_hints(ControlCoverageFinding)["reason"])
    assert set(_CONTROL_COVERAGE_SUGGESTIONS) == set(reasons)


def test_scope_mismatch_suggestion_repairs_the_control_scope_not_the_wiring() -> None:
    """A scope mismatch on a correct topology must not be sent upstream.

    When the node's protected fields and the dominating control's scanned
    fields are BOTH known, the control already dominates the input — the
    coverage walk only found the two sets disjoint. "Interpose the control
    upstream" names a repair for wiring that is already right, and a planner
    that follows it re-emits the same topology and draws the same rejection.
    """
    from elspeth.contracts.plugin_capabilities import ControlRole, PluginCapability
    from elspeth.web.plugin_policy.coverage import ControlCoverageFinding
    from elspeth.web.plugin_policy.validation import _control_coverage_finding

    finding = _control_coverage_finding(
        ControlCoverageFinding(
            component_id="judge",
            component_type="transform",
            capability=PluginCapability.PROMPT_SHIELD,
            role=ControlRole.INPUT,
            reason="input_not_dominated",
            protected_fields=("untrusted_prompt",),
            scanned_fields=("benign_label",),
        )
    )

    assert finding.suggestion is not None
    assert "Interpose" not in finding.suggestion
    assert "upstream" not in finding.suggestion
    assert "fields" in finding.suggestion
    assert "untrusted_prompt" in finding.suggestion
    assert "benign_label" in finding.suggestion
    assert "'recommend'" in finding.suggestion
