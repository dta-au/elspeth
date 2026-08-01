from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pytest

from elspeth.contracts.plugin_capabilities import CapabilityDeclaration, PluginCapability, WebConfigAuthority
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.plugins.sources.llm import LLMSource
from elspeth.web.catalog.schemas import PluginSchemaInfo, PluginSummary
from elspeth.web.config import WebSettings
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.availability import build_plugin_snapshot
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.models import (
    PluginAvailabilitySnapshot,
    PluginId,
    PluginUnavailableReason,
    WebPluginPolicy,
)
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig


@dataclass
class _Inventory:
    server: frozenset[str] = frozenset()
    users: dict[str, frozenset[str]] | None = None
    server_generations: dict[str, str] | None = None
    user_generations: dict[tuple[str, str], str] | None = None

    def has_server_ref(self, name: str) -> bool:
        return name in self.server

    def has_user_ref(self, principal: str, name: str) -> bool:
        return name in (self.users or {}).get(principal, frozenset())

    def has_ref(self, principal: str, name: str) -> bool:
        return self.has_user_ref(principal, name) or self.has_server_ref(name)

    def server_generation(self, name: str) -> str | None:
        if self.server_generations is not None:
            return self.server_generations.get(name)
        return "present" if self.has_server_ref(name) else None

    def user_generation(self, principal: str, name: str) -> str | None:
        if self.user_generations is not None:
            return self.user_generations.get((principal, name))
        return "present" if self.has_user_ref(principal, name) else None


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


def _build_with_policy(
    settings: WebSettings,
    *,
    principal: str = "local:alice",
    inventory: _Inventory | None = None,
) -> tuple[WebPluginPolicy, PluginAvailabilitySnapshot]:
    runtime = RuntimeWebPluginConfig.from_settings(settings)
    manager = get_shared_plugin_manager()
    policy = compile_web_plugin_policy(registry=manager, settings=runtime)
    profiles = OperatorProfileRegistry(policy=policy, settings=runtime)
    snapshot = build_plugin_snapshot(
        policy=policy,
        catalog=create_catalog_service(),
        profiles=profiles,
        principal_scope=principal,
        secret_inventory=inventory or _Inventory(),
        generation_key=b"deterministic-test-generation-key",
    )
    return policy, snapshot


def _build(settings: WebSettings, *, principal: str = "local:alice", inventory: _Inventory | None = None):
    _policy, snapshot = _build_with_policy(settings, principal=principal, inventory=inventory)
    return snapshot


_AWS_S3_ALLOWLIST = ("source:aws_s3", "sink:aws_s3")


class _InternalLLMSourceCatalog:
    """Task-8-only catalog seam; real built-in discovery remains disabled."""

    _summary = PluginSummary(
        name="llm",
        description="internal LLM source",
        plugin_type="source",
        config_fields=[],
        web_config_authority=WebConfigAuthority.OPERATOR_PROFILED,
        policy_capabilities=(CapabilityDeclaration(PluginCapability.LLM),),
        secret_requirements=(),
    )
    _schema = PluginSchemaInfo(
        name="llm",
        plugin_type="source",
        description="internal LLM source",
        json_schema={},
        knob_schema={"fields": []},
        web_config_authority=WebConfigAuthority.OPERATOR_PROFILED,
        policy_capabilities=(CapabilityDeclaration(PluginCapability.LLM),),
        secret_requirements=(),
    )

    def list_sources(self) -> list[PluginSummary]:
        return [self._summary]

    def list_transforms(self) -> list[PluginSummary]:
        return []

    def list_sinks(self) -> list[PluginSummary]:
        return []

    def get_schema(self, plugin_type: Literal["source", "transform", "sink"], name: str) -> PluginSchemaInfo:
        if plugin_type != "source" or name != "llm":
            raise ValueError("unknown internal plugin")
        return self._schema

    def post_call_hints(
        self,
        *,
        plugin_type: Literal["source", "transform", "sink"],
        plugin_name: str,
        tool_name: str,
        config_snapshot: object,
    ) -> tuple[str, ...]:
        del plugin_type, plugin_name, tool_name, config_snapshot
        return ()


