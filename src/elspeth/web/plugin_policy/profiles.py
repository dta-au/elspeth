"""Typed operator profile settings and frozen runtime conversion."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from elspeth.contracts.aws_s3 import (
    S3_MAX_KEY_BYTES,
    S3_PRIVATE_BINDING_OPTION_NAMES,
    S3_PROFILED_AUTHOR_OPTION_NAMES,
    S3ProfiledAuditIdentity,
    s3_profiled_binding_fingerprint,
    validate_relative_s3_path,
)
from elspeth.contracts.aws_textract import (
    TEXTRACT_PRIVATE_BINDING_OPTION_NAMES,
    TEXTRACT_PROFILED_AUTHOR_OPTION_NAMES,
    TextractProfiledAuditIdentity,
    textract_profiled_binding_fingerprint,
)
from elspeth.contracts.freeze import freeze_fields
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.plugin_capabilities import ControlMode, PluginCapability
from elspeth.contracts.trust_boundary import observation_boundary, trust_boundary
from elspeth.contracts.wire_visible_identity import reject_operator_required_placeholder_value
from elspeth.core.llm_profiles import (
    LLM_PROFILE_PRIVATE_FIELDS,
    CredentialScope,
    RuntimeLLMProfile,
    lower_llm_profile_options,
    validate_profile_alias,
)
from elspeth.plugins.transforms.aws.guardrail_profiles import BedrockGuardrailProfileSettings
from elspeth.plugins.transforms.aws.textract_regions import is_supported_textract_region, is_well_formed_aws_region

if TYPE_CHECKING:
    from elspeth.web.catalog.schemas import PluginSchemaInfo, PluginSummary
    from elspeth.web.config import WebSettings
    from elspeth.web.plugin_policy.models import PluginId, WebPluginPolicy


_S3_MAX_BUCKET_CHARS = 2048


class AWSS3SourceProfileSettings(BaseModel):
    """Operator-owned binding for one Web-authorable S3 source location."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    alias: str
    bucket: str = Field(repr=False)
    prefix: str | None = Field(default=None, repr=False)

    @field_validator("alias")
    @classmethod
    def _validate_alias(cls, value: str) -> str:
        validate_profile_alias(value)
        return value

    @field_validator("bucket")
    @classmethod
    def _validate_bucket(cls, value: str) -> str:
        if not value or value != value.strip() or len(value) > _S3_MAX_BUCKET_CHARS:
            raise ValueError("bucket must be non-blank, canonical, and at most 2048 characters")
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
            raise ValueError("bucket must not contain control characters")
        return reject_operator_required_placeholder_value(value, field_name="bucket")

    @field_validator("prefix")
    @classmethod
    def _validate_prefix(cls, value: str | None) -> str | None:
        if value is None:
            return None
        validated = validate_relative_s3_path(value, field_name="prefix")
        if len(validated.encode("utf-8")) > S3_MAX_KEY_BYTES - 2:
            raise ValueError("prefix must leave room for a relative S3 object key")
        return validated


