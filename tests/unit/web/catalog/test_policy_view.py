from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import SecretBytes

from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.catalog.policy_view import PolicyCatalogView
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
) -> PolicyCatalogView:
    settings = WebSettings(
        composer_max_composition_turns=4,
        composer_max_discovery_turns=4,
        composer_timeout_seconds=60,
        composer_rate_limit_per_minute=20,
        shareable_link_signing_key=SecretBytes(b"0123456789abcdef0123456789abcdef"),
        plugin_allowlist=plugin_allowlist,
        deployment_aws_region=deployment_aws_region,
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


def test_hidden_schema_uses_sanitized_closed_error(view: PolicyCatalogView) -> None:
    with pytest.raises(ValueError) as exc_info:
        view.get_schema("transform", "azure_prompt_shield")

    assert str(exc_info.value) == "plugin_not_enabled"


def test_web_prohibited_source_is_coherent_across_every_reader() -> None:
    """All three readers of one prohibition must agree.

    ``PolicyCatalogView`` answers "what exists" (``list_*``), "why not"
    (``unavailable_reason``) and "configure it" (``get_schema``) from the same
    snapshot, and a PARTIAL fix is the dangerous outcome: a source missing from
    the listing but still schema-readable, or hidden with no reason attached,
    leaves the composer able to name the plugin and be told nothing it can act
    on. Kind-qualified identity is what keeps the aws_s3 SINK fully usable
    while the SOURCE is refused.
    """
    view = _build_view(plugin_allowlist=("source:aws_s3", "sink:aws_s3"))
    source_id = PluginId("source", "aws_s3")

    assert "aws_s3" not in {item.name for item in view.list_sources()}
    assert "aws_s3" in {item.name for item in view.list_sinks()}
    assert view.unavailable_reason(source_id) is PluginUnavailableReason.WEB_SURFACE_PROHIBITED
    assert view.unavailable_reason(PluginId("sink", "aws_s3")) is None
    with pytest.raises(ValueError, match="plugin_not_enabled"):
        view.get_schema("source", "aws_s3")
    assert view.get_schema("sink", "aws_s3").name == "aws_s3"


def test_trained_operator_view_still_offers_the_web_prohibited_source() -> None:
    """The local MCP projection keeps the plugin the web surface refuses."""
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    view = PolicyCatalogView.for_trained_operator(catalog, snapshot)

    assert "aws_s3" in {item.name for item in view.list_sources()}
    assert view.unavailable_reason(PluginId("source", "aws_s3")) is None
    assert view.get_schema("source", "aws_s3").name == "aws_s3"


def test_web_username_matching_trained_operator_marker_cannot_create_unrestricted_view() -> None:
    web_view = _build_view(principal_scope="local:trained-operator")

    with pytest.raises(ValueError, match="trained_operator_snapshot_required"):
        PolicyCatalogView.for_trained_operator(create_catalog_service(), web_view.snapshot)