def _build_internal_llm_source(
    profile: dict[str, object] | None,
    *,
    inventory: _Inventory | None = None,
    principal: str = "local:alice",
) -> PluginAvailabilitySnapshot:
    source_id = PluginId("source", "llm")
    settings = _settings(
        llm_profiles={} if profile is None else {"source-profile": profile},
        default_llm_profile=None if profile is None else "source-profile",
    )
    runtime = RuntimeWebPluginConfig.from_settings(settings)
    policy = WebPluginPolicy.create(
        required=frozenset({source_id}),
        configured_optional=frozenset(),
        preferences=(),
        control_modes=(),
        plugin_code_identities=((source_id, "1.0.0", "internal-task-8"),),
    )
    return build_plugin_snapshot(
        policy=policy,
        catalog=_InternalLLMSourceCatalog(),
        profiles=OperatorProfileRegistry(policy=policy, settings=runtime),
        principal_scope=principal,
        secret_inventory=inventory or _Inventory(),
        generation_key=b"task-8-internal-source-generation-key",
    )


def test_llm_source_has_no_flat_discovery_credential_requirement() -> None:
    assert LLMSource.discovery_secret_requirements == {}


def test_keyless_bedrock_source_profile_is_available_with_empty_inventory() -> None:
    snapshot = _build_internal_llm_source(
        {
            "provider": "bedrock",
            "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
        }
    )

    source_id = PluginId("source", "llm")
    assert source_id in snapshot.available
    assert dict(snapshot.usable_profile_aliases)[source_id] == ("source-profile",)


@pytest.mark.parametrize(
    ("provider", "scope", "credential_ref", "profile_options"),
    [
        (
            "azure",
            "server",
            "AZURE_SOURCE_KEY",
            {
                "model": "deployment",
                "endpoint": "https://example.openai.azure.com",
                "deployment_name": "deployment",
            },
        ),
        ("openrouter", "server", "OPENROUTER_SOURCE_KEY", {"model": "openai/gpt-5-mini"}),
        ("openrouter", "user", "OPENROUTER_PERSONAL_KEY", {"model": "openai/gpt-5-mini"}),
        (
            "gateway",
            "server",
            "GATEWAY_SOURCE_KEY",
            {
                "model": "standard",
                "endpoint": "https://gateway.example.com/v1",
                "contract_major": 1,
                "required_capabilities": ["text", "usage"],
            },
        ),
    ],
)
def test_credentialed_source_profile_requires_its_exact_scoped_inventory_ref(
    provider: str,
    scope: str,
    credential_ref: str,
    profile_options: dict[str, object],
) -> None:
    principal = "local:alice"
    profile = {
        "provider": provider,
        "credential_scope": scope,
        "credential_ref": credential_ref,
        **profile_options,
    }
    wrong = _build_internal_llm_source(
        profile,
        principal=principal,
        inventory=_Inventory(
            server=frozenset({"WRONG_KEY"}),
            users={principal: frozenset({"WRONG_KEY"})},
        ),
    )
    correct_inventory = (
        _Inventory(server=frozenset({credential_ref}), server_generations={credential_ref: "private-generation"})
        if scope == "server"
        else _Inventory(
            users={principal: frozenset({credential_ref})},
            user_generations={(principal, credential_ref): "private-generation"},
        )
    )
    correct = _build_internal_llm_source(profile, principal=principal, inventory=correct_inventory)

    source_id = PluginId("source", "llm")
    assert source_id not in wrong.available
    assert dict(wrong.usable_profile_aliases)[source_id] == ()
    assert source_id in correct.available
    assert dict(correct.usable_profile_aliases)[source_id] == ("source-profile",)
    assert credential_ref not in repr(correct)
    assert "private-generation" not in repr(correct)


def test_llm_source_with_no_configured_profile_fails_closed() -> None:
    snapshot = _build_internal_llm_source(None)
    source_id = PluginId("source", "llm")

    assert source_id not in snapshot.available
    assert dict(snapshot.usable_profile_aliases)[source_id] == ()
    assert [item.reason for item in snapshot.unavailable if item.plugin_id == source_id] == [PluginUnavailableReason.PROFILE_UNAVAILABLE]


