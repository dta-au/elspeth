from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.plugin_capabilities import WebConfigAuthority
from elspeth.engine.orchestrator.preflight import check_config_value_sources
from elspeth.plugins.infrastructure.discovery import create_dynamic_hookimpl
from elspeth.plugins.infrastructure.manager import PluginManager
from elspeth.plugins.sources.llm import LLMSource
from elspeth.plugins.transforms.llm.providers.azure import AzureOpenAIConfig
from elspeth.plugins.transforms.llm.providers.gateway import GatewayConfig
from elspeth.web.catalog.schemas import PluginSchemaInfo
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.config import WebSettings
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.models import PluginId
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig


def _settings(**overrides: object) -> WebSettings:
    values: dict[str, object] = {
        "composer_max_composition_turns": 4,
        "composer_max_discovery_turns": 4,
        "composer_timeout_seconds": 60,
        "composer_rate_limit_per_minute": 20,
        "shareable_link_signing_key": b"0123456789abcdef0123456789abcdef",
    }
    values.update(overrides)
    return WebSettings.model_validate(values)


def _isolated_manager_with_llm_source() -> PluginManager:
    class _CompilerLLMSource(LLMSource):
        determinism = LLMSource.determinism
        source_file_hash = "sha256:0123456789abcdef"

    manager = PluginManager()
    manager.register_builtin_plugins()
    if all(source.name != "llm" for source in manager.get_sources()):
        manager.register(create_dynamic_hookimpl([_CompilerLLMSource], "elspeth_get_source"))
    return manager


def test_openrouter_profile_requires_explicit_scoped_credential() -> None:
    with pytest.raises(ValidationError):
        _settings(llm_profiles={"tutorial": {"provider": "openrouter", "model": "openai/gpt-5-mini"}}, default_llm_profile="tutorial")


@pytest.mark.parametrize(
    "profile",
    [
        {
            "provider": "bedrock",
            "model": "bedrock/apac.amazon.nova-micro-v1:0",
            "timeout_seconds": 17.5,
        },
        {
            "provider": "azure",
            "model": "private-model",
            "credential_scope": "server",
            "credential_ref": "AZURE_OPENAI_API_KEY",
            "endpoint": "https://example.openai.azure.com",
            "deployment_name": "deployment",
            "timeout_seconds": 60.0,
        },
        {
            "provider": "azure",
            "model": "private-model",
            "credential_scope": "server",
            "credential_ref": "AZURE_OPENAI_API_KEY",
            "endpoint": "https://example.openai.azure.com",
            "deployment_name": "deployment",
            "region_name": "australiaeast",
        },
    ],
)
def test_llm_profiles_reject_provider_options_runtime_cannot_honor(profile: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="does not support"):
        _settings(llm_profiles={"invalid": profile}, default_llm_profile="invalid")


def test_bedrock_profile_is_keyless_and_uses_canonical_provider_registry() -> None:
    settings = _settings(
        llm_profiles={
            "tutorial": {
                "provider": "bedrock",
                "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
                "region_name": "ap-southeast-2",
            }
        },
        default_llm_profile="tutorial",
    )
    runtime = RuntimeWebPluginConfig.from_settings(settings)

    profile = runtime.llm_profiles[0][1]
    assert profile.provider == "bedrock"
    assert profile.credential_scope is None
    assert profile.credential_ref is None
    assert "credential" not in repr(profile)


def test_llm_public_profile_schema_accepts_observed_schema_without_fields() -> None:
    settings = _settings(
        llm_profiles={
            "tutorial": {
                "provider": "bedrock",
                "model": "bedrock/apac.amazon.nova-micro-v1:0",
                "region_name": "ap-southeast-1",
            }
        },
        default_llm_profile="tutorial",
    )
    runtime = RuntimeWebPluginConfig.from_settings(settings)
    policy = compile_web_plugin_policy(registry=_isolated_manager_with_llm_source(), settings=runtime)
    profiles = OperatorProfileRegistry(policy=policy, settings=runtime)
    public_schema = profiles.public_schema(
        PluginId("transform", "llm"),
        create_catalog_service().get_schema("transform", "llm"),
        available_aliases=("tutorial",),
    ).json_schema

    options = {
        "profile": "tutorial",
        "prompt_template": "{{ row.text }}",
        "schema": {
            "mode": "observed",
            "required_fields": ["text"],
            "guaranteed_fields": ["text", "llm_response"],
        },
    }

    assert list(Draft202012Validator(public_schema).iter_errors(options)) == []


def test_runtime_conversion_is_frozen_and_canonical() -> None:
    settings = _settings(
        plugin_allowlist=("sink:database",),
        llm_profiles={
            "tutorial": {
                "provider": "openrouter",
                "model": "openai/gpt-5-mini",
                "credential_scope": "server",
                "credential_ref": "TOP_SECRET_MARKER",
            }
        },
        default_llm_profile="tutorial",
    )
    runtime = RuntimeWebPluginConfig.from_settings(settings)

    assert runtime.plugin_allowlist == ("sink:database",)
    assert runtime.llm_profiles[0][0] == "tutorial"
    assert "TOP_SECRET_MARKER" not in repr(runtime)
    with pytest.raises(FrozenInstanceError):
        runtime.default_llm_profile = "changed"  # type: ignore[misc]


