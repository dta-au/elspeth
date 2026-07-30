"""Typed operator profile settings and frozen runtime conversion."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from elspeth.contracts.freeze import freeze_fields
from elspeth.contracts.plugin_capabilities import ControlMode, PluginCapability
from elspeth.core.llm_profiles import CredentialScope, RuntimeLLMProfile
from elspeth.plugins.transforms.aws.guardrail_profiles import BedrockGuardrailProfileSettings

if TYPE_CHECKING:
    from elspeth.web.catalog.schemas import PluginSchemaInfo
    from elspeth.web.config import WebSettings
    from elspeth.web.plugin_policy.models import PluginId, WebPluginPolicy


@dataclass(frozen=True, slots=True)
class RuntimeWebPluginConfig:
    plugin_allowlist: tuple[str, ...]
    plugin_preferences: tuple[tuple[PluginCapability, tuple[str, ...]], ...]
    plugin_control_modes: tuple[tuple[PluginCapability, ControlMode], ...]
    llm_profiles: tuple[tuple[str, RuntimeLLMProfile], ...] = field(repr=False)
    default_llm_profile: str | None
    bedrock_guardrail_profiles: tuple[BedrockGuardrailProfileSettings, ...] = field(repr=False)
    bedrock_guardrail_default_profiles: tuple[tuple[str, str], ...]

    @property
    def operator_profiles(self) -> tuple[BedrockGuardrailProfileSettings, ...]:
        """Return profiles that own an authorized plugin's startup readiness."""
        return self.bedrock_guardrail_profiles

    @classmethod
    def from_settings(cls, settings: WebSettings) -> RuntimeWebPluginConfig:
        return cls(
            plugin_allowlist=tuple(settings.plugin_allowlist),
            plugin_preferences=tuple(
                (capability, tuple(plugin_ids))
                for capability, plugin_ids in sorted(settings.plugin_preferences.items(), key=lambda item: item[0].value)
            ),
            plugin_control_modes=tuple(sorted(settings.plugin_control_modes.items(), key=lambda item: item[0].value)),
            llm_profiles=tuple(
                (alias, RuntimeLLMProfile.from_settings(alias, profile)) for alias, profile in sorted(settings.llm_profiles.items())
            ),
            default_llm_profile=settings.default_llm_profile,
            bedrock_guardrail_profiles=tuple(sorted(settings.bedrock_guardrail_profiles, key=lambda profile: profile.alias)),
            bedrock_guardrail_default_profiles=tuple(sorted(settings.bedrock_guardrail_default_profiles.items())),
        )


class ProfileUnavailableReason(StrEnum):
    CREDENTIAL_MISSING = "credential_unavailable"
    LOCAL_REQUIREMENT_MISSING = "local_requirement_unavailable"


@dataclass(frozen=True, slots=True, order=True)
class ProfileAvailability:
    alias: str
    credential_scope: CredentialScope | None
    usable: bool
    reason: ProfileUnavailableReason | None = None
    generation: str | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class LocalRequirementResult:
    available: bool
    reason: ProfileUnavailableReason | None = None


@dataclass(frozen=True, slots=True)
class LoweredPluginConfig:
    executable_options: Mapping[str, object] = field(repr=False)
    audit_safe_options: Mapping[str, object]

    def __post_init__(self) -> None:
        freeze_fields(self, "executable_options", "audit_safe_options")


class ProfileCredentialInventory(Protocol):
    def has_server_ref(self, name: str) -> bool: ...

    def has_user_ref(self, principal: str, name: str) -> bool: ...

    def server_generation(self, name: str) -> str | None: ...

    def user_generation(self, principal: str, name: str) -> str | None: ...


class OperatorProfileResolver(Protocol):
    def public_schema(self, full_schema: PluginSchemaInfo, available_aliases: tuple[str, ...]) -> PluginSchemaInfo: ...

    def lower_options(self, alias: str, safe_options: dict[str, object]) -> LoweredPluginConfig: ...

    def profile_availability(
        self,
        principal: str,
        inventory: ProfileCredentialInventory,
    ) -> tuple[ProfileAvailability, ...]: ...

    def check_local_requirements(self, alias: str) -> LocalRequirementResult: ...


