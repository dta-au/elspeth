"""Precise canonical get_pipeline_state responses, independent of disclosure."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from pydantic import JsonValue, TypeAdapter

from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.freeze import FrozenJsonArray, freeze_fields
from elspeth.web.composer.redaction import SetPipelineArgumentsModel
from elspeth.web.composer.response_contracts import AdmittedResponse, ResponseContract
from elspeth.web.composer.state import EdgeSpec, EdgeType, NodeSpec, NodeType, OutputSpec, SourceSpec
from elspeth.web.composer.tools._common import (
    _FULL_STATE_COMPONENT_ALIASES,
    _serialize_edge,
    _serialize_node,
    _serialize_output,
    _serialize_source,
)


@dataclass(frozen=True, slots=True)
class SourceStateResponse:
    sources: Mapping[str, SourceSpec]

    def __post_init__(self) -> None:
        freeze_fields(self, "sources")


@dataclass(frozen=True, slots=True)
class NodeStateResponse:
    node: NodeSpec


@dataclass(frozen=True, slots=True)
class OutputStateResponse:
    output: OutputSpec


@dataclass(frozen=True, slots=True)
class StateInspection:
    requested_component: str | None
    resolved_component: Literal["full"]
    accepted_full_state_aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StateMetadata:
    name: str | None
    description: str | None


@dataclass(frozen=True, slots=True)
class FullStateResponse:
    sources: Mapping[str, SourceSpec]
    nodes: tuple[NodeSpec, ...]
    outputs: tuple[OutputSpec, ...]
    edges: tuple[EdgeSpec, ...]
    metadata: StateMetadata
    inspection: StateInspection

    def __post_init__(self) -> None:
        freeze_fields(self, "sources", "nodes", "outputs", "edges")


@dataclass(frozen=True, slots=True)
class AuthoringSource:
    source: SourceSpec
    blob_id: str | None


@dataclass(frozen=True, slots=True)
class AuthoringStateResponse:
    nodes: tuple[NodeSpec, ...]
    edges: tuple[EdgeSpec, ...]
    outputs: tuple[OutputSpec, ...]
    metadata: StateMetadata
    source: AuthoringSource | None
    sources: Mapping[str, AuthoringSource] | None

    def __post_init__(self) -> None:
        freeze_fields(self, "nodes", "edges", "outputs", "sources")


type PipelineStateResponse = SourceStateResponse | NodeStateResponse | OutputStateResponse | FullStateResponse | AuthoringStateResponse

_NODE_TYPE: TypeAdapter[NodeType] = TypeAdapter(NodeType)
_EDGE_TYPE: TypeAdapter[EdgeType] = TypeAdapter(EdgeType)
_ERROR = "Invalid get_pipeline_state producer response"


def _require(condition: bool) -> None:
    if not condition:
        raise FrameworkBugError(_ERROR)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict | MappingProxyType) or type(value) not in (dict, MappingProxyType):
        raise FrameworkBugError(_ERROR)
    _require(all(type(key) is str for key in value))
    return value


def _array(value: object) -> tuple[object, ...]:
    if not isinstance(value, list | tuple) or type(value) not in (list, tuple, FrozenJsonArray):
        raise FrameworkBugError(_ERROR)
    return tuple(value)


def _text(value: object) -> str:
    if not isinstance(value, str) or type(value) is not str:
        raise FrameworkBugError(_ERROR)
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _integer(value: object) -> int:
    if not isinstance(value, int) or type(value) is not int:
        raise FrameworkBugError(_ERROR)
    return value


def _number(value: object) -> float:
    if not isinstance(value, int | float) or type(value) not in (int, float):
        raise FrameworkBugError(_ERROR)
    _require(type(value) is int or math.isfinite(value))
    return value


def _json(value: object) -> JsonValue:
    """Close only explicit config leaves and the final encoder's JSON output."""
    if value is None:
        return None
    if type(value) is bool or type(value) is int or type(value) is str:
        return value
    if isinstance(value, float) and type(value) is float:
        _require(math.isfinite(value))
        return value
    if isinstance(value, list | tuple):
        return [_json(item) for item in _array(value)]
    return {key: _json(item) for key, item in _mapping(value).items()}