def test_profile_reprs_hide_provider_and_provider_specific_settings() -> None:
    settings = _settings(
        llm_profiles={
            "private-binding": {
                "provider": "azure",
                "model": "PRIVATE_DEPLOYMENT_MARKER",
                "credential_scope": "server",
                "credential_ref": "PRIVATE_CREDENTIAL_MARKER",
                "endpoint": "https://private-endpoint-marker.example.com",
                "deployment_name": "PRIVATE_DEPLOYMENT_MARKER",
                "api_version": "PRIVATE_API_VERSION_MARKER",
                "max_tokens": 12345,
            }
        },
        default_llm_profile="private-binding",
    )

    settings_repr = repr(settings.llm_profiles["private-binding"])
    runtime_repr = repr(RuntimeWebPluginConfig.from_settings(settings).llm_profiles[0][1])

    for marker in (
        "azure",
        "PRIVATE_CREDENTIAL_MARKER",
        "private-endpoint-marker",
        "PRIVATE_DEPLOYMENT_MARKER",
        "PRIVATE_API_VERSION_MARKER",
        "12345",
    ):
        assert marker not in settings_repr
        assert marker not in runtime_repr


def test_profile_aliases_are_opaque_canonical_identifiers() -> None:
    with pytest.raises(ValidationError):
        _settings(
            llm_profiles={
                "Not Valid": {
                    "provider": "bedrock",
                    "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
                }
            },
            default_llm_profile="Not Valid",
        )


def test_runtime_conversion_consumes_every_universal_setting_field() -> None:
    settings_fields = {
        "plugin_allowlist",
        "plugin_preferences",
        "plugin_control_modes",
        "llm_profiles",
        "default_llm_profile",
        "bedrock_guardrail_profiles",
        "bedrock_guardrail_default_profiles",
        "deployment_aws_region",
    }
    runtime_fields = set(RuntimeWebPluginConfig.__dataclass_fields__)

    assert settings_fields == runtime_fields


def test_textract_deployment_profile_exposes_only_alias_and_ordinary_options() -> None:
    runtime = RuntimeWebPluginConfig.from_settings(_settings(deployment_aws_region="ap-southeast-1"))
    policy = compile_web_plugin_policy(
        registry=_isolated_manager_with_llm_source(),
        settings=RuntimeWebPluginConfig.from_settings(
            _settings(
                deployment_aws_region="ap-southeast-1",
                plugin_allowlist=("transform:aws_textract_document_analysis",),
            )
        ),
    )
    registry = OperatorProfileRegistry(policy=policy, settings=runtime)
    plugin_id = PluginId("transform", "aws_textract_document_analysis")

    public = registry.public_schema(
        plugin_id,
        create_catalog_service().get_schema("transform", "aws_textract_document_analysis"),
        available_aliases=("deployment",),
    )

    properties = public.json_schema["properties"]
    assert properties["profile"]["enum"] == ["deployment"]
    assert {"bucket_field", "key_field", "feature_types", "text_field", "schema"} <= set(properties)
    assert not set(properties) & {
        "region",
        "auth_mode",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
    }
    assert set(public.json_schema["required"]) == {
        "profile",
        "bucket_field",
        "key_field",
        "feature_types",
        "schema",
    }
    assert public.json_schema["additionalProperties"] is False
    assert public.secret_requirements == ()
    assert "SchemaConfig" in public.json_schema["$defs"]


def test_textract_deployment_profile_lowers_and_rotates_binding_with_region() -> None:
    def registry_for(region: str) -> OperatorProfileRegistry:
        runtime = RuntimeWebPluginConfig.from_settings(_settings(deployment_aws_region=region))
        policy = compile_web_plugin_policy(
            registry=_isolated_manager_with_llm_source(),
            settings=RuntimeWebPluginConfig.from_settings(
                _settings(
                    deployment_aws_region=region,
                    plugin_allowlist=("transform:aws_textract_document_analysis",),
                )
            ),
        )
        return OperatorProfileRegistry(policy=policy, settings=runtime)

    plugin_id = PluginId("transform", "aws_textract_document_analysis")
    safe = {
        "bucket_field": "bucket",
        "key_field": "key",
        "feature_types": ["FORMS"],
        "text_field": "text",
        "schema": {"mode": "observed"},
    }
    first = registry_for("ap-southeast-1")
    second = registry_for("eu-west-1")
    lowered = first.lower_options(plugin_id, alias="deployment", safe_options=safe)

    assert deep_thaw(lowered.executable_options) == {
        **safe,
        "region": "ap-southeast-1",
        "auth_mode": "default_chain",
    }
    assert deep_thaw(lowered.audit_safe_options) == {"profile": "deployment", **safe}
    assert first.profile_availability(plugin_id, principal="local:alice", inventory=cast(Any, object()))[0].generation != (
        second.profile_availability(plugin_id, principal="local:alice", inventory=cast(Any, object()))[0].generation
    )


