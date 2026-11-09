"""Factual, batch-local prerequisites; existing saved resources remain usable.

A failed call can promise a new component or connection that never exists.
Only an explicit reference to that still-absent resource blocks a later call.
Ordering, shared existing components and semantic similarity are not evidence.
This does not plan topology or reject ordinary incomplete incremental drafts.
"""

from collections.abc import Mapping
from typing import cast

from pydantic import JsonValue

from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.web.composer._producer_resolver import _IMPLICIT_SELF_PUBLISHING_NODE_TYPES, ProducerResolver
from elspeth.web.composer.state import CompositionState

type Resource = tuple[str, str]


def _object(value: object) -> Mapping[str, JsonValue]:
    # Semantic tool arguments have already crossed bounded JSON admission.
    return cast("Mapping[str, JsonValue]", value) if type(value) is dict else {}


def _add(resources: set[Resource], kind: str, value: object) -> None:
    if type(value) is str and value:
        resources.add((kind, value))


@trust_boundary(
    tier=3,
    source="provider-authored JSON node fields before tool-specific schema admission",
    source_param="node",
    suppresses=("R1",),
    invariant="collects only nonempty string resource names; malformed or absent optional fields contribute no resource",
    non_raising=True,
)
def _node_produces(node: Mapping[str, JsonValue]) -> set[Resource]:
    resources: set[Resource] = set()
    _add(resources, "component", node.get("id"))
    _add(resources, "node", node.get("id"))
    _add(resources, "connection", node.get("on_success"))
    for target in _object(node.get("routes")).values():
        if target != "fork":
            _add(resources, "connection", target)
    targets = node.get("fork_to")
    if type(targets) is list:
        for target in targets:
            _add(resources, "connection", target)
    node_type = node.get("node_type")
    if type(node_type) is str and node_type in _IMPLICIT_SELF_PUBLISHING_NODE_TYPES and node.get("on_success") is None:
        _add(resources, "connection", node.get("id"))
    return resources


@trust_boundary(
    tier=3,
    source="bounded provider JSON tool arguments, before tool-specific schema admission",
    source_param="arguments",
    suppresses=("R1",),
    invariant="extracts only explicitly supplied string resources from recognized structural fields; ignores malformed field shapes",
    non_raising=True,
)
def promised_resources(tool: str, arguments: Mapping[str, JsonValue]) -> frozenset[Resource]:
    """Named resources explicitly offered by a call, whether admitted or not."""
    resources: set[Resource] = set()
    if tool == "upsert_node":
        resources.update(_node_produces(arguments))
    elif tool in ("set_source", "set_source_from_blob", "set_source_from_blobs"):
        _add(resources, "component", arguments.get("source_name", "source"))
        _add(resources, "source", arguments.get("source_name", "source"))
        _add(resources, "connection", arguments.get("on_success"))
    elif tool == "set_output":
        _add(resources, "component", arguments.get("sink_name"))
        _add(resources, "output", arguments.get("sink_name"))
    elif tool == "upsert_edge":
        _add(resources, "edge", arguments.get("id"))
    elif tool == "splice_transform":
        resources.update(_node_produces(_object(arguments.get("node"))))
    elif tool == "set_pipeline":
        source = _object(arguments.get("source"))
        if source:
            resources.update(promised_resources("set_source", source))
        for name, source_value in _object(arguments.get("sources")).items():
            resources.update(promised_resources("set_source", {**_object(source_value), "source_name": name}))
        for key, child_tool in (("nodes", "upsert_node"), ("edges", "upsert_edge"), ("outputs", "set_output")):
            children = arguments.get(key)
            if type(children) is list:
                for child in children:
                    resources.update(promised_resources(child_tool, _object(child)))
    return frozenset(resources)


@trust_boundary(
    tier=3,
    source="bounded provider JSON tool arguments, before tool-specific schema admission",
    source_param="arguments",
    suppresses=("R1",),
    invariant="extracts only string prerequisite references from recognized structural fields; ignores malformed field shapes",
    non_raising=True,
)
def referenced_resources(tool: str, arguments: Mapping[str, JsonValue]) -> frozenset[Resource]:
    """Read prerequisites from declared structural fields, never option prose."""
    resources: set[Resource] = set()
    if tool == "upsert_node":
        _add(resources, "connection", arguments.get("input"))
        # Routing destinations can publish connections or target sinks/region
        # closers. Their spelling alone cannot prove a failed sink prerequisite.
    elif tool == "upsert_edge":
        for key in ("from_node", "to_node"):
            _add(resources, "component", arguments.get(key))
    elif tool == "patch_node_options":
        _add(resources, "node", arguments.get("node_id"))
    elif tool in ("wire_secret_ref", "wire_blob_inline_ref"):
        target = arguments.get("target")
        if type(target) is str and target in ("node", "source", "output"):
            _add(resources, target, arguments.get("target_id", "source"))
    elif tool in ("patch_output_options", "remove_output"):
        _add(resources, "output", arguments.get("sink_name"))
    elif tool in ("patch_source_options", "clear_source"):
        _add(resources, "source", arguments.get("source_name", "source"))
    elif tool == "remove_node":
        _add(resources, "node", arguments.get("id"))
    elif tool == "remove_edge":
        _add(resources, "edge", arguments.get("id"))
    elif tool == "splice_transform":
        for key in ("predecessor_id", "successor_id"):
            _add(resources, "component", arguments.get(key))
    elif tool == "set_output":
        if arguments.get("on_write_failure") != "discard":
            _add(resources, "output", arguments.get("on_write_failure"))
    elif tool in ("set_source", "set_source_from_blob", "set_source_from_blobs"):
        if arguments.get("on_validation_failure") != "discard":
            _add(resources, "output", arguments.get("on_validation_failure"))
    elif tool == "set_pipeline":
        source = _object(arguments.get("source"))
        resources.update(referenced_resources("set_source", source))
        for source_value in _object(arguments.get("sources")).values():
            resources.update(referenced_resources("set_source", _object(source_value)))
        for key, child_tool in (("nodes", "upsert_node"), ("edges", "upsert_edge"), ("outputs", "set_output")):
            children = arguments.get(key)
            if type(children) is list:
                for child in children:
                    resources.update(referenced_resources(child_tool, _object(child)))
        # A full replacement may supply its own prerequisite in the same call.
        resources.difference_update(promised_resources(tool, arguments))
    return frozenset(resources)


def unavailable_resources(candidates: frozenset[Resource], state: CompositionState) -> frozenset[Resource]:
    if not candidates:
        return frozenset()
    components = set(state.sources) | {node.id for node in state.nodes} | {output.name for output in state.outputs}
    edges = {edge.id for edge in state.edges}
    resolver = ProducerResolver.build(
        source=None, sources=state.sources, nodes=state.nodes, sink_names=frozenset(output.name for output in state.outputs)
    )
    return frozenset(
        (kind, name)
        for kind, name in candidates
        if (
            (kind == "component" and name not in components)
            or (kind == "node" and not any(node.id == name for node in state.nodes))
            or (kind == "source" and name not in state.sources)
            or (kind == "output" and not any(output.name == name for output in state.outputs))
            or (kind == "edge" and name not in edges)
            or (
                kind == "connection"
                and resolver.find_producer_for(name) is None
                and not resolver.sink_producers(name)
                and not resolver.queue_predecessors(name)
                and name not in resolver.duplicate_connections
            )
        )
    )


def has_unavailable_prerequisite(tool: str, arguments: Mapping[str, JsonValue], refused: set[Resource], state: CompositionState) -> bool:
    return bool(unavailable_resources(referenced_resources(tool, arguments) & refused, state))