def test_web_prohibited_source_is_a_declined_authorization_not_an_offer() -> None:
    """A runtime-authorized aws_s3 SOURCE must be declined, never offered.

    A deployment can legitimately authorize S3 for its own runtime (the AWS
    scenario module's ``default_plugin_allowlist`` does). The web authoring
    surface refuses author-controlled S3 reads categorically, so the snapshot
    has to carry that authorization as a *declined* one: without it, every
    reader of ``available`` (discovery listings, the guided step-1 picker,
    prompts, tool validation) offers the plugin and only the far end of
    authoring refuses it — an unrepairable dead end (F13/F14, 2026-07-31).
    """
    policy, snapshot = _build_with_policy(_settings(plugin_allowlist=_AWS_S3_ALLOWLIST))
    baseline = _build(_settings())
    source_id = PluginId("source", "aws_s3")

    assert source_id in policy.authorized
    assert source_id not in snapshot.available
    # Declared exactly once, and with the reason that names a policy no
    # operator setting can clear — not a repairable credential/profile gap.
    assert [item.reason for item in snapshot.unavailable if item.plugin_id == source_id] == [PluginUnavailableReason.WEB_SURFACE_PROHIBITED]
    # The SINK is untouched: kind-qualified identity keeps S3 writes usable.
    assert PluginId("sink", "aws_s3") in snapshot.available
    # available never exceeds authorized, and a prohibition cannot silently
    # re-point a capability selection.
    assert snapshot.available <= policy.authorized
    assert snapshot.selected == baseline.selected


def test_trained_operator_snapshot_keeps_the_web_prohibited_source() -> None:
    """The local trained-operator (CLI/MCP) exemption is unchanged.

    ``for_trained_operator`` is a separate constructor that never consults the
    web policy, so the prohibition cannot leak into the local surface.
    """
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service())

    assert PluginId("source", "aws_s3") in snapshot.available
    assert snapshot.unavailable == ()


def test_operator_profiled_llm_is_unavailable_without_usable_alias() -> None:
    snapshot = _build(_settings())

    assert PluginId("transform", "llm") not in snapshot.available
    assert dict(snapshot.usable_profile_aliases)[PluginId("transform", "llm")] == ()


def test_bedrock_profile_is_locally_available_without_secret() -> None:
    snapshot = _build(
        _settings(
            llm_profiles={
                "task-role": {
                    "provider": "bedrock",
                    "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
                }
            },
            default_llm_profile="task-role",
        )
    )

    assert PluginId("transform", "llm") in snapshot.available
    assert dict(snapshot.selected_profile_aliases)[PluginId("transform", "llm")] == "task-role"


def test_configured_tutorial_profile_is_the_selected_usable_alias() -> None:
    snapshot = _build(
        _settings(
            llm_profiles={
                "alpha": {
                    "provider": "bedrock",
                    "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
                },
                "tutorial": {
                    "provider": "bedrock",
                    "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
                },
            },
            default_llm_profile="tutorial",
        )
    )

    llm_id = PluginId("transform", "llm")
    assert dict(snapshot.usable_profile_aliases)[llm_id] == ("tutorial", "alpha")
    assert dict(snapshot.selected_profile_aliases)[llm_id] == "tutorial"


def test_missing_default_profile_leaves_no_selected_alias() -> None:
    """No designated default means no selected alias — not the alphabetical first.

    A missing ``default_llm_profile`` is a supported degraded-readiness state:
    profiles stay usable for explicit authoring, but the snapshot must not
    promote whichever alias sorts first into a house default the operator never
    designated — the Composer would author against the wrong provider/model.
    """
    snapshot = _build(
        _settings(
            llm_profiles={
                "alpha": {
                    "provider": "bedrock",
                    "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
                },
                "beta": {
                    "provider": "bedrock",
                    "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
                },
            },
        )
    )

    llm_id = PluginId("transform", "llm")
    assert llm_id in snapshot.available
    assert dict(snapshot.usable_profile_aliases)[llm_id] == ("alpha", "beta")
    assert dict(snapshot.selected_profile_aliases)[llm_id] is None