def test_textract_deployment_profile_requires_local_boto3(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        "elspeth.web.plugin_policy.profiles.importlib.util.find_spec",
        lambda name: None if name == "boto3" else real_find_spec(name),
    )
    runtime = RuntimeWebPluginConfig.from_settings(_settings(deployment_aws_region="ap-southeast-1"))
    policy = compile_web_plugin_policy(registry=_isolated_manager_with_llm_source(), settings=runtime)
    registry = OperatorProfileRegistry(policy=policy, settings=runtime)

    assert (
        registry.profile_availability(
            PluginId("transform", "aws_textract_document_analysis"),
            principal="local:alice",
            inventory=cast(Any, object()),
        )
        == ()
    )


@pytest.mark.parametrize(
    "private",
    ["region", "auth_mode", "aws_access_key_id", "aws_secret_access_key", "aws_session_token"],
)
def test_textract_deployment_profile_rejects_private_or_mixed_options(private: str) -> None:
    runtime = RuntimeWebPluginConfig.from_settings(_settings(deployment_aws_region="ap-southeast-1"))
    policy = compile_web_plugin_policy(
        registry=_isolated_manager_with_llm_source(),
        settings=RuntimeWebPluginConfig.from_settings(
            _settings(
                deployment_aws_region="ap-southeast-1",
                plugin_allowlist=("transform:aws_textract_document_analysis",),
            )
        ),
    )
    registry = OperatorProfileRegistry(policy=policy, settings=runtime)

    with pytest.raises(ValueError, match="private_profile_option"):
        registry.lower_options(
            PluginId("transform", "aws_textract_document_analysis"),
            alias="deployment",
            safe_options={"bucket_field": "bucket", private: "attacker"},
        )


def test_llm_profiles_without_a_standard_profile_start_in_degraded_mode() -> None:
    """Ordinary authoring remains available while tutorial readiness is degraded."""
    profiles = {
        "standard": {
            "provider": "bedrock",
            "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
            "region_name": "ap-southeast-2",
        },
        "fast": {
            "provider": "bedrock",
            "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
            "region_name": "ap-southeast-2",
        },
    }

    degraded = _settings(llm_profiles=profiles)
    assert degraded.llm_profiles
    assert degraded.default_llm_profile is None

    settings = _settings(llm_profiles=profiles, default_llm_profile="standard")
    assert settings.default_llm_profile == "standard"


def test_no_llm_profiles_needs_no_standard_profile() -> None:
    """A deployment with no LLM authoring has no standard model to designate.

    The gate above must not turn "this deployment offers no llm nodes" into a
    boot failure — that posture is supported, and the first-run tutorial is
    simply disabled.
    """
    settings = _settings()

    assert settings.llm_profiles == {}
    assert settings.default_llm_profile is None


def test_bedrock_profiles_require_explicit_default_when_plugin_has_multiple_profiles() -> None:
    profiles = (
        {
            "alias": "first",
            "plugin": "aws_bedrock_prompt_shield",
            "guardrail_identifier": "firstguardrail",
            "guardrail_version": "1",
            "region": "us-east-1",
        },
        {
            "alias": "second",
            "plugin": "aws_bedrock_prompt_shield",
            "guardrail_identifier": "secondguardrail",
            "guardrail_version": "2",
            "region": "us-east-1",
        },
    )

    with pytest.raises(ValidationError):
        _settings(bedrock_guardrail_profiles=profiles)

    settings = _settings(
        bedrock_guardrail_profiles=profiles,
        bedrock_guardrail_default_profiles={"aws_bedrock_prompt_shield": "second"},
    )
    assert settings.bedrock_guardrail_default_profiles["aws_bedrock_prompt_shield"] == "second"


def test_bedrock_profile_aliases_are_unique_across_plugins() -> None:
    shared = {
        "alias": "shared",
        "guardrail_identifier": "privateguardrail",
        "guardrail_version": "1",
        "region": "us-east-1",
    }
    with pytest.raises(ValidationError):
        _settings(
            bedrock_guardrail_profiles=(
                {**shared, "plugin": "aws_bedrock_prompt_shield"},
                {**shared, "plugin": "aws_bedrock_content_safety"},
            )
        )