def _options(value: object) -> dict[str, JsonValue]:
    return {key: _json(item) for key, item in _mapping(value).items()}


def _strings(value: object) -> dict[str, str]:
    return {key: _text(item) for key, item in _mapping(value).items()}


def _frozen_config(value: object) -> None:
    """Recheck deep immutability before accepting an existing owned value."""
    if isinstance(value, MappingProxyType):
        for item in _mapping(value).values():
            _frozen_config(item)
    elif type(value) is tuple or type(value) is FrozenJsonArray:
        for item in _array(value):
            _frozen_config(item)
    else:
        _require(value is None or type(value) in (bool, int, float, str))
        _json(value)


def _source(value: object) -> SourceSpec:
    if type(value) is SourceSpec:
        _require(type(value.options) is MappingProxyType)
        _frozen_config(value.options)
        value = {
            "plugin": value.plugin,
            "on_success": value.on_success,
            "options": value.options,
            "on_validation_failure": value.on_validation_failure,
            "description": value.description,
        }
    wire = _mapping(value)
    _require(set(wire) == {"plugin", "on_success", "options", "on_validation_failure", "description"})
    return SourceSpec(
        _text(wire["plugin"]),
        _text(wire["on_success"]),
        _options(wire["options"]),
        _text(wire["on_validation_failure"]),
        _optional_text(wire["description"]),
    )


def _node(value: object) -> NodeSpec:
    if type(value) is NodeSpec:
        _require(type(value.options) is MappingProxyType)
        _frozen_config(value.options)
        _require(value.routes is None or type(value.routes) is MappingProxyType)
        _require(value.fork_to is None or type(value.fork_to) is tuple)
        _require(value.branches is None or type(value.branches) in (MappingProxyType, tuple))
        _require(value.trigger is None or type(value.trigger) is MappingProxyType)
        # Read every owned field before the legacy serializer can collapse a
        # falsey malformed optional. Cache re-admission never trusts identity.
        value = {
            "id": value.id,
            "node_type": value.node_type,
            "plugin": value.plugin,
            "input": value.input,
            "on_success": value.on_success,
            "on_error": value.on_error,
            "options": value.options,
            "condition": value.condition,
            "routes": value.routes,
            "fork_to": value.fork_to,
            "branches": value.branches,
            "policy": value.policy,
            "merge": value.merge,
            "trigger": value.trigger,
            "output_mode": value.output_mode,
            "expected_output_count": value.expected_output_count,
            "timeout_seconds": value.timeout_seconds,
            "description": value.description,
            "scope_name": value.scope_name,
            "scope_opener": value.scope_opener,
            "scope_policy": value.scope_policy,
        }
    wire = _mapping(value)
    _require(
        set(wire)
        == {
            "id",
            "node_type",
            "plugin",
            "input",
            "on_success",
            "on_error",
            "options",
            "condition",
            "routes",
            "fork_to",
            "branches",
            "policy",
            "merge",
            "trigger",
            "output_mode",
            "expected_output_count",
            "timeout_seconds",
            "description",
            "scope_name",
            "scope_opener",
            "scope_policy",
        }
    )
    branches = wire["branches"]
    parsed_branches = (
        None
        if branches is None
        else (_strings(branches) if isinstance(branches, dict | MappingProxyType) else tuple(_text(item) for item in _array(branches)))
    )
    trigger = None
    if wire["trigger"] is not None:
        raw_trigger = _mapping(wire["trigger"])
        _require(set(raw_trigger) <= {"count", "timeout_seconds", "condition"})
        trigger = {}
        for key, item in raw_trigger.items():
            trigger[key] = (
                None if item is None else _integer(item) if key == "count" else _number(item) if key == "timeout_seconds" else _text(item)
            )
    result = NodeSpec(
        id=_text(wire["id"]),
        node_type=_NODE_TYPE.validate_python(wire["node_type"], strict=True),
        plugin=_optional_text(wire["plugin"]),
        input=_text(wire["input"]),
        on_success=_optional_text(wire["on_success"]),
        on_error=_optional_text(wire["on_error"]),
        options=_options(wire["options"]),
        condition=_optional_text(wire["condition"]),
        routes=None if wire["routes"] is None else _strings(wire["routes"]),
        fork_to=None if wire["fork_to"] is None else tuple(_text(item) for item in _array(wire["fork_to"])),
        branches=parsed_branches,
        policy=_optional_text(wire["policy"]),
        merge=_optional_text(wire["merge"]),
        trigger=trigger,
        output_mode=_optional_text(wire["output_mode"]),
        expected_output_count=None if wire["expected_output_count"] is None else _integer(wire["expected_output_count"]),
        timeout_seconds=None if wire["timeout_seconds"] is None else _number(wire["timeout_seconds"]),
        description=_optional_text(wire["description"]),
        scope_name=_optional_text(wire["scope_name"]),
        scope_opener=_optional_text(wire["scope_opener"]),
        scope_policy=_optional_text(wire["scope_policy"]),
    )
    # SourceSpec/NodeSpec freeze configuration without rewriting it. Only
    # these node fields normalize or collapse in the existing serializer;
    # compare them without thawing the entire options tree a second time.
    _require(result.policy == wire["policy"] and result.merge == wire["merge"])
    _require(_json(result.branches) == _json(wire["branches"]))
    for name in ("routes", "fork_to", "branches", "trigger"):
        _require(wire[name] is None or bool(wire[name]))
    return result