_LLM_PRIVATE_OPTIONS = frozenset(
    {
        "provider",
        "model",
        "api_key",
        "api_key_secret",
        "base_url",
        "endpoint",
        "deployment_name",
        "region_name",
        "api_version",
        "credential_ref",
        "credential_scope",
        "tracing",
        "timeout_seconds",
        "max_tokens",
        "pool_size",
        "min_dispatch_delay_ms",
        "max_dispatch_delay_ms",
        "backoff_multiplier",
        "recovery_step_ms",
        "max_capacity_retry_seconds",
        "prompt_template_source",
        "lookup_source",
        "system_prompt_source",
        "resolved_prompt_template_hash",
    }
)


class _LLMProfileResolver:
    def __init__(
        self,
        profiles: tuple[tuple[str, RuntimeLLMProfile], ...],
        *,
        preferred_alias: str | None,
    ) -> None:
        self._profiles = dict(profiles)
        aliases = tuple(self._profiles)
        self._ordered_aliases = (
            (preferred_alias, *(alias for alias in aliases if alias != preferred_alias)) if preferred_alias in self._profiles else aliases
        )

    def public_schema(self, full_schema: PluginSchemaInfo, available_aliases: tuple[str, ...]) -> PluginSchemaInfo:
        from elspeth.web.catalog.schemas import PluginSchemaInfo

        safe_properties: dict[str, Any] = {}
        definitions = full_schema.json_schema.get("$defs", {})
        if isinstance(definitions, dict):
            from elspeth.plugins.transforms.llm.transform import LLMTransform

            provider_definition_names = {config_cls.__name__ for config_cls in LLMTransform.discriminated_variants()[1].values()}
            for definition_name in provider_definition_names:
                definition = definitions.get(definition_name)
                if not isinstance(definition, dict):
                    continue
                properties = definition.get("properties", {})
                if not isinstance(properties, dict):
                    continue
                for name, property_schema in properties.items():
                    if name not in _LLM_PRIVATE_OPTIONS and isinstance(property_schema, dict):
                        safe_properties.setdefault(name, deepcopy(property_schema))
        safe_properties = {
            "profile": {
                "type": "string",
                "enum": list(available_aliases),
                "description": "Operator-approved LLM profile alias",
            },
            **safe_properties,
        }
        public_json_schema: dict[str, Any] = {
            "type": "object",
            "properties": safe_properties,
            "required": ["profile", "prompt_template", "schema"],
            "additionalProperties": False,
        }
        referenced_definitions: dict[str, Any] = {}
        pending = _schema_refs(public_json_schema)
        while pending:
            definition_name = pending.pop()
            if definition_name in referenced_definitions:
                continue
            definition = definitions.get(definition_name) if isinstance(definitions, dict) else None
            if isinstance(definition, dict):
                referenced_definitions[definition_name] = deepcopy(definition)
                pending.update(_schema_refs(definition))
        if referenced_definitions:
            public_json_schema["$defs"] = referenced_definitions
        fields = [
            {
                "name": name,
                "type": "string" if name == "profile" else str(schema.get("type", "object")),
                "required": name in public_json_schema["required"],
                "description": schema.get("description"),
                **({"choices": list(available_aliases)} if name == "profile" else {}),
            }
            for name, schema in safe_properties.items()
        ]
        return PluginSchemaInfo(
            name=full_schema.name,
            plugin_type=full_schema.plugin_type,
            description=full_schema.description,
            json_schema=public_json_schema,
            knob_schema={"fields": fields},
            composer_hints=full_schema.composer_hints,
            secret_requirements=(),
            web_config_authority=full_schema.web_config_authority,
            policy_capabilities=full_schema.policy_capabilities,
        )

    def lower_options(self, alias: str, safe_options: dict[str, object]) -> LoweredPluginConfig:
        if set(safe_options) & _LLM_PRIVATE_OPTIONS:
            raise ValueError("private_profile_option")
        try:
            profile = self._profiles[alias]
        except KeyError:
            raise ValueError("profile_unavailable") from None
        executable = dict(safe_options)
        executable["provider"] = profile.provider
        if profile.provider != "azure":
            executable["model"] = profile.model
        executable.update(profile.provider_options)
        if profile.credential_ref is not None:
            assert profile.credential_scope is not None
            executable["api_key"] = {
                "secret_ref": profile.credential_ref,
                "secret_scope": profile.credential_scope,
            }
        # Web-authored multi-query LLM nodes cannot set the sequential retry
        # budget themselves — ``pool_size`` and ``max_capacity_retry_seconds``
        # are private profile options rejected by the node's public schema — yet
        # the web execution-worker safety policy (``web_llm_retry_budget_policy_error``)
        # requires a bounded budget whenever ``queries`` is present, or the
        # lowered config's one-hour default would monopolise a worker. The
        # operator-profile layer supplies the web-safe default so typed
        # multi-query nodes stay both committable and run-safe.
        if executable.get("queries") is not None and "pool_size" not in executable and "max_capacity_retry_seconds" not in executable:
            from elspeth.web.provider_config_policy import WEB_LLM_SEQUENTIAL_MULTI_QUERY_MAX_RETRY_SECONDS

            executable["max_capacity_retry_seconds"] = WEB_LLM_SEQUENTIAL_MULTI_QUERY_MAX_RETRY_SECONDS
        audit_safe = {"profile": alias, **safe_options}
        return LoweredPluginConfig(
            executable_options=MappingProxyType(executable),
            audit_safe_options=MappingProxyType(audit_safe),
        )

    def profile_availability(
        self,
        principal: str,
        inventory: ProfileCredentialInventory,
    ) -> tuple[ProfileAvailability, ...]:
        result: list[ProfileAvailability] = []
        for alias in self._ordered_aliases:
            profile = self._profiles[alias]
            if profile.credential_scope is None:
                result.append(
                    ProfileAvailability(
                        alias=alias,
                        credential_scope=None,
                        usable=True,
                        generation=self._binding_generation(profile, credential_generation=None),
                    )
                )
                continue
            assert profile.credential_ref is not None
            credential_generation = (
                inventory.server_generation(profile.credential_ref)
                if profile.credential_scope == "server"
                else inventory.user_generation(principal, profile.credential_ref)
            )
            usable = credential_generation is not None
            result.append(
                ProfileAvailability(
                    alias=alias,
                    credential_scope=profile.credential_scope,
                    usable=usable,
                    reason=None if usable else ProfileUnavailableReason.CREDENTIAL_MISSING,
                    generation=(self._binding_generation(profile, credential_generation=credential_generation) if usable else None),
                )
            )
        return tuple(result)

    @staticmethod
    def _binding_generation(profile: RuntimeLLMProfile, *, credential_generation: str | None) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "provider": profile.provider,
                    "model": profile.model,
                    "credential_scope": profile.credential_scope,
                    "credential_ref": profile.credential_ref,
                    "provider_options": dict(profile.provider_options),
                    "credential_generation": credential_generation,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def check_local_requirements(self, alias: str) -> LocalRequirementResult:
        return LocalRequirementResult(available=alias in self._profiles)