def test_bedrock_profile_resolver_exposes_only_alias_and_safe_options() -> None:
    runtime = RuntimeWebPluginConfig.from_settings(
        _settings(
            bedrock_guardrail_profiles=(
                {
                    "alias": "prompt-default",
                    "plugin": "aws_bedrock_prompt_shield",
                    "guardrail_identifier": "privateguardrail",
                    "guardrail_version": "7",
                    "region": "us-east-1",
                },
            ),
        )
    )
    policy = compile_web_plugin_policy(registry=_isolated_manager_with_llm_source(), settings=runtime)
    registry = OperatorProfileRegistry(policy=policy, settings=runtime)
    plugin_id = PluginId("transform", "aws_bedrock_prompt_shield")
    full = PluginSchemaInfo(
        name="aws_bedrock_prompt_shield",
        plugin_type="transform",
        description="test schema",
        json_schema={
            "type": "object",
            "properties": {
                "guardrail_identifier": {"type": "string"},
                "guardrail_version": {"type": "string"},
                "region": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "string"}},
                "schema": {"type": "object"},
            },
            "required": ["guardrail_identifier", "guardrail_version", "region", "fields", "schema"],
            "additionalProperties": False,
        },
        knob_schema={"fields": []},
        web_config_authority=WebConfigAuthority.OPERATOR_PROFILED,
    )

    public = registry.public_schema(plugin_id, full, available_aliases=("prompt-default",))
    rendered = public.model_dump_json()
    assert '"profile"' in rendered
    assert '"fields"' in rendered
    assert '"schema"' in rendered
    for private in ("guardrail_identifier", "guardrail_version", "region", "endpoint", "credential"):
        assert private not in rendered

    lowered = registry.lower_options(
        plugin_id,
        alias="prompt-default",
        safe_options={"fields": ["prompt"], "schema": {"mode": "observed"}},
    )
    assert deep_thaw(lowered.audit_safe_options) == {
        "profile": "prompt-default",
        "fields": ["prompt"],
        "schema": {"mode": "observed"},
    }
    assert lowered.executable_options["guardrail_identifier"] == "privateguardrail"


def test_bedrock_profile_resolver_returns_only_an_authorized_exact_approved_binding() -> None:
    runtime = RuntimeWebPluginConfig.from_settings(
        _settings(
            plugin_allowlist=("transform:aws_bedrock_prompt_shield",),
            bedrock_guardrail_profiles=(
                {
                    "alias": "prompt-approved",
                    "plugin": "aws_bedrock_prompt_shield",
                    "guardrail_identifier": "privateguardrail",
                    "guardrail_version": "7",
                    "region": "us-east-1",
                },
            ),
        )
    )
    policy = compile_web_plugin_policy(registry=_isolated_manager_with_llm_source(), settings=runtime)
    registry = OperatorProfileRegistry(policy=policy, settings=runtime)
    plugin_id = PluginId("transform", "aws_bedrock_prompt_shield")

    profile = registry.approved_bedrock_guardrail_profile(plugin_id, alias="prompt-approved")

    assert profile.alias == "prompt-approved"
    assert profile.plugin == "aws_bedrock_prompt_shield"
    assert profile.guardrail_version == "7"
    with pytest.raises(ValueError, match="profile_unavailable"):
        registry.approved_bedrock_guardrail_profile(plugin_id, alias="wrong")
    with pytest.raises(ValueError, match="profile_unavailable"):
        registry.approved_bedrock_guardrail_profile(PluginId("transform", "llm"), alias="prompt-approved")


def test_bedrock_profile_resolver_preserves_referenced_public_schema_definitions() -> None:
    runtime = RuntimeWebPluginConfig.from_settings(
        _settings(
            bedrock_guardrail_profiles=(
                {
                    "alias": "prompt-default",
                    "plugin": "aws_bedrock_prompt_shield",
                    "guardrail_identifier": "privateguardrail",
                    "guardrail_version": "7",
                    "region": "us-east-1",
                },
            ),
        )
    )
    policy = compile_web_plugin_policy(registry=_isolated_manager_with_llm_source(), settings=runtime)
    registry = OperatorProfileRegistry(policy=policy, settings=runtime)

    public = registry.public_schema(
        PluginId("transform", "aws_bedrock_prompt_shield"),
        create_catalog_service().get_schema("transform", "aws_bedrock_prompt_shield"),
        available_aliases=("prompt-default",),
    )

    assert public.json_schema["properties"]["schema"]["$ref"] == "#/$defs/SchemaConfig"
    assert "SchemaConfig" in public.json_schema["$defs"]


def test_bedrock_profile_resolver_rejects_private_or_mixed_options() -> None:
    runtime = RuntimeWebPluginConfig.from_settings(
        _settings(
            bedrock_guardrail_profiles=(
                {
                    "alias": "prompt-default",
                    "plugin": "aws_bedrock_prompt_shield",
                    "guardrail_identifier": "privateguardrail",
                    "guardrail_version": "7",
                    "region": "us-east-1",
                },
            ),
        )
    )
    policy = compile_web_plugin_policy(registry=_isolated_manager_with_llm_source(), settings=runtime)
    registry = OperatorProfileRegistry(policy=policy, settings=runtime)

    with pytest.raises(ValueError, match="private_profile_option"):
        registry.lower_options(
            PluginId("transform", "aws_bedrock_prompt_shield"),
            alias="prompt-default",
            safe_options={"fields": ["prompt"], "guardrail_identifier": "attacker"},
        )