def _output(value: object) -> OutputSpec:
    if type(value) is OutputSpec:
        _require(type(value.options) is MappingProxyType)
        _frozen_config(value.options)
        value = {
            "sink_name": value.name,
            "plugin": value.plugin,
            "options": value.options,
            "on_write_failure": value.on_write_failure,
            "description": value.description,
        }
    wire = _mapping(value)
    _require(set(wire) == {"sink_name", "plugin", "options", "on_write_failure", "description"})
    return OutputSpec(
        _text(wire["sink_name"]),
        _text(wire["plugin"]),
        _options(wire["options"]),
        _text(wire["on_write_failure"]),
        _optional_text(wire["description"]),
    )


def _edge(value: object) -> EdgeSpec:
    if type(value) is EdgeSpec:
        value = {"id": value.id, "from_node": value.from_node, "to_node": value.to_node, "edge_type": value.edge_type, "label": value.label}
    wire = _mapping(value)
    _require(set(wire) == {"id", "from_node", "to_node", "edge_type", "label"})
    return EdgeSpec(
        _text(wire["id"]),
        _text(wire["from_node"]),
        _text(wire["to_node"]),
        _EDGE_TYPE.validate_python(wire["edge_type"], strict=True),
        _optional_text(wire["label"]),
    )


def _metadata(value: object) -> StateMetadata:
    if type(value) is StateMetadata:
        value = {"name": value.name, "description": value.description}
    wire = _mapping(value)
    _require(set(wire) == {"name", "description"})
    return StateMetadata(_optional_text(wire["name"]), _optional_text(wire["description"]))


def _inspection(value: object) -> StateInspection:
    if type(value) is StateInspection:
        _require(type(value.accepted_full_state_aliases) is tuple)
        value = {
            "requested_component": value.requested_component,
            "resolved_component": value.resolved_component,
            "accepted_full_state_aliases": value.accepted_full_state_aliases,
        }
    wire = _mapping(value)
    _require(set(wire) == {"requested_component", "resolved_component", "accepted_full_state_aliases"})
    requested = _optional_text(wire["requested_component"])
    resolved = _text(wire["resolved_component"])
    aliases = tuple(_text(item) for item in _array(wire["accepted_full_state_aliases"]))
    _require(resolved == "full" and aliases == _FULL_STATE_COMPONENT_ALIASES)
    _require(requested is None or requested.strip().lower() in _FULL_STATE_COMPONENT_ALIASES)
    return StateInspection(requested, "full", aliases)


def _sources(value: object) -> Mapping[str, SourceSpec]:
    return MappingProxyType({name: _source(item) for name, item in _mapping(value).items()})