class AWSTextractProfileSettings(BaseModel):
    """Operator-owned binding for one Web-authorable Textract document location."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    alias: str
    bucket: str = Field(repr=False)
    key_prefix: str | None = Field(default=None, repr=False)

    @field_validator("alias")
    @classmethod
    def _validate_alias(cls, value: str) -> str:
        validate_profile_alias(value)
        return value

    @field_validator("bucket")
    @classmethod
    def _validate_bucket(cls, value: str) -> str:
        if not value or value != value.strip() or len(value) > _S3_MAX_BUCKET_CHARS:
            raise ValueError("bucket must be non-blank, canonical, and at most 2048 characters")
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
            raise ValueError("bucket must not contain control characters")
        return reject_operator_required_placeholder_value(value, field_name="bucket")

    @field_validator("key_prefix")
    @classmethod
    def _validate_key_prefix(cls, value: str | None) -> str | None:
        if value is None:
            return None
        validated = validate_relative_s3_path(value, field_name="key_prefix")
        if len(validated.encode("utf-8")) > S3_MAX_KEY_BYTES - 2:
            raise ValueError("key_prefix must leave room for a relative S3 object key")
        return validated


@dataclass(frozen=True, slots=True)
class RuntimeWebPluginConfig:
    plugin_allowlist: tuple[str, ...]
    plugin_preferences: tuple[tuple[PluginCapability, tuple[str, ...]], ...]
    plugin_control_modes: tuple[tuple[PluginCapability, ControlMode], ...]
    llm_profiles: tuple[tuple[str, RuntimeLLMProfile], ...] = field(repr=False)
    default_llm_profile: str | None
    bedrock_guardrail_profiles: tuple[BedrockGuardrailProfileSettings, ...] = field(repr=False)
    bedrock_guardrail_default_profiles: tuple[tuple[str, str], ...]
    aws_s3_source_profiles: tuple[AWSS3SourceProfileSettings, ...] = field(repr=False)
    aws_textract_profiles: tuple[AWSTextractProfileSettings, ...] = field(repr=False)
    deployment_aws_region: str | None

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
            aws_s3_source_profiles=tuple(sorted(settings.aws_s3_source_profiles, key=lambda profile: profile.alias)),
            aws_textract_profiles=tuple(sorted(settings.aws_textract_profiles, key=lambda profile: profile.alias)),
            deployment_aws_region=settings.deployment_aws_region,
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
    profiled_s3_audit_identity: S3ProfiledAuditIdentity | None = None
    profiled_textract_audit_identity: TextractProfiledAuditIdentity | None = None

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

    def selected_alias(self, usable_aliases: tuple[str, ...]) -> str | None: ...


class _LLMProfileResolver:
    def __init__(
        self,
        profiles: tuple[tuple[str, RuntimeLLMProfile], ...],
        *,
        preferred_alias: str | None,
    ) -> None:
        self._profiles = dict(profiles)
        aliases = tuple(self._profiles)
        self._preferred_alias = preferred_alias if preferred_alias in self._profiles else None
        self._ordered_aliases = (
            (self._preferred_alias, *(alias for alias in aliases if alias != self._preferred_alias))
            if self._preferred_alias is not None
            else aliases
        )

    def selected_alias(self, usable_aliases: tuple[str, ...]) -> str | None:
        """The operator-designated default, and only that — never a stand-in.

        A missing ``default_llm_profile`` is a supported degraded state: the
        aliases stay individually authorable, but no alias is the house
        default, and promoting whichever one sorts first would point the
        Composer at a provider/model the operator never designated. The same
        holds when the designated default is not usable by this principal —
        substituting the next usable alias swaps providers silently.
        """
        if self._preferred_alias is not None and self._preferred_alias in usable_aliases:
            return self._preferred_alias
        return None

    @trust_boundary(
        tier=3,
        source="PluginSchemaInfo.json_schema for an LLM plugin: a pydantic-discriminated-union JSON Schema whose $defs/discriminator/properties shape is generated from plugin-author-owned provider config models",
        source_param="full_schema",
        suppresses=("R1", "R5"),
        invariant=(
            "raises ValueError('malformed_profile_schema') when $defs, discriminator, discriminator['mapping'], a "
            "mapped provider definition, its properties, or its required list deviates from the pydantic-generated "
            "discriminated-union shape (dict / list[str]) — the section that decides public-vs-LLM_PROFILE_PRIVATE_FIELDS "
            "exposure never silently narrows there. The downstream $ref-closure walk that assembles public $defs, and "
            "the source knob-field projection, are unchanged pre-existing best-effort behaviour and are not covered "
            "by this invariant. An ABSENT key is not a softer path into the exposure decision: a discriminator "
            "naming a $defs entry the schema does not carry raises, and with neither $defs nor discriminator the "
            "projection collapses to the operator-approved 'profile' alias alone — an absent key can only narrow "
            "this public schema, never admit a provider field the union did not declare."
        ),
        test_ref="tests/unit/web/plugin_policy/test_profiles.py::test_llm_public_schema_rejects_malformed_defs_shape",
        test_fingerprint="607c1d68c9a2b0168d5172e35bb81bd5b514e0cb59ae4bcd945f5068ced0c577",
    )
    def public_schema(self, full_schema: PluginSchemaInfo, available_aliases: tuple[str, ...]) -> PluginSchemaInfo:
        from elspeth.web.catalog.schemas import PluginSchemaInfo

        safe_properties: dict[str, Any] = {}
        definitions = full_schema.json_schema.get("$defs", {})
        if not isinstance(definitions, dict):
            raise ValueError("malformed_profile_schema")
        discriminator = full_schema.json_schema.get("discriminator", {})
        if not isinstance(discriminator, dict):
            raise ValueError("malformed_profile_schema")
        mapping = discriminator.get("mapping", {})
        if not isinstance(mapping, dict):
            raise ValueError("malformed_profile_schema")
        provider_definitions: list[dict[str, Any]] = []
        for definition_ref in mapping.values():
            if not isinstance(definition_ref, str) or not definition_ref.startswith("#/$defs/"):
                raise ValueError("malformed_profile_schema")
            definition = definitions.get(definition_ref.removeprefix("#/$defs/"))
            if not isinstance(definition, dict):
                raise ValueError("malformed_profile_schema")
            provider_definitions.append(definition)
            properties = definition.get("properties", {})
            if not isinstance(properties, dict):
                raise ValueError("malformed_profile_schema")
            for name, property_schema in properties.items():
                if name in LLM_PROFILE_PRIVATE_FIELDS:
                    continue
                if not isinstance(property_schema, dict):
                    raise ValueError("malformed_profile_schema")
                # First variant wins: the union's variants share these public
                # property names and the projection publishes one shape for each.
                if name not in safe_properties:
                    safe_properties[name] = deepcopy(property_schema)
        required_by_variant: list[set[str]] = []
        for definition in provider_definitions:
            # An absent "required" is itself a shape deviation, not "nothing is
            # required": every pydantic-generated variant of a discriminated
            # union carries one, so its absence means the schema did not come
            # from the declared source and the exposure decision below cannot be
            # made from it. ``list``/``str`` are exact because the schema is
            # json/pydantic-generated, never a subclass or a frozen container.
            required_names = definition["required"] if "required" in definition else None
            if type(required_names) is not list or any(type(name) is not str for name in required_names):
                raise ValueError("malformed_profile_schema")
            required_by_variant.append(set(required_names))
        required_in_all_variants = set.intersection(*required_by_variant) if required_by_variant else set()
        safe_properties = {
            "profile": {
                "type": "string",
                "enum": list(available_aliases),
                "description": "Operator-approved LLM profile alias",
            },
            **safe_properties,
        }
        required_properties = [name for name in safe_properties if name in required_in_all_variants]
        if full_schema.plugin_type == "transform":
            # Preserve the established transform projection byte-for-byte;
            # source variants add their own required routing field below the
            # naturally ordered common properties.
            established_order = ("prompt_template", "schema")
            required_properties = [
                *(name for name in established_order if name in required_properties),
                *(name for name in required_properties if name not in established_order),
            ]
        public_json_schema: dict[str, Any] = {
            "type": "object",
            "properties": safe_properties,
            "required": ["profile", *required_properties],
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
        if full_schema.plugin_type == "source":
            raw_fields = full_schema.knob_schema.get("fields", ())
            first_raw_field: dict[str, dict[str, Any]] = {}
            for field in raw_fields:
                if isinstance(field, dict) and isinstance(field.get("name"), str) and field["name"] not in first_raw_field:
                    first_raw_field[field["name"]] = field
            fields: list[dict[str, Any]] = [
                {
                    "name": "profile",
                    "label": "profile",
                    "kind": "enum",
                    "required": True,
                    "nullable": False,
                    "enum": list(available_aliases),
                    "description": "Operator-approved LLM profile alias",
                }
            ]
            for name in safe_properties:
                if name == "profile":
                    continue
                try:
                    field = deepcopy(first_raw_field[name])
                except KeyError as exc:
                    raise ValueError(f"source profile field {name!r} has no canonical knob projection") from exc
                if "visible_when" in field:
                    del field["visible_when"]
                fields.append(field)
        else:
            # Keep the established transform policy-view projection
            # byte-compatible. Source forms use the current-schema knob wire
            # contract because Guided Step 1 consumes them directly.
            fields = [
                {
                    "name": name,
                    "type": "string" if name == "profile" else str(schema["type"] if "type" in schema else "object"),
                    "required": name in public_json_schema["required"],
                    # The composer-surface help text substitutes the CLI/YAML
                    # description on web knobs (mirrors
                    # web/catalog/knob_schema._composer_description); a present
                    # but empty composer_description falls through to description.
                    "description": (schema["composer_description"] if "composer_description" in schema else None)
                    or (schema["description"] if "description" in schema else None),
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
        if set(safe_options) & LLM_PROFILE_PRIVATE_FIELDS:
            raise ValueError("private_profile_option")
        try:
            profile = self._profiles[alias]
        except KeyError:
            raise ValueError("profile_unavailable") from None
        executable, audit_safe = lower_llm_profile_options(alias, profile, safe_options, private_fields=LLM_PROFILE_PRIVATE_FIELDS)
        # Web-authored multi-query LLM nodes cannot set the sequential retry
        # budget themselves — ``pool_size`` and ``max_capacity_retry_seconds``
        # are private profile options rejected by the node's public schema — yet
        # the web execution-worker safety policy (``web_llm_retry_budget_policy_error``)
        # requires a bounded budget whenever ``queries`` is present, or the
        # lowered config's one-hour default would monopolise a worker. The
        # operator-profile layer supplies the web-safe default so typed
        # multi-query nodes stay both committable and run-safe.
        if (
            "queries" in executable
            and executable["queries"] is not None
            and "pool_size" not in executable
            and "max_capacity_retry_seconds" not in executable
        ):
            from elspeth.web.provider_config_policy import WEB_LLM_SEQUENTIAL_MULTI_QUERY_MAX_RETRY_SECONDS

            executable["max_capacity_retry_seconds"] = WEB_LLM_SEQUENTIAL_MULTI_QUERY_MAX_RETRY_SECONDS
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

    @trust_boundary(
        tier=3,
        source="PluginSchemaInfo.json_schema for a Bedrock Guardrail plugin: a plugin-author-owned JSON Schema whose properties shape is generated from the plugin's config model",
        source_param="full_schema",
        suppresses=("R1", "R5"),
        invariant=(
            "raises ValueError('malformed_profile_schema') if full_schema.json_schema['properties'] is not a mapping, "
            "or if the always-required 'fields'/'schema' properties are present but not mappings — never emits a "
            "'required' list naming a property absent from 'properties', which would be an unsatisfiable schema. "
            "An ABSENT 'properties' reaches that same rejection, because 'fields'/'schema' are always required"
        ),
        test_ref="tests/unit/web/plugin_policy/test_profiles.py::test_bedrock_public_schema_rejects_non_mapping_properties",
        test_fingerprint="629473a1e9f8d1697fa7861f5ca3f2e71a679a85875e2e4be6d1dde2f2b1033b",
    )
    def public_schema(self, full_schema: PluginSchemaInfo, available_aliases: tuple[str, ...]) -> PluginSchemaInfo:
        from elspeth.web.catalog.schemas import PluginSchemaInfo

        safe_names = ("fields", "schema") if full_schema.name == "aws_bedrock_prompt_shield" else ("fields", "schema", "source")
        full_properties = full_schema.json_schema.get("properties", {})
        if not isinstance(full_properties, dict):
            raise ValueError("malformed_profile_schema")
        safe_properties: dict[str, Any] = {
            "profile": {
                "type": "string",
                "enum": list(available_aliases),
                "description": "Operator-approved Bedrock Guardrail profile alias",
            }
        }
        required = ["profile", "fields", "schema"]
        for name in safe_names:
            value = full_properties.get(name)
            if value is None:
                if name in required:
                    raise ValueError("malformed_profile_schema")
                continue
            if not isinstance(value, dict):
                raise ValueError("malformed_profile_schema")
            safe_properties[name] = deepcopy(value)
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
                "type": "string" if name == "profile" else str(schema["type"] if "type" in schema else "object"),
                "required": name in required,
                # Mirrors the LLM profile lowering above: composer-surface help
                # text substitutes the CLI/YAML description on web knobs, and a
                # present but empty composer_description falls through.
                "description": (schema["composer_description"] if "composer_description" in schema else None)
                or (schema["description"] if "description" in schema else None),
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
        if alias not in self._profiles or not self._profiles[alias].check_local_requirements().available:
            return LocalRequirementResult(available=False, reason=ProfileUnavailableReason.LOCAL_REQUIREMENT_MISSING)
        return LocalRequirementResult(available=True)

    def selected_alias(self, usable_aliases: tuple[str, ...]) -> str | None:
        # Config validation guarantees an explicit default whenever more than
        # one profile exists, and ``_ordered_aliases`` puts it first — so the
        # first usable alias IS the designated (or sole) profile.
        return usable_aliases[0] if usable_aliases else None

    def approved_profile(self, alias: str) -> BedrockGuardrailProfileSettings:
        """Return one exact frozen operator binding for an already-authorized plugin."""

        try:
            return self._profiles[alias]
        except KeyError:
            raise ValueError("profile_unavailable") from None


class _S3SourceProfileResolver:
    def __init__(self, profiles: tuple[AWSS3SourceProfileSettings, ...], *, region: str) -> None:
        if not profiles or not is_well_formed_aws_region(region):
            raise ValueError("profile_unavailable")
        self._profiles = {profile.alias: profile for profile in profiles}
        self._region = region

    @trust_boundary(
        tier=3,
        source="PluginSchemaInfo.json_schema for the aws_s3 source plugin: a plugin-author-owned JSON Schema whose properties shape is generated from the plugin's config model",
        source_param="full_schema",
        suppresses=("R1", "R5"),
        invariant=(
            "raises ValueError('malformed_profile_schema') if full_schema.json_schema['properties'] is not a "
            "mapping or if any S3_PROFILED_AUTHOR_OPTION_NAMES entry is present but not a mapping; never silently "
            "narrows the profiled-author-visible option set. An ABSENT key is not a softer path into that set: "
            "'properties' absent reaches the same rejection because every profiled-author option is read by name, "
            "an absent knob 'fields' list reaches it through the canonical-projection lookup, an absent 'required' "
            "carries JSON Schema's own 'nothing is required', and an absent '$defs' is reachable only for a schema "
            "carrying no $ref, since the declared pydantic-generated source never emits one without the other"
        ),
        test_ref="tests/unit/web/plugin_policy/test_profiles.py::test_s3_public_schema_rejects_non_mapping_properties",
        test_fingerprint="5d4be073e45335f7ec4883ab4c3e6d0ead594be3879bde781f6428b97760a761",
    )
    def public_schema(self, full_schema: PluginSchemaInfo, available_aliases: tuple[str, ...]) -> PluginSchemaInfo:
        from elspeth.web.catalog.schemas import PluginSchemaInfo

        raw_properties = full_schema.json_schema.get("properties", {})
        if not isinstance(raw_properties, dict):
            raise ValueError("malformed_profile_schema")
        safe_properties: dict[str, Any] = {
            "profile": {
                "type": "string",
                "enum": list(available_aliases),
                "description": "Operator-approved S3 source profile alias",
            }
        }
        for name in S3_PROFILED_AUTHOR_OPTION_NAMES:
            raw_schema = raw_properties.get(name)
            if not isinstance(raw_schema, dict):
                raise ValueError("malformed_profile_schema")
            safe_properties[name] = deepcopy(raw_schema)
        safe_properties["key"].update(
            {
                "description": "Relative object key within the operator-approved S3 source prefix",
                "pattern": (
                    r"^(?!/)(?![A-Za-z][A-Za-z0-9+.-]*:)(?!.*\\)"
                    r"(?!.*(?:^|/)(?:\.|\.\.)(?:/|$))(?!.*//)(?!.*\/$).+$"
                ),
            }
        )
        raw_required = full_schema.json_schema.get("required", ())
        required = [
            "profile",
            *(name for name in raw_required if isinstance(name, str) and name in S3_PROFILED_AUTHOR_OPTION_NAMES),
        ]
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

        raw_fields = full_schema.knob_schema.get("fields", ())
        canonical_fields = {
            raw_field["name"]: raw_field
            for raw_field in raw_fields
            if isinstance(raw_field, dict) and isinstance(raw_field.get("name"), str)
        }
        fields: list[dict[str, Any]] = [
            {
                "name": "profile",
                "label": "Profile",
                "kind": "enum",
                "required": True,
                "nullable": False,
                "enum": list(available_aliases),
                "description": "Operator-approved S3 source profile alias",
            }
        ]
        for name in S3_PROFILED_AUTHOR_OPTION_NAMES:
            try:
                field_projection = deepcopy(canonical_fields[name])
            except KeyError as exc:
                raise ValueError("malformed_profile_schema") from exc
            field_projection["required"] = name in required
            if name == "key":
                field_projection["label"] = "Relative Object Key"
                field_projection["description"] = "Relative object key within the operator-approved S3 source prefix"
            fields.append(field_projection)
        return PluginSchemaInfo(
            name=full_schema.name,
            plugin_type=full_schema.plugin_type,
            description=full_schema.description,
            json_schema=public_json_schema,
            knob_schema={"fields": fields},
            composer_hints=(
                "Select an operator-approved S3 source profile and provide only a relative object key.",
                "The server supplies the profile's private runtime binding.",
                "Choose the parser, schema, and validation-failure routing for the selected object.",
            ),
            secret_requirements=(),
            web_config_authority=full_schema.web_config_authority,
            policy_capabilities=full_schema.policy_capabilities,
        )

    def lower_options(self, alias: str, safe_options: dict[str, object]) -> LoweredPluginConfig:
        try:
            profile = self._profiles[alias]
        except KeyError:
            raise ValueError("profile_unavailable") from None
        if set(safe_options) & S3_PRIVATE_BINDING_OPTION_NAMES:
            raise ValueError("private_profile_option")
        if set(safe_options) - set(S3_PROFILED_AUTHOR_OPTION_NAMES):
            raise ValueError("private_profile_option")
        relative_key = safe_options["key"] if "key" in safe_options else None
        if type(relative_key) is not str:
            raise ValueError("unsafe_s3_object_key")
        try:
            relative_key = validate_relative_s3_path(relative_key, field_name="key")
        except ValueError:
            raise ValueError("unsafe_s3_object_key") from None
        executable_key = f"{profile.prefix}/{relative_key}" if profile.prefix is not None else relative_key
        if len(executable_key.encode("utf-8")) > S3_MAX_KEY_BYTES:
            raise ValueError("unsafe_s3_object_key")
        executable = {
            **safe_options,
            "bucket": profile.bucket,
            "key": executable_key,
            "region_name": self._region,
        }
        return LoweredPluginConfig(
            executable_options=MappingProxyType(executable),
            audit_safe_options=MappingProxyType({"profile": alias, **safe_options}),
            profiled_s3_audit_identity=S3ProfiledAuditIdentity(
                profile_alias=alias,
                relative_key=relative_key,
                binding_fingerprint=s3_profiled_binding_fingerprint(
                    bucket=profile.bucket,
                    executable_key=executable_key,
                    region_name=self._region,
                    endpoint_url=None,
                ),
            ),
        )

    def profile_availability(
        self,
        principal: str,
        inventory: ProfileCredentialInventory,
    ) -> tuple[ProfileAvailability, ...]:
        del principal, inventory
        return tuple(
            ProfileAvailability(
                alias=alias,
                credential_scope=None,
                usable=True,
                generation=hashlib.sha256(
                    json.dumps(
                        {
                            "bucket": profile.bucket,
                            "prefix": profile.prefix,
                            "region_name": self._region,
                            "auth_mode": "default_chain",
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            )
            for alias, profile in self._profiles.items()
        )

    def check_local_requirements(self, alias: str) -> LocalRequirementResult:
        return LocalRequirementResult(available=alias in self._profiles)

    def selected_alias(self, usable_aliases: tuple[str, ...]) -> str | None:
        return usable_aliases[0] if len(usable_aliases) == 1 else None


class _TextractProfileResolver:
    def __init__(self, profiles: tuple[AWSTextractProfileSettings, ...], *, region: str) -> None:
        if not profiles or not is_supported_textract_region(region):
            raise ValueError("profile_unavailable")
        self._profiles = {profile.alias: profile for profile in profiles}
        self._region = region

    @trust_boundary(
        tier=3,
        source="PluginSchemaInfo.json_schema for the aws_textract_document_analysis transform plugin: a plugin-author-owned JSON Schema whose properties shape is generated from the plugin's config model",
        source_param="full_schema",
        suppresses=("R1", "R5"),
        invariant=(
            "raises ValueError('malformed_profile_schema') if full_schema.json_schema['properties'] is not a "
            "mapping or if any TEXTRACT_PROFILED_AUTHOR_OPTION_NAMES entry is present but not a mapping; never "
            "silently narrows the profiled-author-visible option set. An ABSENT key is not a softer path into that "
            "set: 'properties' absent reaches the same rejection because every profiled-author option is read by "
            "name, an absent knob 'fields' list reaches it through the canonical-projection lookup, an absent "
            "'required' carries JSON Schema's own 'nothing is required', and an absent '$defs' is reachable only "
            "for a schema carrying no $ref, since the declared pydantic-generated source never emits one without "
            "the other"
        ),
        test_ref="tests/unit/web/plugin_policy/test_profiles.py::test_textract_public_schema_rejects_non_mapping_properties",
        test_fingerprint="9f2da2dcb0d7727bcaa893abb5aba4663bfd75f5e4661d1b60756bddfa050b94",
    )
    def public_schema(self, full_schema: PluginSchemaInfo, available_aliases: tuple[str, ...]) -> PluginSchemaInfo:
        from elspeth.web.catalog.schemas import PluginSchemaInfo

        raw_properties = full_schema.json_schema.get("properties", {})
        if not isinstance(raw_properties, dict):
            raise ValueError("malformed_profile_schema")
        safe_properties: dict[str, Any] = {
            "profile": {
                "type": "string",
                "enum": list(available_aliases),
                "description": "Operator-approved Textract document profile alias",
            }
        }
        for name in TEXTRACT_PROFILED_AUTHOR_OPTION_NAMES:
            raw_schema = raw_properties.get(name)
            if not isinstance(raw_schema, dict):
                raise ValueError("malformed_profile_schema")
            safe_properties[name] = deepcopy(raw_schema)
        safe_properties["key_field"].update(
            {
                "description": ("Input row field containing the relative S3 object key within the operator-approved document location"),
            }
        )
        raw_required = full_schema.json_schema.get("required", ())
        required = [
            "profile",
            *(name for name in raw_required if isinstance(name, str) and name in TEXTRACT_PROFILED_AUTHOR_OPTION_NAMES),
        ]
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

        raw_fields = full_schema.knob_schema.get("fields", ())
        canonical_fields = {
            raw_field["name"]: raw_field
            for raw_field in raw_fields
            if isinstance(raw_field, dict) and isinstance(raw_field.get("name"), str)
        }
        fields: list[dict[str, Any]] = [
            {
                "name": "profile",
                "type": "string",
                "required": True,
                "description": "Operator-approved Textract document profile alias",
                "choices": list(available_aliases),
            }
        ]
        for name in TEXTRACT_PROFILED_AUTHOR_OPTION_NAMES:
            try:
                field_projection = deepcopy(canonical_fields[name])
            except KeyError as exc:
                raise ValueError("malformed_profile_schema") from exc
            field_projection["required"] = name in required
            if name == "key_field":
                field_projection["description"] = (
                    "Input row field containing the relative S3 object key within the operator-approved document location"
                )
            fields.append(field_projection)
        return PluginSchemaInfo(
            name=full_schema.name,
            plugin_type=full_schema.plugin_type,
            description=full_schema.description,
            json_schema=public_json_schema,
            knob_schema={"fields": fields},
            composer_hints=(
                "Select an operator-approved Textract document profile; the server supplies the private storage binding.",
                "Rows carry relative object keys in key_field — never bucket names or document locations.",
                "Choose feature_types and map at least one output field.",
            ),
            secret_requirements=(),
            web_config_authority=full_schema.web_config_authority,
            policy_capabilities=full_schema.policy_capabilities,
        )

    def lower_options(self, alias: str, safe_options: dict[str, object]) -> LoweredPluginConfig:
        try:
            profile = self._profiles[alias]
        except KeyError:
            raise ValueError("profile_unavailable") from None
        if set(safe_options) & TEXTRACT_PRIVATE_BINDING_OPTION_NAMES:
            raise ValueError("private_profile_option")
        if set(safe_options) - set(TEXTRACT_PROFILED_AUTHOR_OPTION_NAMES):
            raise ValueError("private_profile_option")
        executable: dict[str, object] = {
            **safe_options,
            "bucket": profile.bucket,
            "region": self._region,
            "auth_mode": "default_chain",
        }
        if profile.key_prefix is not None:
            executable["key_prefix"] = profile.key_prefix
        return LoweredPluginConfig(
            executable_options=MappingProxyType(executable),
            audit_safe_options=MappingProxyType({"profile": alias, **safe_options}),
            profiled_textract_audit_identity=TextractProfiledAuditIdentity(
                profile_alias=alias,
                binding_fingerprint=textract_profiled_binding_fingerprint(
                    bucket=profile.bucket,
                    region=self._region,
                    key_prefix=profile.key_prefix,
                ),
            ),
        )

    def profile_availability(
        self,
        principal: str,
        inventory: ProfileCredentialInventory,
    ) -> tuple[ProfileAvailability, ...]:
        del principal, inventory
        return tuple(
            ProfileAvailability(
                alias=alias,
                credential_scope=None,
                usable=True,
                generation=hashlib.sha256(
                    json.dumps(
                        {
                            "bucket": profile.bucket,
                            "key_prefix": profile.key_prefix,
                            "region": self._region,
                            "auth_mode": "default_chain",
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            )
            for alias, profile in self._profiles.items()
        )

    def check_local_requirements(self, alias: str) -> LocalRequirementResult:
        return LocalRequirementResult(available=alias in self._profiles)

    def selected_alias(self, usable_aliases: tuple[str, ...]) -> str | None:
        return usable_aliases[0] if len(usable_aliases) == 1 else None


@observation_boundary(
    tier=3,
    source="a node of a plugin-author-owned JSON Schema (PluginSchemaInfo.json_schema) being walked for $defs references",
    source_param="value",
    suppresses=("R1", "R5"),
    invariant=(
        "returns only the '#/$defs/<name>' references it can prove present; any non-mapping, non-list, or "
        "non-string $ref contributes nothing and is never coerced. Never raises on a malformed schema node"
    ),
)
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
        llm_transform_resolver = _LLMProfileResolver(
            settings.llm_profiles,
            preferred_alias=settings.default_llm_profile,
        )
        self._resolvers: dict[PluginId, OperatorProfileResolver] = {
            PluginId("source", "llm"): _LLMProfileResolver(
                settings.llm_profiles,
                preferred_alias=settings.default_llm_profile,
            ),
            PluginId("transform", "llm"): llm_transform_resolver,
        }
        defaults = dict(settings.bedrock_guardrail_default_profiles)
        for plugin_name in ("aws_bedrock_prompt_shield", "aws_bedrock_content_safety"):
            plugin_profiles = tuple(profile for profile in settings.bedrock_guardrail_profiles if profile.plugin == plugin_name)
            if plugin_profiles:
                self._resolvers[PluginId("transform", plugin_name)] = _BedrockGuardrailProfileResolver(
                    plugin_profiles,
                    default_alias=defaults[plugin_name] if plugin_name in defaults else None,
                )
        if (
            settings.aws_s3_source_profiles
            and type(settings.deployment_aws_region) is str
            and is_well_formed_aws_region(settings.deployment_aws_region)
            and importlib.util.find_spec("boto3") is not None
        ):
            assert settings.deployment_aws_region is not None
            self._resolvers[PluginId("source", "aws_s3")] = _S3SourceProfileResolver(
                settings.aws_s3_source_profiles,
                region=settings.deployment_aws_region,
            )
        if (
            settings.aws_textract_profiles
            and is_supported_textract_region(settings.deployment_aws_region)
            and importlib.util.find_spec("boto3") is not None
        ):
            assert settings.deployment_aws_region is not None
            self._resolvers[PluginId("transform", "aws_textract_document_analysis")] = _TextractProfileResolver(
                settings.aws_textract_profiles,
                region=settings.deployment_aws_region,
            )

    def public_schema(
        self,
        plugin_id: PluginId,
        full_schema: PluginSchemaInfo,
        *,
        available_aliases: tuple[str, ...],
    ) -> PluginSchemaInfo:
        if plugin_id not in self._resolvers:
            return full_schema
        return self._resolvers[plugin_id].public_schema(full_schema, available_aliases)

    def public_summary(
        self,
        plugin_id: PluginId,
        full_summary: PluginSummary,
        full_schema: PluginSchemaInfo,
        *,
        available_aliases: tuple[str, ...],
    ) -> PluginSummary:
        """Project list discovery through the same profile contract as schema discovery."""
        from elspeth.web.catalog.schema_parse import SchemaObject
        from elspeth.web.catalog.schemas import ConfigFieldSummary

        if plugin_id not in self._resolvers:
            return full_summary
        resolver = self._resolvers[plugin_id]
        public_schema = resolver.public_schema(full_schema, available_aliases)
        parsed = SchemaObject.model_validate(public_schema.json_schema)
        required = set(parsed.required)
        config_fields: list[ConfigFieldSummary] = []
        for name, field_schema in parsed.properties.items():
            json_type = field_schema.type or "object"
            if field_schema.any_of and field_schema.type is None:
                json_type = next((branch.type or "object" for branch in field_schema.any_of if branch.type != "null"), "object")
            config_fields.append(
                ConfigFieldSummary(
                    name=name,
                    type=json_type,
                    required=name in required,
                    description=field_schema.description,
                    default=field_schema.default,
                )
            )
        updates: dict[str, object] = {
            "config_fields": config_fields,
            "composer_hints": public_schema.composer_hints,
            "secret_requirements": public_schema.secret_requirements,
        }
        # Exact-type dispatch over the three module-private resolver classes:
        # each arm publishes the guidance for ONE operator-profile contract, so a
        # future subclass must declare its own arm rather than silently inherit.
        if type(resolver) is _S3SourceProfileResolver:
            example_alias = available_aliases[0] if available_aliases else "operator-approved-profile"
            updates["usage_when_to_use"] = (
                "Use in Web Composer when an operator-approved S3 source profile is available and the workflow "
                "needs one bounded CSV, JSON-array, or JSONL object selected by relative object key."
            )
            updates["usage_when_not_to_use"] = (
                "Do not use when no matching operator-approved profile is available, the object is outside the "
                "approved prefix, or the workflow needs to enumerate or stream multiple objects."
            )
            updates["example_use"] = (
                "sources:\n"
                "  s3_input:\n"
                "    plugin: aws_s3\n"
                "    on_success: output\n"
                "    options:\n"
                f"      profile: {example_alias}\n"
                "      key: records/input.csv\n"
                "      format: csv\n"
                "      schema: {mode: observed}\n"
                "      on_validation_failure: discard"
            )
        elif type(resolver) is _TextractProfileResolver:
            example_alias = available_aliases[0] if available_aliases else "operator-approved-profile"
            updates["example_use"] = (
                "transform:\n"
                "  plugin: aws_textract_document_analysis\n"
                "  options:\n"
                f"    profile: {example_alias}\n"
                "    key_field: document_key\n"
                "    feature_types: [TABLES, FORMS]\n"
                "    text_field: textract_text\n"
                "    schema: {mode: observed}"
            )
        return full_summary.model_copy(update=updates)

    def public_assistance(
        self,
        plugin_id: PluginId,
        full_assistance: PluginAssistance,
    ) -> PluginAssistance:
        """Project plugin guidance through the same operator-profile authority."""
        resolver = self._resolvers[plugin_id] if plugin_id in self._resolvers else None
        if type(resolver) is _S3SourceProfileResolver:
            return PluginAssistance(
                plugin_name=full_assistance.plugin_name,
                issue_code=full_assistance.issue_code,
                summary="Read bounded CSV, JSON-array, or JSONL rows through an operator-approved S3 source profile.",
                composer_hints=(
                    "Select an available profile and provide a canonical relative object key.",
                    "Choose the parser, schema, and validation-failure routing for the selected object.",
                    "Use a different approved profile when the object belongs to a different operator-managed location.",
                ),
            )
        if type(resolver) is _TextractProfileResolver:
            return PluginAssistance(
                plugin_name=full_assistance.plugin_name,
                issue_code=full_assistance.issue_code,
                summary="Analyze S3-backed documents asynchronously through an operator-approved Textract document profile.",
                composer_hints=(
                    "Select an available profile; the server supplies the private document storage binding.",
                    "Rows carry relative object keys in key_field; choose feature_types and map at least one output field.",
                    "The bound document location is verified against the deployment before analysis starts.",
                ),
            )
        return full_assistance

    def lower_options(
        self,
        plugin_id: PluginId,
        *,
        alias: str,
        safe_options: dict[str, object],
    ) -> LoweredPluginConfig:
        if plugin_id not in self._resolvers:
            raise ValueError("plugin_has_no_operator_profile")
        return self._resolvers[plugin_id].lower_options(alias, safe_options)

    def profile_availability(
        self,
        plugin_id: PluginId,
        *,
        principal: str,
        inventory: ProfileCredentialInventory,
    ) -> tuple[ProfileAvailability, ...]:
        if plugin_id not in self._resolvers:
            return ()
        return self._resolvers[plugin_id].profile_availability(principal, inventory)

    def check_local_requirements(self, plugin_id: PluginId, alias: str) -> LocalRequirementResult:
        if plugin_id not in self._resolvers:
            return LocalRequirementResult(available=False, reason=ProfileUnavailableReason.LOCAL_REQUIREMENT_MISSING)
        return self._resolvers[plugin_id].check_local_requirements(alias)

    def selected_profile_alias(self, plugin_id: PluginId, *, usable_aliases: tuple[str, ...]) -> str | None:
        """The designated default among ``usable_aliases``, or None if none is."""
        if plugin_id not in self._resolvers:
            return None
        return self._resolvers[plugin_id].selected_alias(usable_aliases)

    def approved_bedrock_guardrail_profile(
        self,
        plugin_id: PluginId,
        *,
        alias: str,
    ) -> BedrockGuardrailProfileSettings:
        """Resolve one authorized opaque alias to its frozen private binding."""

        if plugin_id not in self._policy.authorized:
            raise ValueError("profile_unavailable")
        resolver = self._resolvers[plugin_id] if plugin_id in self._resolvers else None
        if type(resolver) is not _BedrockGuardrailProfileResolver:
            raise ValueError("profile_unavailable")
        return resolver.approved_profile(alias)