def _profile_registry() -> OperatorProfileRegistry:
    runtime = RuntimeWebPluginConfig.from_settings(
        _settings(
            llm_profiles={
                "tutorial": {
                    "provider": "openrouter",
                    "model": "openai/gpt-5-mini",
                    "credential_scope": "server",
                    "credential_ref": "OPENROUTER_API_KEY",
                },
                "bedrock-task-role": {
                    "provider": "bedrock",
                    "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
                    "region_name": "ap-southeast-2",
                },
                "gateway-task": {
                    "provider": "gateway",
                    "model": "standard",
                    "credential_scope": "server",
                    "credential_ref": "GATEWAY_BEARER_TOKEN",
                    "endpoint": "https://gateway.example.com/v1",
                    "contract_major": 1,
                    "required_capabilities": ["text", "usage"],
                },
            },
            default_llm_profile="tutorial",
        )
    )
    policy = compile_web_plugin_policy(registry=_isolated_manager_with_llm_source(), settings=runtime)
    return OperatorProfileRegistry(policy=policy, settings=runtime)


def test_public_llm_schema_exposes_alias_not_private_provider_binding() -> None:
    full = create_catalog_service().get_schema("transform", "llm")
    public = _profile_registry().public_schema(
        PluginId("transform", "llm"),
        full,
        available_aliases=("tutorial",),
    )
    rendered = public.model_dump_json()

    assert '"profile"' in rendered
    assert '"tutorial"' in rendered
    for private_name in ("api_key", "base_url", "endpoint", "deployment_name", "region_name", '"provider"', '"model"'):
        assert private_name not in rendered


def test_public_llm_source_schema_is_component_specific() -> None:
    full = CatalogServiceImpl(_isolated_manager_with_llm_source()).get_schema("source", "llm")
    public = _profile_registry().public_schema(
        PluginId("source", "llm"),
        full,
        available_aliases=("tutorial",),
    )

    assert set(public.json_schema["properties"]) == {
        "profile",
        "schema",
        "prompt_template",
        "system_prompt",
        "temperature",
        "response_field",
        "on_validation_failure",
        "lookup",
    }
    assert set(public.json_schema["required"]) == {
        "profile",
        "schema",
        "prompt_template",
        "on_validation_failure",
    }
    assert public.json_schema["properties"]["profile"]["enum"] == ["tutorial"]
    Draft202012Validator.check_schema(public.json_schema)
    Draft202012Validator(public.json_schema).validate(
        {
            "profile": "tutorial",
            "schema": {"mode": "observed"},
            "prompt_template": "Write one briefing from {{ lookup.topic }}.",
            "response_field": "briefing",
            "on_validation_failure": "discard",
            "lookup": {"topic": "the audit"},
        }
    )


def test_public_llm_source_schema_excludes_transform_and_private_fields() -> None:
    full = CatalogServiceImpl(_isolated_manager_with_llm_source()).get_schema("source", "llm")
    public = _profile_registry().public_schema(
        PluginId("source", "llm"),
        full,
        available_aliases=("tutorial",),
    )
    rendered = public.model_dump_json()

    for excluded in (
        "queries",
        "required_input_fields",
        "interpretation_requirements",
        "provider",
        "model",
        "api_key",
        "base_url",
        "endpoint",
        "deployment_name",
        "region_name",
        "timeout_seconds",
        "prompt_template_source",
        "lookup_source",
        "resolved_prompt_template_hash",
    ):
        assert f'"{excluded}"' not in rendered


def test_profile_lowering_splits_executable_and_audit_safe_options() -> None:
    lowered = _profile_registry().lower_options(
        PluginId("transform", "llm"),
        alias="tutorial",
        safe_options={"prompt_template": "Summarise {{ row }}", "response_field": "summary"},
    )

    assert lowered.executable_options["provider"] == "openrouter"
    assert lowered.executable_options["model"] == "openai/gpt-5-mini"
    assert lowered.executable_options["api_key"] == {
        "secret_ref": "OPENROUTER_API_KEY",
        "secret_scope": "server",
    }
    assert deep_thaw(lowered.audit_safe_options) == {
        "profile": "tutorial",
        "prompt_template": "Summarise {{ row }}",
        "response_field": "summary",
    }
    assert "OPENROUTER_API_KEY" not in repr(lowered)