def _owned_sources(value: object) -> Mapping[str, SourceSpec]:
    _require(type(value) is MappingProxyType)
    _require(all(type(item) is SourceSpec for item in _mapping(value).values()))
    return _sources(value)


def _authoring_source(value: object, *, named: bool) -> AuthoringSource:
    if type(value) is AuthoringSource:
        _require(type(value.source) is SourceSpec)
        _require(value.blob_id is None or not named)
        blob_id = _optional_text(value.blob_id)
        _require(blob_id is None or bool(blob_id.strip()))
        return AuthoringSource(_source(value.source), blob_id)
    wire = dict(_mapping(value))
    blob_id = None
    if "blob_id" in wire:
        _require(not named)
        blob_id = _text(wire.pop("blob_id"))
        _require(bool(blob_id.strip()))
    return AuthoringSource(_source(wire), blob_id)


def _authoring_sources(value: object) -> Mapping[str, AuthoringSource]:
    return MappingProxyType({name: _authoring_source(item, named=True) for name, item in _mapping(value).items()})


def _parse(value: object) -> PipelineStateResponse:
    if type(value) is SourceStateResponse:
        return SourceStateResponse(_owned_sources(value.sources))
    if type(value) is NodeStateResponse:
        _require(type(value.node) is NodeSpec)
        return NodeStateResponse(_node(value.node))
    if type(value) is OutputStateResponse:
        _require(type(value.output) is OutputSpec)
        return OutputStateResponse(_output(value.output))
    if type(value) is FullStateResponse:
        _require(type(value.nodes) is tuple and type(value.outputs) is tuple and type(value.edges) is tuple)
        _require(all(type(item) is NodeSpec for item in value.nodes))
        _require(all(type(item) is OutputSpec for item in value.outputs))
        _require(all(type(item) is EdgeSpec for item in value.edges))
        _require(type(value.metadata) is StateMetadata and type(value.inspection) is StateInspection)
        return FullStateResponse(
            _owned_sources(value.sources),
            tuple(_node(item) for item in value.nodes),
            tuple(_output(item) for item in value.outputs),
            tuple(_edge(item) for item in value.edges),
            _metadata(value.metadata),
            _inspection(value.inspection),
        )
    if type(value) is AuthoringStateResponse:
        _require(type(value.nodes) is tuple and type(value.outputs) is tuple and type(value.edges) is tuple)
        _require(all(type(item) is NodeSpec for item in value.nodes))
        _require(all(type(item) is OutputSpec for item in value.outputs))
        _require(all(type(item) is EdgeSpec for item in value.edges))
        _require(type(value.metadata) is StateMetadata)
        _require((value.source is None) != (value.sources is None))
        _require(value.source is None or type(value.source) is AuthoringSource)
        if value.sources is not None:
            _require(type(value.sources) is MappingProxyType)
            _require(all(type(item) is AuthoringSource for item in value.sources.values()))
        result = AuthoringStateResponse(
            tuple(_node(item) for item in value.nodes),
            tuple(_edge(item) for item in value.edges),
            tuple(_output(item) for item in value.outputs),
            _metadata(value.metadata),
            None if value.source is None else _authoring_source(value.source, named=False),
            None if value.sources is None else _authoring_sources(value.sources),
        )
        SetPipelineArgumentsModel.model_validate(_encode(result), strict=True)
        return result
    wire = _mapping(value)
    keys = set(wire)
    if keys == {"sources"}:
        return SourceStateResponse(_sources(wire["sources"]))
    if keys == {"node"}:
        return NodeStateResponse(_node(wire["node"]))
    if keys == {"output"}:
        return OutputStateResponse(_output(wire["output"]))
    if keys == {"sources", "nodes", "outputs", "edges", "metadata", "inspection"}:
        return FullStateResponse(
            _sources(wire["sources"]),
            tuple(_node(item) for item in _array(wire["nodes"])),
            tuple(_output(item) for item in _array(wire["outputs"])),
            tuple(_edge(item) for item in _array(wire["edges"])),
            _metadata(wire["metadata"]),
            _inspection(wire["inspection"]),
        )
    _require(keys in ({"source", "nodes", "edges", "outputs", "metadata"}, {"sources", "nodes", "edges", "outputs", "metadata"}))
    result = AuthoringStateResponse(
        tuple(_node(item) for item in _array(wire["nodes"])),
        tuple(_edge(item) for item in _array(wire["edges"])),
        tuple(_output(item) for item in _array(wire["outputs"])),
        _metadata(wire["metadata"]),
        _authoring_source(wire["source"], named=False) if "source" in wire else None,
        _authoring_sources(wire["sources"]) if "sources" in wire else None,
    )
    SetPipelineArgumentsModel.model_validate(_encode(result), strict=True)
    return result