_BEDROCK_PRIVATE_OPTIONS = frozenset(
    {
        "guardrail_identifier",
        "guardrail_version",
        "region",
        "endpoint",
        "endpoint_url",
        "credential",
        "credentials",
        "access_key",
        "secret_key",
        "session_token",
        "environment",
        "environment_marker",
    }
)


class _BedrockGuardrailProfileResolver:
    def __init__(self, profiles: tuple[BedrockGuardrailProfileSettings, ...], *, default_alias: str | None) -> None:
        self._profiles = {profile.alias: profile for profile in profiles}
        aliases = tuple(self._profiles)
        self._ordered_aliases = (
            (default_alias, *(alias for alias in aliases if alias != default_alias)) if default_alias in self._profiles else aliases
        )

    def public_schema(self, full_schema: PluginSchemaInfo, available_aliases: tuple[str, ...]) -> PluginSchemaInfo:
        from elspeth.web.catalog.schemas import PluginSchemaInfo

        safe_names = ("fields", "schema") if full_schema.name == "aws_bedrock_prompt_shield" else ("fields", "schema", "source")
        full_properties = full_schema.json_schema.get("properties", {})
        safe_properties: dict[str, Any] = {
            "profile": {
                "type": "string",
                "enum": list(available_aliases),
                "description": "Operator-approved Bedrock Guardrail profile alias",
            }
        }
        if isinstance(full_properties, dict):
            for name in safe_names:
                value = full_properties.get(name)
                if isinstance(value, dict):
                    safe_properties[name] = deepcopy(value)
        required = ["profile", "fields", "schema"]
        public_json_schema: dict[str, Any] = {
            "type": "object",
            "properties": safe_properties,
            "required": required,
            "additionalProperties": False,
        }
        definitions = full_schema.json_schema.get("$defs", {})
        referenced_definitions: dict[str, Any] = {}
        pending = _schema_refs(public_json_schema)
        while pending:
            definition_name = pending.pop()
            if definition_name in referenced_definitions:
                continue
            definition = definitions.get(definition_name) if isinstance(definitions, dict) else None
            if isinstance(definition, dict):
                referenced_definitions[definition_name] = deepcopy(definition)
                pending.update(_schema_refs(definition))
        if referenced_definitions:
            public_json_schema["$defs"] = referenced_definitions
        fields = [
            {
                "name": name,
                "type": "string" if name == "profile" else str(schema.get("type", "object")),
                "required": name in required,
                "description": schema.get("description"),
                **({"choices": list(available_aliases)} if name == "profile" else {}),
            }
            for name, schema in safe_properties.items()
        ]
        return PluginSchemaInfo(
            name=full_schema.name,
            plugin_type=full_schema.plugin_type,
            description=full_schema.description,
            json_schema=public_json_schema,
            knob_schema={"fields": fields},
            composer_hints=full_schema.composer_hints,
            secret_requirements=(),
            web_config_authority=full_schema.web_config_authority,
            policy_capabilities=full_schema.policy_capabilities,
        )

    def lower_options(self, alias: str, safe_options: dict[str, object]) -> LoweredPluginConfig:
        if set(safe_options) & _BEDROCK_PRIVATE_OPTIONS:
            raise ValueError("private_profile_option")
        try:
            profile = self._profiles[alias]
        except KeyError:
            raise ValueError("profile_unavailable") from None
        allowed = {"fields", "schema"}
        if profile.plugin == "aws_bedrock_content_safety":
            allowed.add("source")
        if set(safe_options) - allowed:
            raise ValueError("private_profile_option")
        executable = dict(safe_options)
        executable.update(
            {
                "guardrail_identifier": profile.guardrail_identifier,
                "guardrail_version": profile.guardrail_version,
                "region": profile.region,
            }
        )
        return LoweredPluginConfig(
            executable_options=MappingProxyType(executable),
            audit_safe_options=MappingProxyType({"profile": alias, **safe_options}),
        )

    def profile_availability(
        self,
        principal: str,
        inventory: ProfileCredentialInventory,
    ) -> tuple[ProfileAvailability, ...]:
        del principal, inventory
        result: list[ProfileAvailability] = []
        for alias in self._ordered_aliases:
            profile = self._profiles[alias]
            binding_generation = hashlib.sha256(
                json.dumps(
                    {
                        "guardrail_identifier": profile.guardrail_identifier,
                        "guardrail_version": profile.guardrail_version,
                        "region": profile.region,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            result.append(
                ProfileAvailability(
                    alias=alias,
                    credential_scope=None,
                    usable=True,
                    generation=binding_generation,
                )
            )
        return tuple(result)

    def check_local_requirements(self, alias: str) -> LocalRequirementResult:
        profile = self._profiles.get(alias)
        if profile is None or not profile.check_local_requirements().available:
            return LocalRequirementResult(available=False, reason=ProfileUnavailableReason.LOCAL_REQUIREMENT_MISSING)
        return LocalRequirementResult(available=True)

    def approved_profile(self, alias: str) -> BedrockGuardrailProfileSettings:
        """Return one exact frozen operator binding for an already-authorized plugin."""

        try:
            return self._profiles[alias]
        except KeyError:
            raise ValueError("profile_unavailable") from None


def _schema_refs(value: object) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            refs.add(ref.removeprefix("#/$defs/"))
        for nested in value.values():
            refs.update(_schema_refs(nested))
    elif isinstance(value, list):
        for nested in value:
            refs.update(_schema_refs(nested))
    return refs


class OperatorProfileRegistry:
    """Resolver registry bound to one frozen process policy/config."""

    def __init__(self, *, policy: WebPluginPolicy, settings: RuntimeWebPluginConfig) -> None:
        from elspeth.web.plugin_policy.models import PluginId

        self._policy = policy
        self._resolvers: dict[PluginId, OperatorProfileResolver] = {
            PluginId("transform", "llm"): _LLMProfileResolver(
                settings.llm_profiles,
                preferred_alias=settings.default_llm_profile,
            )
        }
        defaults = dict(settings.bedrock_guardrail_default_profiles)
        for plugin_name in ("aws_bedrock_prompt_shield", "aws_bedrock_content_safety"):
            plugin_profiles = tuple(profile for profile in settings.bedrock_guardrail_profiles if profile.plugin == plugin_name)
            if plugin_profiles:
                self._resolvers[PluginId("transform", plugin_name)] = _BedrockGuardrailProfileResolver(
                    plugin_profiles,
                    default_alias=defaults.get(plugin_name),
                )

    def public_schema(
        self,
        plugin_id: PluginId,
        full_schema: PluginSchemaInfo,
        *,
        available_aliases: tuple[str, ...],
    ) -> PluginSchemaInfo:
        resolver = self._resolvers.get(plugin_id)
        if resolver is None:
            return full_schema
        return resolver.public_schema(full_schema, available_aliases)

    def lower_options(
        self,
        plugin_id: PluginId,
        *,
        alias: str,
        safe_options: dict[str, object],
    ) -> LoweredPluginConfig:
        try:
            resolver = self._resolvers[plugin_id]
        except KeyError:
            raise ValueError("plugin_has_no_operator_profile") from None
        return resolver.lower_options(alias, safe_options)

    def profile_availability(
        self,
        plugin_id: PluginId,
        *,
        principal: str,
        inventory: ProfileCredentialInventory,
    ) -> tuple[ProfileAvailability, ...]:
        try:
            resolver = self._resolvers[plugin_id]
        except KeyError:
            return ()
        return resolver.profile_availability(principal, inventory)

    def check_local_requirements(self, plugin_id: PluginId, alias: str) -> LocalRequirementResult:
        try:
            resolver = self._resolvers[plugin_id]
        except KeyError:
            return LocalRequirementResult(available=False, reason=ProfileUnavailableReason.LOCAL_REQUIREMENT_MISSING)
        return resolver.check_local_requirements(alias)

    def approved_bedrock_guardrail_profile(
        self,
        plugin_id: PluginId,
        *,
        alias: str,
    ) -> BedrockGuardrailProfileSettings:
        """Resolve one authorized opaque alias to its frozen private binding."""

        if plugin_id not in self._policy.authorized:
            raise ValueError("profile_unavailable")
        resolver = self._resolvers.get(plugin_id)
        if not isinstance(resolver, _BedrockGuardrailProfileResolver):
            raise ValueError("profile_unavailable")
        return resolver.approved_profile(alias)
