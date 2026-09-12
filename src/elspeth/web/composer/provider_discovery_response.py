"""Immutable policy-owned state projections and restricted discovery envelopes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, cast

from pydantic import JsonValue

from elspeth.contracts.errors import FrameworkBugError
from elspeth.web.composer.response_contracts import AdmittedResponse, ResponseContract
from elspeth.web.composer.state import COMPOSER_NODE_TYPES, NodeType, Severity, ValidationEntry
from elspeth.web.composer.tools._common import ToolResult

if TYPE_CHECKING:
    from elspeth.web.composer.planner_authoring_aids import PlannerPluginContract
    from elspeth.web.composer.protocol import ToolArgumentError


def _bad() -> FrameworkBugError:
    return FrameworkBugError("Malformed policy-owned provider current-state context")


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


def _nullable(value: object) -> str | None:
    return None if value is None else _text(value)


def _texts(value: object) -> tuple[str, ...]:
    return tuple(_text(item) for item in _items(value))


@dataclass(frozen=True, slots=True)
class ProviderSource:
    name: str
    plugin: str
    option_keys: tuple[str, ...]
    on_success: str
    on_validation_failure: str

    def to_wire(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "plugin": self.plugin,
            "option_keys": list(self.option_keys),
            "on_success": self.on_success,
            "on_validation_failure": self.on_validation_failure,
        }


@dataclass(frozen=True, slots=True)
class ProviderNode:
    id: str
    node_type: NodeType
    plugin: str | None
    option_keys: tuple[str, ...]
    input: str
    on_success: str | None
    on_error: str | None

    def to_wire(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "node_type": self.node_type,
            "plugin": self.plugin,
            "option_keys": list(self.option_keys),
            "input": self.input,
            "on_success": self.on_success,
            "on_error": self.on_error,
        }


@dataclass(frozen=True, slots=True)
class ProviderOutput:
    name: str
    plugin: str
    option_keys: tuple[str, ...]
    on_write_failure: str

    def to_wire(self) -> dict[str, JsonValue]:
        return {"name": self.name, "plugin": self.plugin, "option_keys": list(self.option_keys), "on_write_failure": self.on_write_failure}


@dataclass(frozen=True, slots=True)
class ProviderStateContext:
    schema: Literal["guided.current-state-context.v1"]
    version: int
    sources: tuple[ProviderSource, ...]
    nodes: tuple[ProviderNode, ...]
    outputs: tuple[ProviderOutput, ...]

    def to_wire(self) -> dict[str, JsonValue]:
        return {
            "schema": self.schema,
            "version": self.version,
            "sources": [source.to_wire() for source in self.sources],
            "nodes": [node.to_wire() for node in self.nodes],
            "outputs": [output.to_wire() for output in self.outputs],
        }


def admit_provider_current_state(value: object) -> ProviderStateContext:
    """Parse the entire trusted projection, rejecting missing or corrupt fields."""
    root = _record(value, ("schema", "version", "sources", "nodes", "outputs"))
    if _text(root["schema"]) != "guided.current-state-context.v1" or type(root["version"]) is not int:
        raise _bad()
    sources = []
    for item in _items(root["sources"]):
        source = _record(item, ("name", "plugin", "option_keys", "on_success", "on_validation_failure"))
        sources.append(
            ProviderSource(
                _text(source["name"]),
                _text(source["plugin"]),
                _texts(source["option_keys"]),
                _text(source["on_success"]),
                _text(source["on_validation_failure"]),
            )
        )
    nodes = []
    for item in _items(root["nodes"]):
        node = _record(item, ("id", "node_type", "plugin", "option_keys", "input", "on_success", "on_error"))
        kind = _text(node["node_type"])
        if kind not in COMPOSER_NODE_TYPES:
            raise _bad()
        nodes.append(
            ProviderNode(
                _text(node["id"]),
                cast(NodeType, kind),
                _nullable(node["plugin"]),
                _texts(node["option_keys"]),
                _text(node["input"]),
                _nullable(node["on_success"]),
                _nullable(node["on_error"]),
            )
        )
    outputs = []
    for item in _items(root["outputs"]):
        output = _record(item, ("name", "plugin", "option_keys", "on_write_failure"))
        outputs.append(
            ProviderOutput(_text(output["name"]), _text(output["plugin"]), _texts(output["option_keys"]), _text(output["on_write_failure"]))
        )
    return ProviderStateContext("guided.current-state-context.v1", root["version"], tuple(sources), tuple(nodes), tuple(outputs))


class _UncachedProjection(AdmittedResponse):
    def readmit(self, contract: ResponseContract) -> AdmittedResponse:
        raise FrameworkBugError("Policy-owned provider projections cannot be cached as producer responses")


@dataclass(frozen=True, slots=True)
class _ProjectedPluginContractResponse(_UncachedProjection):
    contract: PlannerPluginContract

    def to_wire(self) -> dict[str, JsonValue]:
        # The owned bounded projection has already admitted its schema leaves.
        # This cast narrows serialization output, never an admitted input root.
        return cast(dict[str, JsonValue], self.contract.to_dict())


def projected_plugin_contract_response(contract: PlannerPluginContract) -> AdmittedResponse:
    return _ProjectedPluginContractResponse(contract)


@dataclass(frozen=True, slots=True)
class _ArgumentErrorResponse(_UncachedProjection):
    component: str
    error_code: str

    def to_wire(self) -> dict[str, JsonValue]:
        return {
            "argument_error": {
                "component": self.component,
                "severity": "high",
                "error_code": self.error_code,
                "error_class": "ToolArgumentError",
            }
        }


def argument_error_response(error: ToolArgumentError) -> AdmittedResponse:
    return _ArgumentErrorResponse(error.argument, error.code or "argument_error")


@dataclass(frozen=True, slots=True)
class _StateResponse(_UncachedProjection):
    context: ProviderStateContext

    def to_wire(self) -> dict[str, JsonValue]:
        return self.context.to_wire()


@dataclass(frozen=True, slots=True)
class _SourcesResponse(_UncachedProjection):
    sources: tuple[ProviderSource, ...]

    def to_wire(self) -> dict[str, JsonValue]:
        return {"sources": [source.to_wire() for source in self.sources]}


@dataclass(frozen=True, slots=True)
class _NodeResponse(_UncachedProjection):
    node: ProviderNode

    def to_wire(self) -> dict[str, JsonValue]:
        return {"node": self.node.to_wire()}


@dataclass(frozen=True, slots=True)
class _OutputResponse(_UncachedProjection):
    output: ProviderOutput

    def to_wire(self) -> dict[str, JsonValue]:
        return {"output": self.output.to_wire()}


def provider_state_response(context: ProviderStateContext) -> AdmittedResponse:
    return _StateResponse(context)


def provider_sources_response(context: ProviderStateContext) -> AdmittedResponse:
    return _SourcesResponse(context.sources)


def provider_node_response(node: ProviderNode) -> AdmittedResponse:
    return _NodeResponse(node)


def provider_output_response(output: ProviderOutput) -> AdmittedResponse:
    return _OutputResponse(output)


@dataclass(frozen=True, slots=True)
class ClosedProviderValidationEntry:
    severity: Severity
    error_code: str

    def to_wire(self) -> dict[str, JsonValue]:
        return {"component": "pipeline", "severity": self.severity, "error_code": self.error_code}


@dataclass(frozen=True, slots=True)
class ClosedProviderValidation:
    is_valid: bool
    errors: tuple[ClosedProviderValidationEntry, ...]
    warnings: tuple[ClosedProviderValidationEntry, ...]
    suggestions: tuple[ClosedProviderValidationEntry, ...]

    def to_wire(self) -> dict[str, JsonValue]:
        return {
            "is_valid": self.is_valid,
            "errors": [entry.to_wire() for entry in self.errors],
            "warnings": [entry.to_wire() for entry in self.warnings],
            "suggestions": [entry.to_wire() for entry in self.suggestions],
            "semantic_contracts": [],
            "graph_repair_suggestions": [],
        }


@dataclass(frozen=True, slots=True)
class ClosedProviderDiscoveryEnvelope:
    success: bool
    validation: ClosedProviderValidation
    affected_nodes: tuple[str, ...]
    version: int
    data: AdmittedResponse | None

    def to_wire(self) -> dict[str, JsonValue]:
        wire: dict[str, JsonValue] = {
            "success": self.success,
            "validation": self.validation.to_wire(),
            "affected_nodes": list(self.affected_nodes),
            "version": self.version,
        }
        if self.data is not None:
            wire["data"] = self.data.to_wire()
        return wire


def _validation_entry(entry: ValidationEntry, fallback: str) -> ClosedProviderValidationEntry:
    return ClosedProviderValidationEntry(entry.severity, entry.error_code or fallback)


def closed_provider_envelope(
    result: ToolResult, *, success: bool | None = None, data: AdmittedResponse | None = None
) -> ClosedProviderDiscoveryEnvelope:
    """Construct the final envelope without reading or traversing result.data."""
    validation = result.validation
    return ClosedProviderDiscoveryEnvelope(
        result.success if success is None else success,
        ClosedProviderValidation(
            validation.is_valid,
            tuple(_validation_entry(entry, "validation_error") for entry in validation.errors),
            tuple(_validation_entry(entry, "validation_warning") for entry in validation.warnings),
            tuple(_validation_entry(entry, "validation_suggestion") for entry in validation.suggestions),
        ),
        tuple(result.affected_nodes),
        result.updated_state.version,
        data,
    )


@dataclass(frozen=True, slots=True)
class _ProjectionFailure(_UncachedProjection):
    kind: Literal["surface", "schema_unavailable", "schema_budget"]

    def to_wire(self) -> dict[str, JsonValue]:
        if self.kind == "surface":
            return {
                "error": "The requested component is unavailable on this planner disclosure surface.",
                "error_code": "surface_projection_unavailable",
            }
        if self.kind == "schema_unavailable":
            message = "The selected plugin schema cannot be represented in the bounded planner projection. Use get_plugin_assistance."
            code = "schema_projection_unavailable"
        else:
            message = "The selected plugin contracts exceed the aggregate planner schema budget. Use get_plugin_assistance."
            code = "schema_contract_budget_exceeded"
        return {"error": message, "error_code": code, "next_tool": "get_plugin_assistance"}


def surface_projection_failure() -> AdmittedResponse:
    return _ProjectionFailure("surface")


def schema_projection_failure() -> AdmittedResponse:
    return _ProjectionFailure("schema_unavailable")


def schema_budget_failure() -> AdmittedResponse:
    return _ProjectionFailure("schema_budget")