def parse_pipeline_state_response(value: object) -> PipelineStateResponse:
    try:
        return _parse(value)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise FrameworkBugError(_ERROR) from None


def _authoring_source_wire(value: AuthoringSource) -> JsonValue:
    source = _serialize_source(value.source)
    if value.blob_id is None:
        return _json(source)
    return _json(
        {
            "plugin": source["plugin"],
            "on_success": source["on_success"],
            "blob_id": value.blob_id,
            "options": source["options"],
            "on_validation_failure": source["on_validation_failure"],
            "description": source["description"],
        }
    )


def _encode(value: PipelineStateResponse) -> JsonValue:
    if type(value) is SourceStateResponse:
        return {"sources": {name: _json(_serialize_source(source)) for name, source in value.sources.items()}}
    if type(value) is NodeStateResponse:
        return {"node": _json(_serialize_node(value.node))}
    if type(value) is OutputStateResponse:
        return {"output": _json(_serialize_output(value.output))}
    if type(value) is FullStateResponse:
        return {
            "sources": {name: _json(_serialize_source(source)) for name, source in value.sources.items()},
            "nodes": [_json(_serialize_node(node)) for node in value.nodes],
            "outputs": [_json(_serialize_output(output)) for output in value.outputs],
            "edges": [_json(_serialize_edge(edge)) for edge in value.edges],
            "metadata": {"name": value.metadata.name, "description": value.metadata.description},
            "inspection": {
                "requested_component": value.inspection.requested_component,
                "resolved_component": value.inspection.resolved_component,
                "accepted_full_state_aliases": list(value.inspection.accepted_full_state_aliases),
            },
        }
    if not isinstance(value, AuthoringStateResponse) or type(value) is not AuthoringStateResponse:
        raise FrameworkBugError(_ERROR)
    result: dict[str, JsonValue] = {
        "nodes": [_json(_serialize_node(node)) for node in value.nodes],
        "edges": [_json(_serialize_edge(edge)) for edge in value.edges],
        "outputs": [_json(_serialize_output(output)) for output in value.outputs],
        "metadata": {"name": value.metadata.name, "description": value.metadata.description},
    }
    if value.source is not None:
        result["source"] = _authoring_source_wire(value.source)
    else:
        assert value.sources is not None
        result["sources"] = {name: _authoring_source_wire(source) for name, source in value.sources.items()}
    return result


@dataclass(frozen=True, slots=True)
class AdmittedPipelineStateResponse(AdmittedResponse):
    value: PipelineStateResponse
    contract: PipelineStateResponseContract
    _value_type: type[PipelineStateResponse]

    def to_wire(self) -> JsonValue:
        return _encode(self.value)

    def readmit(self, contract: ResponseContract) -> AdmittedPipelineStateResponse:
        if contract is not self.contract:
            raise FrameworkBugError("Cached get_pipeline_state response contract changed")
        if type(self.value) is not self._value_type:
            raise FrameworkBugError("Cached get_pipeline_state canonical response type changed")
        return self.contract.admit(self.value)


@dataclass(frozen=True, slots=True)
class PipelineStateResponseContract(ResponseContract):
    def admit(self, value: object) -> AdmittedPipelineStateResponse:
        parsed = parse_pipeline_state_response(value)
        return AdmittedPipelineStateResponse(parsed, self, type(parsed))


PIPELINE_STATE_RESPONSE_CONTRACT = PipelineStateResponseContract()