def test_llm_source_uses_internal_profile_resolver_without_public_discovery() -> None:
    registry = _profile_registry()
    source_id = PluginId("source", "llm")

    lowered = registry.lower_options(
        source_id,
        alias="bedrock-task-role",
        safe_options={
            "prompt_template": "Write one audit briefing.",
            "response_field": "briefing",
            "schema": {"mode": "observed"},
            "on_validation_failure": "discard",
        },
    )

    assert lowered.executable_options["provider"] == "bedrock"
    assert lowered.executable_options["model"] == "bedrock/anthropic.claude-3-haiku-20240307-v1:0"
    assert deep_thaw(lowered.audit_safe_options)["profile"] == "bedrock-task-role"
    assert "profile_alias" not in lowered.executable_options

    # Web attribution is carried by the authored audit-safe profile selector
    # and frozen policy selection, so executable provider config needs no
    # forgeable retained alias and remains directly constructible.
    source = LLMSource(cast(dict[str, Any], deep_thaw(lowered.executable_options)))
    assert "profile_alias" not in source.config
    assert source.provider_config.provider == "bedrock"


def test_llm_source_profile_alias_is_not_authorable_as_a_safe_option() -> None:
    registry = _profile_registry()

    with pytest.raises(ValueError, match="private_profile_option"):
        registry.lower_options(
            PluginId("source", "llm"),
            alias="bedrock-task-role",
            safe_options={
                "profile_alias": "forged",
                "prompt_template": "Write one audit briefing.",
                "schema": {"mode": "observed"},
                "on_validation_failure": "discard",
            },
        )


def test_llm_source_profile_resolver_fails_closed_for_unknown_alias_and_wrong_kind() -> None:
    registry = _profile_registry()
    safe_options: dict[str, object] = {
        "prompt_template": "Write one audit briefing.",
        "schema": {"mode": "observed"},
        "on_validation_failure": "discard",
    }

    with pytest.raises(ValueError, match="profile_unavailable"):
        registry.lower_options(
            PluginId("source", "llm"),
            alias="not-configured",
            safe_options=safe_options,
        )
    with pytest.raises(ValueError, match="plugin_has_no_operator_profile"):
        registry.lower_options(
            PluginId("sink", "llm"),
            alias="tutorial",
            safe_options=safe_options,
        )


@pytest.mark.parametrize(
    ("alias", "expected_aliases"),
    [
        ("tutorial", ("tutorial", "bedrock-task-role", "gateway-task")),
        (None, ()),
    ],
)
def test_llm_source_profile_availability_enumerates_only_configured_aliases(
    alias: str | None,
    expected_aliases: tuple[str, ...],
) -> None:
    registry = (
        _profile_registry()
        if alias is not None
        else OperatorProfileRegistry(
            policy=compile_web_plugin_policy(
                registry=_isolated_manager_with_llm_source(),
                settings=RuntimeWebPluginConfig.from_settings(_settings()),
            ),
            settings=RuntimeWebPluginConfig.from_settings(_settings()),
        )
    )

    class _Inventory:
        def server_generation(self, name: str) -> str | None:
            return "present" if name in {"OPENROUTER_API_KEY", "GATEWAY_BEARER_TOKEN"} else None

        def user_generation(self, principal: str, name: str) -> str | None:
            del principal, name
            return None

        def has_server_ref(self, name: str) -> bool:
            return self.server_generation(name) is not None

        def has_user_ref(self, principal: str, name: str) -> bool:
            return self.user_generation(principal, name) is not None

    states = registry.profile_availability(
        PluginId("source", "llm"),
        principal="local:alice",
        inventory=_Inventory(),
    )

    assert tuple(state.alias for state in states if state.usable) == expected_aliases
    selected = registry.selected_profile_alias(
        PluginId("source", "llm"),
        usable_aliases=expected_aliases,
    )
    assert selected == alias


@pytest.mark.parametrize(
    ("alias", "profile"),
    [
        (
            "azure-task",
            {
                "provider": "azure",
                "model": "deployment",
                "credential_scope": "server",
                "credential_ref": "AZURE_OPENAI_API_KEY",
                "endpoint": "https://example.openai.azure.com",
                "deployment_name": "deployment",
            },
        ),
        (
            "openrouter-task",
            {
                "provider": "openrouter",
                "model": "openai/gpt-5-mini",
                "credential_scope": "server",
                "credential_ref": "OPENROUTER_API_KEY",
            },
        ),
        (
            "bedrock-task",
            {
                "provider": "bedrock",
                "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
                "region_name": "ap-southeast-2",
            },
        ),
        (
            "gateway-task",
            {
                "provider": "gateway",
                "model": "standard",
                "credential_scope": "server",
                "credential_ref": "GATEWAY_BEARER_TOKEN",
                "endpoint": "https://gateway.example.com/v1",
                "contract_major": 1,
                "required_capabilities": ["text", "usage"],
            },
        ),
    ],
)
def test_each_profile_provider_lowers_into_source_config(alias: str, profile: dict[str, object]) -> None:
    runtime = RuntimeWebPluginConfig.from_settings(_settings(llm_profiles={alias: profile}, default_llm_profile=alias))
    policy = compile_web_plugin_policy(registry=_isolated_manager_with_llm_source(), settings=runtime)
    registry = OperatorProfileRegistry(policy=policy, settings=runtime)
    lowered = registry.lower_options(
        PluginId("source", "llm"),
        alias=alias,
        safe_options={
            "prompt_template": "Write one audit briefing.",
            "schema": {"mode": "observed"},
            "on_validation_failure": "discard",
        },
    )
    executable = deep_thaw(lowered.executable_options)
    if "api_key" in executable:
        executable["api_key"] = "resolved-secret"

    source = LLMSource(executable)

    assert source.provider_config.provider == profile["provider"]


