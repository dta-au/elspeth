"""Nominal canonical response for get_plugin_schema, before projection."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import cast

from pydantic import JsonValue

from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.plugin_capabilities import CapabilityDeclaration, ControlRole, PluginCapability, WebConfigAuthority
from elspeth.web.catalog.schemas import PluginKind, PluginSchemaInfo, PluginSecretRequirement
from elspeth.web.composer._schema_response_grammar import (
    JSONSchemaSnapshot,
    KnobSchemaSnapshot,
    parse_json_schema,
    parse_knob_schema,
    readmit_json_schema,
    readmit_knob_schema,
)
from elspeth.web.composer.response_contracts import AdmittedResponse, ResponseContract


@dataclass(frozen=True, slots=True)
class SchemaSecretRequirement:
    field: str
    candidates: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PluginSchemaSnapshot:
    name: str
    plugin_type: PluginKind
    description: str
    json_schema: JSONSchemaSnapshot
    knob_schema: KnobSchemaSnapshot
    composer_hints: tuple[str, ...]
    secret_requirements: tuple[SchemaSecretRequirement, ...]
    web_config_authority: WebConfigAuthority
    policy_capabilities: tuple[CapabilityDeclaration, ...]


def _bad() -> FrameworkBugError:
    return FrameworkBugError("get_plugin_schema producer returned malformed data")


def _text(value: object) -> str:
    if type(value) is not str:
        raise _bad()
    return value


def _tuple(value: object) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise _bad()
    return value


def _texts(value: object) -> tuple[str, ...]:
    return tuple(_text(item) for item in _tuple(value))


def _secret(value: object, *, owned: bool) -> SchemaSecretRequirement:
    if owned:
        if type(value) is not SchemaSecretRequirement:
            raise _bad()
    elif type(value) is PluginSecretRequirement:
        if set(dict(value)) != {"field", "candidates"}:
            raise _bad()
    else:
        raise _bad()
    return SchemaSecretRequirement(_text(value.field), _texts(value.candidates))


def _capability(value: object) -> CapabilityDeclaration:
    if type(value) is not CapabilityDeclaration:
        raise _bad()
    if (
        type(value.capability) is not PluginCapability
        or (value.control_role is not None and type(value.control_role) is not ControlRole)
        or type(value.blocks_positive_detection) is not bool
    ):
        raise _bad()
    try:
        return CapabilityDeclaration(value.capability, value.control_role, value.blocks_positive_detection)
    except ValueError:
        raise _bad() from None


def _snapshot(value: object) -> PluginSchemaSnapshot:
    if type(value) is PluginSchemaInfo:
        if set(dict(value)) != {field.name for field in fields(PluginSchemaSnapshot)}:
            raise _bad()
        json_schema = parse_json_schema(value.json_schema)
        knob_schema = parse_knob_schema(value.knob_schema)
    elif type(value) is PluginSchemaSnapshot:
        if type(value.json_schema) is not JSONSchemaSnapshot or type(value.knob_schema) is not KnobSchemaSnapshot:
            raise _bad()
        json_schema = readmit_json_schema(value.json_schema)
        knob_schema = readmit_knob_schema(value.knob_schema)
    else:
        raise _bad()
    plugin_type = _text(value.plugin_type)
    if plugin_type not in ("source", "transform", "sink") or type(value.web_config_authority) is not WebConfigAuthority:
        raise _bad()
    return PluginSchemaSnapshot(
        _text(value.name),
        cast(PluginKind, plugin_type),
        _text(value.description),
        json_schema,
        knob_schema,
        _texts(value.composer_hints),
        tuple(_secret(item, owned=type(value) is PluginSchemaSnapshot) for item in _tuple(value.secret_requirements)),
        value.web_config_authority,
        tuple(_capability(item) for item in _tuple(value.policy_capabilities)),
    )


def _encode(value: PluginSchemaSnapshot) -> dict[str, JsonValue]:
    return {
        "name": value.name,
        "plugin_type": value.plugin_type,
        "description": value.description,
        "json_schema": value.json_schema.to_wire(),
        "knob_schema": value.knob_schema.to_wire(),
        "composer_hints": list(value.composer_hints),
        "secret_requirements": [{"field": item.field, "candidates": list(item.candidates)} for item in value.secret_requirements],
        "web_config_authority": value.web_config_authority.value,
        "policy_capabilities": [
            {
                "capability": item.capability.value,
                "control_role": item.control_role.value if item.control_role is not None else None,
                "blocks_positive_detection": item.blocks_positive_detection,
            }
            for item in value.policy_capabilities
        ],
    }


@dataclass(frozen=True, slots=True)
class AdmittedPluginSchemaResponse(AdmittedResponse):
    snapshot: PluginSchemaSnapshot
    contract: PluginSchemaResponseContract

    def to_wire(self) -> JsonValue:
        """Encode the admitted value; cache retrieval must call readmit first."""
        return _encode(self.snapshot)

    def readmit(self, contract: ResponseContract) -> AdmittedPluginSchemaResponse:
        if contract is not self.contract:
            raise FrameworkBugError("Cached plugin schema response contract changed")
        if type(self.snapshot) is not PluginSchemaSnapshot:
            raise FrameworkBugError("Cached plugin schema response has the wrong owned snapshot type")
        return self.contract.admit(self.snapshot)


class PluginSchemaResponseContract(ResponseContract):
    def admit(self, value: object) -> AdmittedPluginSchemaResponse:
        return AdmittedPluginSchemaResponse(_snapshot(value), self)


PLUGIN_SCHEMA_RESPONSE_CONTRACT = PluginSchemaResponseContract()
