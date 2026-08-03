from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import SecretBytes

from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools._common import ToolContext
from elspeth.web.composer.tools.generation import _execute_get_plugin_assistance
from elspeth.web.config import WebSettings
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.availability import build_plugin_snapshot
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId, PluginUnavailableReason
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig


@dataclass
class _Inventory:
    def has_server_ref(self, name: str) -> bool:
        return False

    def has_user_ref(self, principal: str, name: str) -> bool:
        return False

    def has_ref(self, principal: str, name: str) -> bool:
        return False

    def server_generation(self, name: str) -> str | None:
        return None

    def user_generation(self, principal: str, name: str) -> str | None:
        return None


def _build_view(
    *,
    principal_scope: str = "local:alice",
    plugin_allowlist: tuple[str, ...] = (),
    deployment_aws_region: str | None = None,
    aws_s3_source_profiles: tuple[dict[str, str], ...] = (),
) -> PolicyCatalogView:
    settings = WebSettings(
        composer_max_composition_turns=4,
        composer_max_discovery_turns=4,
        composer_timeout_seconds=60,
        composer_rate_limit_per_minute=20,
        shareable_link_signing_key=SecretBytes(b"0123456789abcdef0123456789abcdef"),
        plugin_allowlist=plugin_allowlist,
        deployment_aws_region=deployment_aws_region,
        aws_s3_source_profiles=aws_s3_source_profiles,
        llm_profiles={
            "task-role": {
                "provider": "bedrock",
                "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
            }
        },
        default_llm_profile="task-role",
    )
    runtime = RuntimeWebPluginConfig.from_settings(settings)
    policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime)
    profiles = OperatorProfileRegistry(policy=policy, settings=runtime)
    snapshot = build_plugin_snapshot(
        policy=policy,
        catalog=create_catalog_service(),
        profiles=profiles,
        principal_scope=principal_scope,
        secret_inventory=_Inventory(),
        generation_key=b"policy-view-test-key",
    )
    return PolicyCatalogView(create_catalog_service(), snapshot, profiles)


@pytest.fixture
def view() -> PolicyCatalogView:
    return _build_view()


def test_lists_only_snapshot_available_plugins(view: PolicyCatalogView) -> None:
    assert {item.name for item in view.list_sources()} == {"csv", "json", "llm", "text"}
    assert {item.name for item in view.list_sinks()} == {"csv", "json", "text"}
    assert {item.name for item in view.list_transforms()} == {"field_mapper", "llm", "web_scrape"}


def test_public_llm_schema_contains_only_usable_alias(view: PolicyCatalogView) -> None:
    rendered = view.get_schema("transform", "llm").model_dump_json()
    assert '"task-role"' in rendered
    assert '"api_key"' not in rendered


def test_profiled_list_summary_is_projected_from_public_schema(view: PolicyCatalogView) -> None:
    llm = next(item for item in view.list_transforms() if item.name == "llm")

    assert "profile" in {field.name for field in llm.config_fields}
    assert not {field.name for field in llm.config_fields} & {"provider", "model", "api_key", "region_name"}
    assert llm.secret_requirements == ()


def test_textract_web_summary_schema_and_digest_use_deployment_profile() -> None:
    from elspeth.web.composer.planner_authoring_aids import discovery_digest

    view = _build_view(
        plugin_allowlist=("transform:aws_textract_document_analysis",),
        deployment_aws_region="ap-southeast-1",
    )

    summary = next(item for item in view.list_transforms() if item.name == "aws_textract_document_analysis")
    field_names = {field.name for field in summary.config_fields}
    assert "profile" in field_names
    assert not field_names & {"region", "auth_mode", "aws_access_key_id", "aws_secret_access_key", "aws_session_token"}
    assert summary.example_use is not None
    assert "profile: deployment" in summary.example_use
    assert "region:" not in summary.example_use
    assert "auth_mode" not in summary.example_use
    rendered_hints = " ".join(summary.composer_hints)
    assert "profile" in rendered_hints
    assert "credentials" not in rendered_hints.casefold()

    schema = view.get_schema("transform", "aws_textract_document_analysis")
    assert schema.json_schema["properties"]["profile"]["enum"] == ["deployment"]
    digest_entry = next(item for item in discovery_digest(view)["transforms"] if item["name"] == "aws_textract_document_analysis")
    assert digest_entry["profile_aliases"] == ["deployment"]
    assert "profile" in digest_entry["required_options"]
    assert not set(digest_entry["required_options"]) & {"region", "auth_mode"}


