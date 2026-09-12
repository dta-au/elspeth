"""One immutable response contract shared by the three plugin inventories."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from pydantic import JsonValue

from elspeth.contracts.enums import AuditCharacteristic
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.plugin_capabilities import CapabilityDeclaration, ControlRole, PluginCapability, WebConfigAuthority
from elspeth.web.catalog.schemas import ConfigFieldSummary, PluginSecretRequirement, PluginSummary
from elspeth.web.composer._response_json import FrozenResponseJSON, encode_response_json, parse_frozen_response_json, parse_response_json
from elspeth.web.composer.response_contracts import SelectedResponseContract
from elspeth.web.plugin_policy.models import PluginUnavailableReason


def _bad() -> FrameworkBugError:
    return FrameworkBugError("Plugin inventory producer returned malformed data")


def _record(value: object, keys: tuple[str, ...]) -> Mapping[str, object]:
    if type(value) is not dict and type(value) is not MappingProxyType:
        raise _bad()
    if set(value) != set(keys) or any(type(key) is not str for key in value):
        raise _bad()
    return cast(Mapping[str, object], value)


def _items(value: object) -> tuple[object, ...]:
    if type(value) is not list and type(value) is not tuple:
        raise _bad()
    return tuple(value)


def _text(value: object) -> str:
    if type(value) is not str:
        raise _bad()
    return value


def _nullable_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _texts(value: object) -> tuple[str, ...]:
    return tuple(_text(item) for item in _items(value))


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise _bad()
    return value


@dataclass(frozen=True, slots=True)
class InventoryConfigField:
    name: str
    type: str
    required: bool
    description: str | None
    default: FrozenResponseJSON


@dataclass(frozen=True, slots=True)
class InventorySecretRequirement:
    field: str
    candidates: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InventoryPlugin:
    name: str
    description: str
    plugin_type: str
    config_fields: tuple[InventoryConfigField, ...]
    usage_when_to_use: str | None
    usage_when_not_to_use: str | None
    example_use: str | None
    capability_tags: tuple[str, ...]
    web_config_authority: WebConfigAuthority
    policy_capabilities: tuple[CapabilityDeclaration, ...]
    audit_characteristics: tuple[AuditCharacteristic, ...]
    composer_hints: tuple[str, ...]
    secret_requirements: tuple[InventorySecretRequirement, ...]


@dataclass(frozen=True, slots=True)
class InventoryProhibitedPlugin:
    name: str
    reason: str
    explanation: str


@dataclass(frozen=True, slots=True)
class PluginInventoryResponse:
    available: tuple[InventoryPlugin, ...]
    prohibited: tuple[InventoryProhibitedPlugin, ...]


def _config_field(value: object) -> InventoryConfigField:
    parse_default = parse_response_json
    if type(value) is ConfigFieldSummary:
        value = dict(value)
    elif type(value) is InventoryConfigField:
        parse_default = parse_frozen_response_json
        value = {
            "name": value.name,
            "type": value.type,
            "required": value.required,
            "description": value.description,
            "default": value.default,
        }
    row = _record(value, ("name", "type", "required", "description", "default"))
    return InventoryConfigField(
        _text(row["name"]),
        _text(row["type"]),
        _boolean(row["required"]),
        _nullable_text(row["description"]),
        parse_default(row["default"]),
    )


def _secret(value: object) -> InventorySecretRequirement:
    if type(value) is PluginSecretRequirement:
        value = dict(value)
    elif type(value) is InventorySecretRequirement:
        if type(value.candidates) is not tuple:
            raise _bad()
        value = {"field": value.field, "candidates": value.candidates}
    row = _record(value, ("field", "candidates"))
    return InventorySecretRequirement(_text(row["field"]), _texts(row["candidates"]))


def _capability(value: object) -> CapabilityDeclaration:
    if type(value) is CapabilityDeclaration:
        if type(value.capability) is not PluginCapability or (
            value.control_role is not None and type(value.control_role) is not ControlRole
        ):
            raise _bad()
        value = {
            "capability": value.capability,
            "control_role": value.control_role,
            "blocks_positive_detection": value.blocks_positive_detection,
        }
    row = _record(value, ("capability", "control_role", "blocks_positive_detection"))
    capability, role = row["capability"], row["control_role"]
    if type(capability) is str:
        capability = PluginCapability(capability)
    if type(role) is str:
        role = ControlRole(role)
    if type(capability) is not PluginCapability or (role is not None and type(role) is not ControlRole):
        raise _bad()
    return CapabilityDeclaration(capability, role, _boolean(row["blocks_positive_detection"]))


_PLUGIN_FIELDS = (
    "name",
    "description",
    "plugin_type",
    "config_fields",
    "usage_when_to_use",
    "usage_when_not_to_use",
    "example_use",
    "capability_tags",
    "web_config_authority",
    "policy_capabilities",
    "audit_characteristics",
    "composer_hints",
    "secret_requirements",
)


def _plugin(value: object) -> InventoryPlugin:
    if type(value) is PluginSummary:
        value = dict(value)
    elif type(value) is InventoryPlugin:
        if any(
            type(items) is not tuple
            for items in (
                value.config_fields,
                value.capability_tags,
                value.policy_capabilities,
                value.audit_characteristics,
                value.composer_hints,
                value.secret_requirements,
            )
        ):
            raise _bad()
        if (
            any(type(item) is not InventoryConfigField for item in value.config_fields)
            or any(type(item) is not InventorySecretRequirement for item in value.secret_requirements)
            or any(type(item) is not CapabilityDeclaration for item in value.policy_capabilities)
            or any(type(item) is not AuditCharacteristic for item in value.audit_characteristics)
            or type(value.web_config_authority) is not WebConfigAuthority
        ):
            raise _bad()
        value = {
            "name": value.name,
            "description": value.description,
            "plugin_type": value.plugin_type,
            "config_fields": value.config_fields,
            "usage_when_to_use": value.usage_when_to_use,
            "usage_when_not_to_use": value.usage_when_not_to_use,
            "example_use": value.example_use,
            "capability_tags": value.capability_tags,
            "web_config_authority": value.web_config_authority,
            "policy_capabilities": value.policy_capabilities,
            "audit_characteristics": value.audit_characteristics,
            "composer_hints": value.composer_hints,
            "secret_requirements": value.secret_requirements,
        }
    row = _record(value, _PLUGIN_FIELDS)
    plugin_type = _text(row["plugin_type"])
    if plugin_type not in ("source", "transform", "sink"):
        raise _bad()
    authority = row["web_config_authority"]
    if type(authority) is str:
        authority = WebConfigAuthority(authority)
    if type(authority) is not WebConfigAuthority:
        raise _bad()
    characteristics: list[AuditCharacteristic] = []
    for item in _items(row["audit_characteristics"]):
        if type(item) is str:
            item = AuditCharacteristic(item)
        if type(item) is not AuditCharacteristic:
            raise _bad()
        characteristics.append(item)
    return InventoryPlugin(
        _text(row["name"]),
        _text(row["description"]),
        plugin_type,
        tuple(_config_field(item) for item in _items(row["config_fields"])),
        _nullable_text(row["usage_when_to_use"]),
        _nullable_text(row["usage_when_not_to_use"]),
        _nullable_text(row["example_use"]),
        _texts(row["capability_tags"]),
        authority,
        tuple(_capability(item) for item in _items(row["policy_capabilities"])),
        tuple(characteristics),
        _texts(row["composer_hints"]),
        tuple(_secret(item) for item in _items(row["secret_requirements"])),
    )


def _prohibited(value: object) -> InventoryProhibitedPlugin:
    if type(value) is InventoryProhibitedPlugin:
        value = {"name": value.name, "reason": value.reason, "explanation": value.explanation}
    row = _record(value, ("name", "reason", "explanation"))
    reason = _text(row["reason"])
    if reason != PluginUnavailableReason.WEB_SURFACE_PROHIBITED.value:
        raise _bad()
    return InventoryProhibitedPlugin(_text(row["name"]), reason, _text(row["explanation"]))


def parse_plugin_inventory(value: object) -> PluginInventoryResponse:
    try:
        if type(value) is PluginInventoryResponse:
            if type(value.available) is not tuple or type(value.prohibited) is not tuple:
                raise _bad()
            if any(type(item) is not InventoryPlugin for item in value.available) or any(
                type(item) is not InventoryProhibitedPlugin for item in value.prohibited
            ):
                raise _bad()
            value = {"available": value.available, "prohibited": value.prohibited}
        row = _record(value, ("available", "prohibited"))
        return PluginInventoryResponse(
            tuple(_plugin(item) for item in _items(row["available"])), tuple(_prohibited(item) for item in _items(row["prohibited"]))
        )
    except (TypeError, ValueError):
        raise _bad() from None


def _plugin_wire(value: InventoryPlugin) -> dict[str, JsonValue]:
    return {
        "name": value.name,
        "description": value.description,
        "plugin_type": value.plugin_type,
        "config_fields": [
            {
                "name": item.name,
                "type": item.type,
                "required": item.required,
                "description": item.description,
                "default": encode_response_json(item.default),
            }
            for item in value.config_fields
        ],
        "usage_when_to_use": value.usage_when_to_use,
        "usage_when_not_to_use": value.usage_when_not_to_use,
        "example_use": value.example_use,
        "capability_tags": list(value.capability_tags),
        "web_config_authority": value.web_config_authority.value,
        "policy_capabilities": [
            {
                "capability": item.capability.value,
                "control_role": item.control_role.value if item.control_role is not None else None,
                "blocks_positive_detection": item.blocks_positive_detection,
            }
            for item in value.policy_capabilities
        ],
        "audit_characteristics": [item.value for item in value.audit_characteristics],
        "composer_hints": list(value.composer_hints),
        "secret_requirements": [{"field": item.field, "candidates": list(item.candidates)} for item in value.secret_requirements],
    }


def encode_plugin_inventory(value: PluginInventoryResponse) -> JsonValue:
    return {
        "available": [_plugin_wire(item) for item in value.available],
        "prohibited": [{"name": item.name, "reason": item.reason, "explanation": item.explanation} for item in value.prohibited],
    }


PLUGIN_INVENTORY_RESPONSE_CONTRACT = SelectedResponseContract(parse_plugin_inventory, encode_plugin_inventory)