def test_azure_profile_lowering_honors_deployment_derived_model_contract() -> None:
    runtime = RuntimeWebPluginConfig.from_settings(
        _settings(
            llm_profiles={
                "azure-task": {
                    "provider": "azure",
                    "model": "operator-deployment",
                    "credential_scope": "server",
                    "credential_ref": "AZURE_OPENAI_API_KEY",
                    "endpoint": "https://example.openai.azure.com",
                    "deployment_name": "operator-deployment",
                }
            },
            default_llm_profile="azure-task",
        )
    )
    policy = compile_web_plugin_policy(registry=_isolated_manager_with_llm_source(), settings=runtime)
    registry = OperatorProfileRegistry(policy=policy, settings=runtime)

    lowered = registry.lower_options(
        PluginId("transform", "llm"),
        alias="azure-task",
        safe_options={"prompt_template": "Summarise {{ row }}", "schema": {"mode": "observed"}},
    )
    executable = deep_thaw(lowered.executable_options)
    executable["api_key"] = "resolved-secret"
    config = AzureOpenAIConfig.model_validate(executable)

    assert config.model == "operator-deployment"
    assert check_config_value_sources(config, component_id="llm") == ()


def test_azure_profile_rejects_model_that_disagrees_with_deployment() -> None:
    with pytest.raises(ValueError, match="model must match deployment_name"):
        RuntimeWebPluginConfig.from_settings(
            _settings(
                llm_profiles={
                    "azure-task": {
                        "provider": "azure",
                        "model": "catalog-model-name",
                        "credential_scope": "server",
                        "credential_ref": "AZURE_OPENAI_API_KEY",
                        "endpoint": "https://example.openai.azure.com",
                        "deployment_name": "operator-deployment",
                    }
                },
                default_llm_profile="azure-task",
            )
        )


def test_bedrock_profile_lowering_is_keyless() -> None:
    lowered = _profile_registry().lower_options(
        PluginId("transform", "llm"),
        alias="bedrock-task-role",
        safe_options={"prompt_template": "{{ row }}"},
    )

    assert lowered.executable_options["provider"] == "bedrock"
    assert lowered.executable_options["region_name"] == "ap-southeast-2"
    assert "api_key" not in lowered.executable_options


def test_profile_lowering_rejects_raw_provider_options() -> None:
    with pytest.raises(ValueError, match="private_profile_option"):
        _profile_registry().lower_options(
            PluginId("transform", "llm"),
            alias="tutorial",
            safe_options={"provider": "bedrock", "prompt_template": "{{ row }}"},
        )


# ---------------------------------------------------------------------------
# Gateway profiles (Phase 2 Task 4) — closes the app-startup KeyError('gateway')
# crash and covers the credential-seam reconciliation described in the task
# report.
# ---------------------------------------------------------------------------


def _gateway_settings(**profile_overrides: object) -> WebSettings:
    profile: dict[str, object] = {
        "provider": "gateway",
        "model": "standard",
        "credential_scope": "server",
        "credential_ref": "GATEWAY_BEARER_TOKEN",
        "endpoint": "https://gateway.example.com/v1",
        "contract_major": 1,
        "required_capabilities": ["text", "usage"],
    }
    profile.update(profile_overrides)
    return _settings(llm_profiles={"gateway-task": profile}, default_llm_profile="gateway-task")


def test_runtime_web_plugin_config_from_settings_does_not_crash_for_gateway_profile() -> None:
    """Regression test for the Task 2-introduced crash: before this task,
    ``RuntimeLLMProfile.from_settings``'s hard-coded ``provider_fields[...]``
    table had no "gateway" arm, so this call raised a bare ``KeyError``
    (called from every web app boot via ``RuntimeWebPluginConfig.from_settings``,
    see ``web/app.py``)."""
    runtime = RuntimeWebPluginConfig.from_settings(_gateway_settings())

    profile = dict(runtime.llm_profiles)["gateway-task"]
    assert profile.provider == "gateway"
    assert profile.credential_scope == "server"
    assert profile.credential_ref == "GATEWAY_BEARER_TOKEN"
    assert dict(profile.provider_options) == {
        "endpoint": "https://gateway.example.com/v1",
        "contract_major": 1,
        "required_capabilities": ("text", "usage"),
        "timeout_seconds": 60.0,
    }