def test_unusable_default_profile_is_not_silently_substituted() -> None:
    """A designated default the principal cannot use selects nothing.

    Substituting the next usable alias would swap providers behind the
    operator's designation; readiness reports the credential gap instead.
    """
    snapshot = _build(
        _settings(
            llm_profiles={
                "house": {
                    "provider": "openrouter",
                    "model": "openai/gpt-5-mini",
                    "credential_scope": "user",
                    "credential_ref": "OPENROUTER_API_KEY",
                },
                "local": {
                    "provider": "bedrock",
                    "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
                },
            },
            default_llm_profile="house",
        )
    )

    llm_id = PluginId("transform", "llm")
    assert dict(snapshot.usable_profile_aliases)[llm_id] == ("local",)
    assert dict(snapshot.selected_profile_aliases)[llm_id] is None


def test_user_secret_can_narrow_but_never_expand_policy() -> None:
    snapshot = _build(
        _settings(),
        inventory=_Inventory(users={"local:alice": frozenset({"AZURE_CONTENT_SAFETY_KEY"})}),
    )

    assert PluginId("transform", "azure_prompt_shield") not in snapshot.available


def test_profile_aliases_and_hash_are_principal_scoped() -> None:
    settings = _settings(
        llm_profiles={
            "personal": {
                "provider": "openrouter",
                "model": "openai/gpt-5-mini",
                "credential_scope": "user",
                "credential_ref": "OPENROUTER_API_KEY",
            }
        },
        default_llm_profile="personal",
    )
    inventory = _Inventory(users={"local:alice": frozenset({"OPENROUTER_API_KEY"})})
    alice = _build(settings, principal="local:alice", inventory=inventory)
    bob = _build(settings, principal="local:bob", inventory=inventory)

    assert dict(alice.usable_profile_aliases)[PluginId("transform", "llm")] == ("personal",)
    assert dict(bob.usable_profile_aliases)[PluginId("transform", "llm")] == ()
    assert alice.snapshot_hash != bob.snapshot_hash
    assert "OPENROUTER_API_KEY" not in alice.binding_generation_fingerprint


@pytest.mark.parametrize("scope", ["user", "server"])
def test_in_place_profile_credential_rotation_changes_snapshot_identity(scope: str) -> None:
    settings = _settings(
        llm_profiles={
            "rotating": {
                "provider": "openrouter",
                "model": "openai/gpt-5-mini",
                "credential_scope": scope,
                "credential_ref": "OPENROUTER_API_KEY",
            }
        },
        default_llm_profile="rotating",
    )
    principal = "local:alice"
    availability = {"OPENROUTER_API_KEY"}
    if scope == "user":
        before_inventory = _Inventory(
            users={principal: frozenset(availability)},
            user_generations={(principal, "OPENROUTER_API_KEY"): "generation-one"},
        )
        after_inventory = _Inventory(
            users={principal: frozenset(availability)},
            user_generations={(principal, "OPENROUTER_API_KEY"): "generation-two"},
        )
    else:
        before_inventory = _Inventory(
            server=frozenset(availability),
            server_generations={"OPENROUTER_API_KEY": "generation-one"},
        )
        after_inventory = _Inventory(
            server=frozenset(availability),
            server_generations={"OPENROUTER_API_KEY": "generation-two"},
        )

    before = _build(settings, principal=principal, inventory=before_inventory)
    after = _build(settings, principal=principal, inventory=after_inventory)

    assert before.available == after.available
    assert before.usable_profile_aliases == after.usable_profile_aliases
    assert before.binding_generation_fingerprint != after.binding_generation_fingerprint
    assert before.snapshot_hash != after.snapshot_hash
    assert "generation-one" not in before.binding_generation_fingerprint
    assert "generation-two" not in after.binding_generation_fingerprint