def test_trained_operator_textract_summary_retains_explicit_raw_config() -> None:
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    trained = PolicyCatalogView.for_trained_operator(catalog, snapshot)

    summary = next(item for item in trained.list_transforms() if item.name == "aws_textract_document_analysis")
    assert {"region", "auth_mode"} <= {field.name for field in summary.config_fields}
    assert summary.example_use is not None
    assert "region: ap-southeast-2" in summary.example_use
    assert "auth_mode: default_chain" in summary.example_use


def test_textract_web_assistance_uses_only_deployment_profile_guidance() -> None:
    view = _build_view(
        plugin_allowlist=("transform:aws_textract_document_analysis",),
        deployment_aws_region="ap-southeast-1",
    )
    result = _execute_get_plugin_assistance(
        {"plugin_type": "transform", "plugin_name": "aws_textract_document_analysis"},
        CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1),
        ToolContext(catalog=view, plugin_snapshot=view.snapshot),
    )

    assert result.success is True
    payload = result.to_dict()["data"]
    guidance = " ".join((payload["summary"], *payload["composer_hints"]))
    assert "profile" in guidance.casefold()
    assert "deployment" in guidance.casefold()
    for private_name in (
        "region",
        "auth_mode",
        "secret_ref",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
    ):
        assert private_name not in guidance


def test_trained_operator_textract_assistance_retains_raw_configuration_guidance() -> None:
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    trained = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    result = _execute_get_plugin_assistance(
        {"plugin_type": "transform", "plugin_name": "aws_textract_document_analysis"},
        CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1),
        ToolContext(catalog=trained, plugin_snapshot=snapshot),
    )

    assert result.success is True
    payload = result.to_dict()["data"]
    guidance = " ".join((payload["summary"], *payload["composer_hints"]))
    assert "default chain" in guidance
    assert "ELSPETH secret refs" in guidance
    assert "explicit AWS credentials" in guidance


def test_s3_source_web_discovery_and_assistance_expose_only_profile_safe_authoring() -> None:
    from elspeth.web.composer.planner_authoring_aids import discovery_digest

    view = _build_view(
        plugin_allowlist=("source:aws_s3",),
        deployment_aws_region="ap-southeast-1",
        aws_s3_source_profiles=(
            {
                "alias": "demo-input",
                "bucket": "operator-private-bucket-marker",
                "prefix": "operator-private-prefix-marker",
            },
        ),
    )

    summary = next(item for item in view.list_sources() if item.name == "aws_s3")
    field_names = {field.name for field in summary.config_fields}
    assert {"profile", "key", "format", "schema", "on_validation_failure"} <= field_names
    assert not field_names & {
        "bucket",
        "prefix",
        "region",
        "region_name",
        "auth_mode",
        "endpoint_url",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
    }
    assert summary.example_use is not None
    assert "profile: demo-input" in summary.example_use
    assert "key: records/input.csv" in summary.example_use
    assert "operator-private" not in summary.model_dump_json()
    assert "bucket:" not in summary.example_use
    assert "region" not in summary.example_use
    assert "endpoint" not in summary.example_use
    assert "credential" not in summary.example_use
    assert summary.usage_when_to_use is not None
    assert "Web Composer" in summary.usage_when_to_use
    assert "operator-approved S3 source profile" in summary.usage_when_to_use
    assert "CLI or batch" not in summary.usage_when_to_use
    assert summary.usage_when_not_to_use is not None
    assert "Ordinary Web Composer cannot use this source" not in summary.usage_when_not_to_use
    assert "matching operator-approved profile" in summary.usage_when_not_to_use

    schema = view.get_schema("source", "aws_s3")
    assert schema.json_schema["properties"]["profile"]["enum"] == ["demo-input"]
    digest_entry = next(item for item in discovery_digest(view)["sources"] if item["name"] == "aws_s3")
    assert digest_entry["profile_aliases"] == ["demo-input"]
    assert {"profile", "key", "schema"} <= set(digest_entry["required_options"])

    result = _execute_get_plugin_assistance(
        {"plugin_type": "source", "plugin_name": "aws_s3"},
        CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1),
        ToolContext(catalog=view, plugin_snapshot=view.snapshot),
    )
    assert result.success is True
    guidance = " ".join((result.data["summary"], *result.data["composer_hints"]))
    assert "profile" in guidance.casefold()
    assert "relative" in guidance.casefold()
    assert "operator-private" not in guidance
    for private_name in ("bucket", "prefix", "region", "endpoint", "credential"):
        assert private_name not in guidance.casefold()