def test_gateway_profile_rejects_user_credential_scope() -> None:
    with pytest.raises(ValidationError, match="credential_scope 'server'"):
        _gateway_settings(credential_scope="user")


@pytest.mark.parametrize(
    "field_overrides",
    [
        {"region_name": "ap-southeast-2"},
        {"deployment_name": "some-deployment"},
        {"api_version": "2024-01-01"},
    ],
)
def test_gateway_profile_rejects_other_provider_fields(field_overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="another provider"):
        _gateway_settings(**field_overrides)


def test_gateway_profile_requires_endpoint_contract_major_and_capabilities() -> None:
    with pytest.raises(ValidationError, match="requires operator endpoint"):
        _gateway_settings(endpoint=None)
    with pytest.raises(ValidationError, match="requires contract_major"):
        _gateway_settings(contract_major=None)
    with pytest.raises(ValidationError, match="requires required_capabilities"):
        _gateway_settings(required_capabilities=None)


def test_gateway_profile_reuses_gatewayconfig_endpoint_validation() -> None:
    """The profile validator reuses ``GatewayConfig``'s own endpoint
    validator rather than duplicating the loopback/HTTPS/versioned-base
    rule — a plain ``http://`` non-loopback endpoint must fail exactly the
    way ``GatewayConfig`` itself would reject it."""
    with pytest.raises(ValidationError, match="HTTPS"):
        _gateway_settings(endpoint="http://gateway.example.com/v1")


def test_gateway_profile_rejects_unsupported_contract_major() -> None:
    with pytest.raises(ValidationError, match="not supported"):
        _gateway_settings(contract_major=2)


def test_gateway_profile_rejects_unknown_capability() -> None:
    with pytest.raises(ValidationError, match="unknown gateway capability"):
        _gateway_settings(required_capabilities=["not_a_real_capability"])


def test_gateway_profile_lowering_produces_private_executable_and_audit_safe_options() -> None:
    lowered = _profile_registry().lower_options(
        PluginId("transform", "llm"),
        alias="gateway-task",
        safe_options={"prompt_template": "Summarise {{ row }}", "response_field": "summary"},
    )

    assert lowered.executable_options["provider"] == "gateway"
    assert lowered.executable_options["model"] == "standard"
    assert lowered.executable_options["endpoint"] == "https://gateway.example.com/v1"
    assert lowered.executable_options["contract_major"] == 1
    assert lowered.executable_options["required_capabilities"] == ("text", "usage")
    assert lowered.executable_options["timeout_seconds"] == 60.0
    assert lowered.executable_options["api_key"] == {
        "secret_ref": "GATEWAY_BEARER_TOKEN",
        "secret_scope": "server",
    }
    assert deep_thaw(lowered.audit_safe_options) == {
        "profile": "gateway-task",
        "prompt_template": "Summarise {{ row }}",
        "response_field": "summary",
    }
    assert "GATEWAY_BEARER_TOKEN" not in repr(lowered)
    assert "gateway.example.com" not in repr(lowered)


def test_gateway_profile_lowering_round_trips_into_gateway_config() -> None:
    lowered = _profile_registry().lower_options(
        PluginId("transform", "llm"),
        alias="gateway-task",
        safe_options={"prompt_template": "Summarise {{ row }}", "schema": {"mode": "observed"}},
    )
    executable = deep_thaw(lowered.executable_options)
    executable["api_key"] = "resolved-bearer-token"
    config = GatewayConfig.model_validate(executable)

    assert config.model == "standard"
    assert config.endpoint == "https://gateway.example.com/v1"
    assert config.contract_major == 1
    assert config.required_capabilities == ("text", "usage")
    assert check_config_value_sources(config, component_id="llm") == ()


@pytest.mark.parametrize(
    "private_field",
    ["endpoint", "credential_ref", "api_key", "contract_major", "required_capabilities", "credential_scope"],
)
def test_gateway_profile_lowering_rejects_every_private_field_in_web_authored_options(private_field: str) -> None:
    with pytest.raises(ValueError, match="private_profile_option"):
        _profile_registry().lower_options(
            PluginId("transform", "llm"),
            alias="gateway-task",
            safe_options={"prompt_template": "{{ row }}", private_field: "attacker-supplied"},
        )


def test_gateway_public_schema_excludes_private_fields() -> None:
    full = create_catalog_service().get_schema("transform", "llm")
    public = _profile_registry().public_schema(
        PluginId("transform", "llm"),
        full,
        available_aliases=("gateway-task",),
    )
    rendered = public.model_dump_json()

    assert '"profile"' in rendered
    assert '"gateway-task"' in rendered
    for private_name in (
        "endpoint",
        "credential_ref",
        "credential_scope",
        "api_key",
        "contract_major",
        "required_capabilities",
        "gateway.example.com",
        "GATEWAY_BEARER_TOKEN",
    ):
        assert private_name not in rendered