@pytest.mark.parametrize(
    ("before_profile", "after_profile", "inventory"),
    [
        (
            {
                "provider": "bedrock",
                "model": "bedrock/apac.amazon.nova-micro-v1:0",
                "region_name": "ap-southeast-1",
            },
            {
                "provider": "bedrock",
                "model": "bedrock/apac.amazon.nova-lite-v1:0",
                "region_name": "ap-southeast-2",
            },
            _Inventory(),
        ),
        (
            {
                "provider": "azure",
                "model": "before-deployment",
                "credential_scope": "server",
                "credential_ref": "AZURE_OPENAI_API_KEY",
                "endpoint": "https://before.openai.azure.com",
                "deployment_name": "before-deployment",
            },
            {
                "provider": "azure",
                "model": "after-deployment",
                "credential_scope": "server",
                "credential_ref": "AZURE_OPENAI_API_KEY",
                "endpoint": "https://after.openai.azure.com",
                "deployment_name": "after-deployment",
            },
            _Inventory(
                server=frozenset({"AZURE_OPENAI_API_KEY"}),
                server_generations={"AZURE_OPENAI_API_KEY": "same-credential-generation"},
            ),
        ),
    ],
)
def test_llm_operator_binding_change_changes_snapshot_identity(
    before_profile: dict[str, object],
    after_profile: dict[str, object],
    inventory: _Inventory,
) -> None:
    before = _build(_settings(llm_profiles={"stable": before_profile}, default_llm_profile="stable"), inventory=inventory)
    repeated = _build(_settings(llm_profiles={"stable": before_profile}, default_llm_profile="stable"), inventory=inventory)
    after = _build(_settings(llm_profiles={"stable": after_profile}, default_llm_profile="stable"), inventory=inventory)

    assert before.available == after.available
    assert before.binding_generation_fingerprint == repeated.binding_generation_fingerprint
    assert before.snapshot_hash == repeated.snapshot_hash
    assert before.binding_generation_fingerprint != after.binding_generation_fingerprint
    assert before.snapshot_hash != after.snapshot_hash


def test_bedrock_operator_binding_change_requires_rebuild_and_changes_snapshot_identity() -> None:
    def settings(version: str) -> WebSettings:
        return _settings(
            plugin_allowlist=("transform:aws_bedrock_prompt_shield",),
            bedrock_guardrail_profiles=(
                {
                    "alias": "prompt-default",
                    "plugin": "aws_bedrock_prompt_shield",
                    "guardrail_identifier": "privateguardrail",
                    "guardrail_version": version,
                    "region": "us-east-1",
                },
            ),
        )

    before_settings = settings("7")
    before = _build(before_settings)
    repeated = _build(before_settings)
    after = _build(settings("8"))

    assert before.available == after.available
    assert before.binding_generation_fingerprint == repeated.binding_generation_fingerprint
    assert before.snapshot_hash == repeated.snapshot_hash
    assert before.binding_generation_fingerprint != after.binding_generation_fingerprint
    assert before.snapshot_hash != after.snapshot_hash
    for marker in ("privateguardrail", "us-east-1", '"7"', '"8"'):
        assert marker not in before.binding_generation_fingerprint
        assert marker not in after.binding_generation_fingerprint


def test_fresh_snapshot_detects_request_scoped_credential_deletion_without_restart() -> None:
    settings = _settings(
        llm_profiles={
            "personal": {
                "provider": "openrouter",
                "model": "openai/gpt-5-mini",
                "credential_scope": "user",
                "credential_ref": "OPENROUTER_API_KEY",
            }
        },
        default_llm_profile="personal",
    )
    principal = "local:alice"

    before = _build(
        settings,
        principal=principal,
        inventory=_Inventory(
            users={principal: frozenset({"OPENROUTER_API_KEY"})},
            user_generations={(principal, "OPENROUTER_API_KEY"): "generation-one"},
        ),
    )
    after = _build(settings, principal=principal, inventory=_Inventory())

    llm_id = PluginId("transform", "llm")
    assert llm_id in before.available
    assert llm_id not in after.available
    assert dict(after.usable_profile_aliases)[llm_id] == ()
    assert before.snapshot_hash != after.snapshot_hash