def test_hidden_schema_uses_sanitized_closed_error(view: PolicyCatalogView) -> None:
    with pytest.raises(ValueError) as exc_info:
        view.get_schema("transform", "azure_prompt_shield")

    assert str(exc_info.value) == "plugin_not_enabled"


def test_unconfigured_profiled_source_is_coherent_across_every_reader() -> None:
    """All three readers of one unavailable profile must agree.

    ``PolicyCatalogView`` answers "what exists" (``list_*``), "why not"
    (``unavailable_reason``) and "configure it" (``get_schema``) from the same
    snapshot, and a PARTIAL fix is the dangerous outcome: a source missing from
    the listing but still schema-readable, or hidden with no reason attached,
    leaves the composer able to name the plugin and be told nothing it can act
    on. Kind-qualified identity is what keeps the aws_s3 SINK fully usable
    while the unconfigured SOURCE is refused.
    """
    view = _build_view(plugin_allowlist=("source:aws_s3", "sink:aws_s3"))
    source_id = PluginId("source", "aws_s3")

    assert "aws_s3" not in {item.name for item in view.list_sources()}
    assert "aws_s3" in {item.name for item in view.list_sinks()}
    assert view.unavailable_reason(source_id) is PluginUnavailableReason.PROFILE_UNAVAILABLE
    assert view.unavailable_reason(PluginId("sink", "aws_s3")) is None
    with pytest.raises(ValueError, match="plugin_not_enabled"):
        view.get_schema("source", "aws_s3")
    assert view.get_schema("sink", "aws_s3").name == "aws_s3"


def test_trained_operator_view_still_offers_raw_s3_source_configuration() -> None:
    """The local MCP projection keeps unrestricted raw S3 configuration."""
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    view = PolicyCatalogView.for_trained_operator(catalog, snapshot)

    assert "aws_s3" in {item.name for item in view.list_sources()}
    assert view.unavailable_reason(PluginId("source", "aws_s3")) is None
    assert view.get_schema("source", "aws_s3").name == "aws_s3"
    summary = next(item for item in view.list_sources() if item.name == "aws_s3")
    assert summary.usage_when_to_use is not None
    assert "trusted CLI or batch" in summary.usage_when_to_use
    assert summary.usage_when_not_to_use is not None
    assert "Ordinary Web Composer cannot use this source" in summary.usage_when_not_to_use


def test_web_username_matching_trained_operator_marker_cannot_create_unrestricted_view() -> None:
    web_view = _build_view(principal_scope="local:trained-operator")

    with pytest.raises(ValueError, match="trained_operator_snapshot_required"):
        PolicyCatalogView.for_trained_operator(create_catalog_service(), web_view.snapshot)
