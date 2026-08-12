"""Pure authority, redaction, and projection helpers for guided planning.

This module has no persistence or provider authority.  It snapshots the exact
reviewed facts used by :class:`PipelineProposal`, builds the deliberately less
capable model context, and projects a private canonical pipeline into the
closed ``PROPOSE_PIPELINE`` wire contract.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, NotRequired, TypedDict, cast
from uuid import UUID, uuid4

import structlog
from pydantic import JsonValue

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw, freeze_fields
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.trust_boundary import observation_boundary
from elspeth.web.composer._producer_resolver import ProducerEntry, ProducerResolver
from elspeth.web.composer.guided.connection_consumers import canonical_connection_consumers
from elspeth.web.composer.guided.deferred_intents import DeferredIntentClaimError, evaluate_deferred_intent_coverage
from elspeth.web.composer.guided.protocol import (
    PROPOSAL_RATIONALE_TEMPLATE,
    PROPOSAL_SUMMARY_TEMPLATE,
    ProposePipelinePayload,
    TurnType,
    node_options_summary,
    proposal_component_label,
    proposal_structural_label,
    public_node_option_keys,
    validate_payload,
    validate_proposal_catalog_refs,
)
from elspeth.web.composer.guided.resolved import SinkOutputResolved
from elspeth.web.composer.guided.stage_subjects import (
    ComponentCountConstraint,
    EdgeRouteConstraint,
    FailureRouteConstraint,
    OptionValueConstraint,
    StatedGateRoutingConstraint,
    StatedPredicateConstraint,
    SubjectPresenceConstraint,
)
from elspeth.web.composer.guided.state_machine import ComponentTarget, DeferredStageIntent, GuidedSession
from elspeth.web.composer.guided_blob_refs import (
    reviewed_schema_declared_field_names,
    reviewed_schema_mode,
    reviewed_source_is_blob_bound,
)
from elspeth.web.composer.pipeline_proposal import PipelineProposal
from elspeth.web.composer.recipes import ReviewedOutputProjectionConflict, reviewed_output_projection_conflict
from elspeth.web.composer.state import CompositionState, NodeSpec
from elspeth.web.composer.tools.schema_contract import canonical_set_pipeline_schema

slog = structlog.get_logger()


class GuidedBoundSource(TypedDict):
    """One reviewed source restored into a planner-authored topology."""

    plugin: str
    options: dict[str, JsonValue]
    on_success: str
    on_validation_failure: str


class GuidedBoundOutput(TypedDict):
    """One reviewed output restored into a planner-authored topology."""

    sink_name: str
    plugin: str
    options: dict[str, JsonValue]
    on_write_failure: str


class GuidedBoundPipeline(TypedDict):
    """Validated set-pipeline shape after guided authority rebinding."""

    sources: dict[str, GuidedBoundSource]
    nodes: list[dict[str, JsonValue]]
    edges: list[dict[str, JsonValue]]
    outputs: list[GuidedBoundOutput]
    metadata: NotRequired[dict[str, JsonValue] | None]


class _ProjectionNodeKindSummary(TypedDict):
    """Redaction-safe node shape emitted with projection failures."""

    stable_id: object
    node_type: object
    plugin: object
    behavior: object
    branch_aliases: object


_ProjectionEdgeFlowSummary = TypedDict(
    "_ProjectionEdgeFlowSummary",
    {
        "from": object,
        "to": object,
        "flow": object,
        "branch": object,
    },
)


class _ProjectionKindSummary(TypedDict):
    """Closed structural diagnostics for a rejected wire projection."""

    node_kinds: list[_ProjectionNodeKindSummary]
    edge_flows: list[_ProjectionEdgeFlowSummary]


@dataclass(frozen=True, slots=True)
class GuidedEdgeRoutingAuthority:
    """Private scalar slot that owns one public connection."""

    field: Literal["on_success", "on_error", "on_validation_failure", "on_write_failure", "routes", "fork_to"]
    route_key: str | None
    fork_index: int | None
    before_destination: str


@dataclass(frozen=True, slots=True)
class GuidedCorrectionTarget:
    """One closed public selection plus its authoritative private owner."""

    requested: ComponentTarget
    owner_kind: Literal["source", "node", "output"]
    owner_key: str
    authority_key: str | None
    public_target: Mapping[str, Any]
    before_fingerprint: str
    edge_routing: GuidedEdgeRoutingAuthority | None = None
    edge_preserved_fingerprint: str | None = None
    edge_sibling_fingerprints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (self.requested.kind == "edge") != (self.authority_key is None):
            raise ValueError("edge correction targets must not claim a private positional edge identity")
        if self.requested.kind != "edge" and (
            self.edge_routing is not None or self.edge_preserved_fingerprint is not None or self.edge_sibling_fingerprints
        ):
            raise ValueError("non-edge correction targets must not claim edge routing authority")
        freeze_fields(self, "public_target")

    def planner_context(self) -> dict[str, object]:
        return {
            "kind": self.requested.kind,
            "stable_id": self.requested.stable_id,
            "owner_kind": self.owner_kind,
            "owner_key": self.owner_key,
            "target": deep_thaw(self.public_target),
        }


@dataclass(frozen=True, slots=True)
class GuidedRevisionAuthority:
    """Closed authority for an unscoped prose revision of a live proposal.

    ``amend`` is deliberately the conservative default at the HTTP boundary:
    the active proposal is the predecessor and its existing node semantics stay
    server-owned.  ``replace`` is an explicit destructive choice and permits a
    fresh topology while the reviewed source/output authority remains bound by
    :func:`bind_guided_reviewed_components`.
    """

    mode: Literal["amend", "replace"]
    predecessor: CompositionState

    def __post_init__(self) -> None:
        if self.mode not in {"amend", "replace"}:
            raise ValueError("guided revision mode must be amend or replace")
        if type(self.predecessor) is not CompositionState:
            raise TypeError("guided revision predecessor must be an exact CompositionState")

    def planner_context(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "existing_node_policy": (
                "preserve_existing_nodes_allow_insertion_rewiring" if self.mode == "amend" else "explicit_replacement_allowed"
            ),
        }


@dataclass(frozen=True, slots=True)
class GuidedRevisionBindingResult:
    """Server-bound candidate plus one closed repair disposition."""

    pipeline: GuidedBoundPipeline
    rejection_code: Literal["guided_amend_contract_violation"] | None


def guided_revision_execution_hash(state: CompositionState) -> str:
    """Hash revision-responsive pipeline semantics only.

    ``edges`` are a UI projection; runtime routing is owned by source/node
    connection fields.  Metadata is descriptive and cannot satisfy an
    operator request to revise the processing topology.  Keep the general
    composition content hash unchanged and use this narrower equality only
    for prose-revision convergence.
    """

    if type(state) is not CompositionState:
        raise TypeError("state must be an exact CompositionState")
    payload = state.to_dict()
    return stable_hash(
        {
            "sources": payload["sources"],
            "nodes": payload["nodes"],
            "outputs": payload["outputs"],
        }
    )


def _wire_target_fingerprint(
    payload: Mapping[str, Any],
    *,
    collection: Literal["sources", "nodes", "connections", "outputs"],
    index: int,
    authority: CompositionState,
) -> str | None:
    """Fingerprint selected public semantics independent of regenerated IDs."""

    raw_collection = payload.get(collection)
    if type(raw_collection) is not list or index >= len(raw_collection) or type(raw_collection[index]) is not dict:
        return None
    component = cast(dict[str, Any], deep_thaw(raw_collection[index]))
    stable_id = component.pop("stable_id", None)

    authority_keys = {
        "source": tuple(authority.sources),
        "node": tuple(node.id for node in authority.nodes),
        "output": tuple(output.name for output in authority.outputs),
    }
    identities: dict[tuple[str, str], str] = {}
    for component_kind, collection_name in (("source", "sources"), ("node", "nodes"), ("output", "outputs")):
        values = payload.get(collection_name)
        keys = authority_keys[component_kind]
        if type(values) is not list or len(values) != len(keys):
            raise AuditIntegrityError("guided correction wire projection lost identity collections")
        for position, value in enumerate(values):
            if type(value) is not dict or type(value.get("stable_id")) is not str:
                raise AuditIntegrityError("guided correction wire projection has malformed stable identities")
            identities[(component_kind, value["stable_id"])] = keys[position]

    def normalize_endpoint(value: object) -> object:
        # An absent endpoint must fail the same way a malformed one does. Both
        # ``from_endpoint`` and ``to_endpoint`` are required on every wire
        # connection (``_ProposalEndpoint`` is a non-``NotRequired`` field and
        # ``validate_current_turn`` enforces the exact connection key set), so a
        # non-mapping here is an integrity violation, not an optional field.
        # Returning it unchanged would let an omitted key bypass the identity
        # binding check below — the very check that ties an LLM-authored
        # endpoint back to reviewed authority — while a merely wrong endpoint
        # raises.
        if type(value) is not dict:
            raise AuditIntegrityError("guided correction wire edge has a missing or malformed endpoint")
        kind = value.get("kind")
        endpoint_id = value.get("stable_id")
        if kind == "discard":
            return {"kind": "discard"}
        if type(kind) is not str or type(endpoint_id) is not str or (kind, endpoint_id) not in identities:
            raise AuditIntegrityError("guided correction wire edge has an unbound endpoint")
        return {"kind": kind, "key": identities[(kind, endpoint_id)]}

    if collection == "connections":
        from_ep = component.get("from_endpoint")
        to_ep = component.get("to_endpoint")
        if from_ep is None:
            raise AuditIntegrityError("guided correction wire connection is missing from_endpoint")
        if to_ep is None:
            raise AuditIntegrityError("guided correction wire connection is missing to_endpoint")
        component["from_endpoint"] = normalize_endpoint(from_ep)
        component["to_endpoint"] = normalize_endpoint(to_ep)
    else:
        connections = payload.get("connections")
        if type(connections) is not list:
            raise AuditIntegrityError("guided correction wire projection lost connections")
        incident = []
        for connection in connections:
            if type(connection) is not dict:
                raise AuditIntegrityError("guided correction wire projection has a malformed connection")
            origin = connection.get("from_endpoint")
            destination = connection.get("to_endpoint")
            is_origin = type(origin) is dict and origin.get("stable_id") == stable_id
            is_destination = type(destination) is dict and destination.get("stable_id") == stable_id
            if not is_origin and not is_destination:
                continue
            normalized = cast(dict[str, Any], deep_thaw(connection))
            normalized.pop("stable_id", None)
            normalized["from_endpoint"] = normalize_endpoint(normalized.get("from_endpoint"))
            normalized["to_endpoint"] = normalize_endpoint(normalized.get("to_endpoint"))
            incident.append(normalized)
        component["incident_connections"] = incident
    return stable_hash(component)


def _edge_owner_payload(
    state: CompositionState,
    *,
    owner_kind: Literal["source", "node", "output"],
    owner_key: str,
) -> MutableMapping[str, Any]:
    payload = cast(dict[str, Any], deep_thaw(state.to_dict()))
    if owner_kind == "source":
        sources = payload.get("sources")
        if type(sources) is not dict or type(sources.get(owner_key)) is not dict:
            raise AuditIntegrityError("guided edge routing source owner is malformed")
        return cast(dict[str, Any], sources[owner_key])
    if owner_kind == "output":
        outputs = payload.get("outputs")
        if type(outputs) is not list:
            raise AuditIntegrityError("guided edge routing output owner is malformed")
        matches = [item for item in outputs if type(item) is dict and item.get("name") == owner_key]
        if len(matches) != 1:
            raise AuditIntegrityError("guided edge routing output owner does not resolve exactly once")
        return cast(dict[str, Any], matches[0])
    nodes = payload.get("nodes")
    if type(nodes) is not list:
        raise AuditIntegrityError("guided edge routing node owner is malformed")
    matches = [item for item in nodes if type(item) is dict and item.get("id") == owner_key]
    if len(matches) != 1:
        raise AuditIntegrityError("guided edge routing node owner does not resolve exactly once")
    return cast(dict[str, Any], matches[0])


def _edge_route_key_from_public_alias(
    wire_payload: Mapping[str, Any],
    *,
    origin_stable_id: str,
    route_alias: object,
) -> str:
    nodes = wire_payload.get("nodes")
    if type(nodes) is not list:
        raise AuditIntegrityError("guided edge routing lost public node authority")
    matches = [item for item in nodes if type(item) is dict and item.get("stable_id") == origin_stable_id]
    if len(matches) != 1 or type(matches[0].get("behavior")) is not dict:
        raise AuditIntegrityError("guided edge routing lost public gate behavior")
    routes = matches[0]["behavior"].get("routes")
    if type(routes) is not list:
        raise AuditIntegrityError("guided edge routing lost public gate routes")
    route_matches = [item for item in routes if type(item) is dict and item.get("alias") == route_alias]
    if len(route_matches) != 1 or type(route_matches[0].get("key")) is not str:
        raise AuditIntegrityError("guided edge routing route alias does not resolve exactly once")
    return cast(str, route_matches[0]["key"])


def _edge_fork_index_from_public_alias(
    wire_payload: Mapping[str, Any],
    *,
    origin_stable_id: str,
    branch_alias: object,
) -> int:
    nodes = wire_payload.get("nodes")
    if type(nodes) is not list:
        raise AuditIntegrityError("guided edge routing lost public node authority")
    matches = [item for item in nodes if type(item) is dict and item.get("stable_id") == origin_stable_id]
    if len(matches) != 1 or type(matches[0].get("behavior")) is not dict:
        raise AuditIntegrityError("guided edge routing lost public gate behavior")
    branches = matches[0]["behavior"].get("fork_branches")
    if type(branches) is not list:
        raise AuditIntegrityError("guided edge routing lost public fork branches")
    positions = [index for index, item in enumerate(branches) if type(item) is dict and item.get("branch") == branch_alias]
    if len(positions) != 1:
        raise AuditIntegrityError("guided edge routing fork alias does not resolve exactly once")
    return positions[0]


def _resolve_edge_routing_authority(
    *,
    wire_payload: Mapping[str, Any],
    public_target: Mapping[str, Any],
    predecessor: CompositionState,
    owner_kind: Literal["source", "node", "output"],
    owner_key: str,
) -> GuidedEdgeRoutingAuthority | None:
    """Bind a public connection to its one writable private routing scalar."""

    flow = public_target.get("flow")
    origin = public_target.get("from_endpoint")
    if type(flow) is not dict or type(origin) is not dict:
        raise AuditIntegrityError("guided edge routing target has malformed public semantics")
    flow_kind = flow.get("kind")
    if type(flow_kind) is not str:
        flow_kind = flow.get("role")
    if type(flow_kind) is not str:
        raise AuditIntegrityError("guided edge routing target has no public flow kind")
    owner = _edge_owner_payload(predecessor, owner_kind=owner_kind, owner_key=owner_key)
    field: Literal["on_success", "on_error", "on_validation_failure", "on_write_failure", "routes", "fork_to"]
    route_key: str | None = None
    fork_index: int | None = None
    if owner_kind == "source":
        if flow_kind in {"source_success", "on_success"}:
            field = "on_success"
        elif flow_kind == "source_validation_failure":
            field = "on_validation_failure"
        else:
            return None
    elif owner_kind == "output":
        if flow_kind != "output_write_failure":
            return None
        field = "on_write_failure"
    elif flow_kind in {"node_success", "coalesce_success", "row_union_success", "on_success"}:
        field = "on_success"
    elif flow_kind in {"node_error", "on_error"}:
        field = "on_error"
    elif flow_kind in {"gate_route", "route"}:
        origin_stable_id = origin.get("stable_id")
        if type(origin_stable_id) is not str:
            raise AuditIntegrityError("guided edge routing gate origin has no stable identity")
        field = "routes"
        route_key = _edge_route_key_from_public_alias(
            wire_payload,
            origin_stable_id=origin_stable_id,
            route_alias=flow.get("route"),
        )
    elif flow_kind == "gate_fork":
        origin_stable_id = origin.get("stable_id")
        if type(origin_stable_id) is not str:
            raise AuditIntegrityError("guided edge routing fork origin has no stable identity")
        field = "fork_to"
        fork_index = _edge_fork_index_from_public_alias(
            wire_payload,
            origin_stable_id=origin_stable_id,
            branch_alias=flow.get("branch"),
        )
    else:
        # Queue continuation and any future implicit structural flow have no
        # independently writable scalar. They remain selectable for review,
        # but the terminal materializer rejects an attempted mutation rather
        # than widening authority to the whole origin node.
        return None

    raw_destination: object
    if field == "routes":
        routes = owner.get("routes")
        if type(routes) is not dict or route_key is None:
            raise AuditIntegrityError("guided edge routing private gate routes are malformed")
        raw_destination = routes.get(route_key)
    elif field == "fork_to":
        branches = owner.get("fork_to")
        if type(branches) is not list or fork_index is None or fork_index >= len(branches):
            raise AuditIntegrityError("guided edge routing private fork branches are malformed")
        raw_destination = branches[fork_index]
    else:
        raw_destination = owner.get(field)
    if type(raw_destination) is not str:
        return None
    return GuidedEdgeRoutingAuthority(
        field=field,
        route_key=route_key,
        fork_index=fork_index,
        before_destination=raw_destination,
    )


def _edge_preserved_state_fingerprint(
    state: CompositionState,
    *,
    owner_kind: Literal["source", "node", "output"],
    owner_key: str,
    routing: GuidedEdgeRoutingAuthority,
) -> str:
    """Hash all private state except the one selected routing scalar."""

    payload = cast(dict[str, Any], deep_thaw(state.to_dict()))
    if owner_kind == "source":
        sources = payload.get("sources")
        if type(sources) is not dict or type(sources.get(owner_key)) is not dict:
            raise AuditIntegrityError("guided edge preservation source owner is malformed")
        owner = cast(dict[str, Any], sources[owner_key])
    elif owner_kind == "output":
        outputs = payload.get("outputs")
        if type(outputs) is not list:
            raise AuditIntegrityError("guided edge preservation output owner is malformed")
        matches = [item for item in outputs if type(item) is dict and item.get("name") == owner_key]
        if len(matches) != 1:
            raise AuditIntegrityError("guided edge preservation output owner does not resolve exactly once")
        owner = cast(dict[str, Any], matches[0])
    else:
        nodes = payload.get("nodes")
        if type(nodes) is not list:
            raise AuditIntegrityError("guided edge preservation node owner is malformed")
        matches = [item for item in nodes if type(item) is dict and item.get("id") == owner_key]
        if len(matches) != 1:
            raise AuditIntegrityError("guided edge preservation node owner does not resolve exactly once")
        owner = cast(dict[str, Any], matches[0])
    marker = "__ELSPETH_SELECTED_EDGE_AUTHORITY__"
    if routing.field == "routes":
        routes = owner.get("routes")
        if type(routes) is not dict or routing.route_key is None or routing.route_key not in routes:
            raise AuditIntegrityError("guided edge preservation routes are malformed")
        routes[routing.route_key] = marker
    elif routing.field == "fork_to":
        branches = owner.get("fork_to")
        if type(branches) is not list or routing.fork_index is None or routing.fork_index >= len(branches):
            raise AuditIntegrityError("guided edge preservation fork branches are malformed")
        branches[routing.fork_index] = marker
    else:
        owner[routing.field] = marker
    return stable_hash(payload)


def resolve_guided_correction_target(
    *,
    requested: ComponentTarget,
    wire_payload: Mapping[str, Any],
    predecessor: CompositionState,
) -> GuidedCorrectionTarget:
    """Resolve one exact public stable ID without inventing private edge identity."""

    def resolve_owner(kind: str, stable_id: str) -> tuple[Literal["source", "node", "output"], str, int]:
        collection_name = {"source": "sources", "node": "nodes", "output": "outputs"}.get(kind)
        if collection_name is None:
            raise AuditIntegrityError("guided correction edge has an unsupported origin kind")
        components = wire_payload.get(collection_name)
        if type(components) is not list:
            raise AuditIntegrityError("guided correction wire projection lost a component collection")
        positions = [index for index, item in enumerate(components) if type(item) is dict and item.get("stable_id") == stable_id]
        if len(positions) != 1:
            raise AuditIntegrityError("guided correction stable target does not resolve exactly once")
        index = positions[0]
        if kind == "source":
            names = list(predecessor.sources)
            if index >= len(names):
                raise AuditIntegrityError("guided correction source target differs from private authority")
            return "source", names[index], index
        if kind == "node":
            if index >= len(predecessor.nodes):
                raise AuditIntegrityError("guided correction node target differs from private authority")
            return "node", predecessor.nodes[index].id, index
        if index >= len(predecessor.outputs):
            raise AuditIntegrityError("guided correction output target differs from private authority")
        return "output", predecessor.outputs[index].name, index

    edge_routing: GuidedEdgeRoutingAuthority | None = None
    edge_preserved_fingerprint: str | None = None
    edge_sibling_fingerprints: tuple[str, ...] = ()
    if requested.kind == "edge":
        connections = wire_payload.get("connections")
        if type(connections) is not list:
            raise AuditIntegrityError("guided correction wire projection lost connections")
        matches = [item for item in connections if type(item) is dict and item.get("stable_id") == requested.stable_id]
        if len(matches) != 1 or type(matches[0].get("from_endpoint")) is not dict:
            raise AuditIntegrityError("guided correction edge target differs from private authority")
        origin = matches[0]["from_endpoint"]
        owner_kind, owner_key, _owner_index = resolve_owner(origin.get("kind"), origin.get("stable_id"))
        collection_index = connections.index(matches[0])
        collection: Literal["sources", "nodes", "connections", "outputs"] = "connections"
        authority_key = None
        public_target = matches[0]
        edge_routing = _resolve_edge_routing_authority(
            wire_payload=wire_payload,
            public_target=public_target,
            predecessor=predecessor,
            owner_kind=owner_kind,
            owner_key=owner_key,
        )
        if edge_routing is not None:
            edge_preserved_fingerprint = _edge_preserved_state_fingerprint(
                predecessor,
                owner_kind=owner_kind,
                owner_key=owner_key,
                routing=edge_routing,
            )
    else:
        owner_kind, owner_key, collection_index = resolve_owner(requested.kind, requested.stable_id)
        authority_key = owner_key
        if requested.kind == "source":
            collection = "sources"
        elif requested.kind == "node":
            collection = "nodes"
        else:
            collection = "outputs"
        components = wire_payload.get(collection)
        if type(components) is not list or type(components[collection_index]) is not dict:
            raise AuditIntegrityError("guided correction target differs from public wire authority")
        public_target = components[collection_index]
    before_fingerprint = _wire_target_fingerprint(
        wire_payload,
        collection=collection,
        index=collection_index,
        authority=predecessor,
    )
    if before_fingerprint is None:
        raise AuditIntegrityError("guided correction target owner is absent from wire authority")
    if requested.kind == "edge":
        edge_connections = wire_payload.get("connections")
        if type(edge_connections) is not list:
            raise AuditIntegrityError("guided correction wire projection lost connections")
        matching_fingerprints = sum(
            _wire_target_fingerprint(
                wire_payload,
                collection="connections",
                index=index,
                authority=predecessor,
            )
            == before_fingerprint
            for index in range(len(edge_connections))
        )
        if matching_fingerprints != 1:
            raise AuditIntegrityError("guided correction edge semantics do not resolve exactly once")
        siblings: list[str] = []
        for index in range(len(edge_connections)):
            if index == collection_index:
                continue
            fingerprint = _wire_target_fingerprint(
                wire_payload,
                collection="connections",
                index=index,
                authority=predecessor,
            )
            if fingerprint is None:
                raise AuditIntegrityError("guided correction sibling edge has no stable semantics")
            siblings.append(fingerprint)
        edge_sibling_fingerprints = tuple(sorted(siblings))
    return GuidedCorrectionTarget(
        requested=requested,
        owner_kind=owner_kind,
        owner_key=owner_key,
        authority_key=authority_key,
        public_target=public_target,
        before_fingerprint=before_fingerprint,
        edge_routing=edge_routing,
        edge_preserved_fingerprint=edge_preserved_fingerprint,
        edge_sibling_fingerprints=edge_sibling_fingerprints,
    )


def resolve_guided_proposal_correction_target(
    *,
    requested: ComponentTarget,
    proposal_payload: Mapping[str, Any],
    predecessor: CompositionState,
) -> GuidedCorrectionTarget:
    """Resolve an exact correction target from the PROPOSE_PIPELINE projection.

    Proposal review nests sources and edges under ``graph`` while wire review
    names the same closed collections ``sources`` and ``connections`` at the
    top level.  Normalize only that structural envelope, then delegate to the
    one positional stable-ID/private-owner resolver.  No private option value
    is added to the provider-visible target.
    """

    graph = proposal_payload.get("graph")
    nodes = proposal_payload.get("nodes")
    outputs = proposal_payload.get("outputs")
    if (
        type(graph) is not dict
        or type(graph.get("sources")) is not list
        or type(graph.get("edges")) is not list
        or type(nodes) is not list
        or type(outputs) is not list
    ):
        raise AuditIntegrityError("guided proposal correction projection is malformed")
    return resolve_guided_correction_target(
        requested=requested,
        wire_payload={
            "sources": graph["sources"],
            "nodes": nodes,
            "connections": graph["edges"],
            "outputs": outputs,
        },
        predecessor=predecessor,
    )


def require_guided_correction_target_changed(
    wire_payload: Mapping[str, Any],
    target: GuidedCorrectionTarget,
    successor: CompositionState,
) -> None:
    """Reject a plan that edited elsewhere while leaving exact target semantics intact."""

    if target.requested.kind == "edge":
        connections = wire_payload.get("connections")
        if type(connections) is not list:
            raise AuditIntegrityError("guided correction successor lost public connections")
        fingerprints = tuple(
            _wire_target_fingerprint(
                wire_payload,
                collection="connections",
                index=index,
                authority=successor,
            )
            for index in range(len(connections))
        )
        if target.before_fingerprint in fingerprints:
            raise AuditIntegrityError("guided correction planner did not change the selected component")
        unmatched = list(fingerprints)
        for sibling in target.edge_sibling_fingerprints:
            try:
                unmatched.remove(sibling)
            except ValueError as exc:
                raise AuditIntegrityError("guided correction changed state outside selected edge authority") from exc
        if target.edge_sibling_fingerprints and len(unmatched) != 1:
            raise AuditIntegrityError("guided correction changed state outside selected edge authority")
        if target.edge_routing is not None and target.edge_preserved_fingerprint is not None:
            successor_preserved = _edge_preserved_state_fingerprint(
                successor,
                owner_kind=target.owner_kind,
                owner_key=target.owner_key,
                routing=target.edge_routing,
            )
            if successor_preserved != target.edge_preserved_fingerprint:
                raise AuditIntegrityError("guided correction changed state outside selected edge authority")
        return

    if target.requested.kind == "source":
        collection: Literal["sources", "nodes", "outputs"] = "sources"
        successor_keys = tuple(successor.sources)
    elif target.requested.kind == "node":
        collection = "nodes"
        successor_keys = tuple(node.id for node in successor.nodes)
    else:
        collection = "outputs"
        successor_keys = tuple(output.name for output in successor.outputs)
    positions = [index for index, key in enumerate(successor_keys) if key == target.authority_key]
    if not positions:
        return
    if len(positions) != 1:
        raise AuditIntegrityError("guided correction successor duplicated the selected component")

    fingerprint = _wire_target_fingerprint(
        wire_payload,
        collection=collection,
        index=positions[0],
        authority=successor,
    )
    if fingerprint == target.before_fingerprint:
        raise AuditIntegrityError("guided correction planner did not change the selected component")


def require_guided_proposal_correction_target_changed(
    proposal_payload: Mapping[str, Any],
    target: GuidedCorrectionTarget,
    successor: CompositionState,
) -> None:
    """Apply the exact correction-change proof to a Step-3 proposal projection."""

    graph = proposal_payload.get("graph")
    nodes = proposal_payload.get("nodes")
    outputs = proposal_payload.get("outputs")
    if (
        type(graph) is not dict
        or type(graph.get("sources")) is not list
        or type(graph.get("edges")) is not list
        or type(nodes) is not list
        or type(outputs) is not list
    ):
        raise AuditIntegrityError("guided proposal correction projection is malformed")
    require_guided_correction_target_changed(
        {
            "sources": graph["sources"],
            "nodes": nodes,
            "connections": graph["edges"],
            "outputs": outputs,
        },
        target,
        successor,
    )


def guided_private_reviewed_facts(guided: GuidedSession) -> dict[str, object]:
    """Return the exact ordered facts whose hash is stored in a guided ref."""

    return {
        "source_order": list(guided.source_order),
        "reviewed_sources": {stable_id: guided.reviewed_sources[stable_id].to_dict() for stable_id in guided.source_order},
        "output_order": list(guided.output_order),
        "reviewed_outputs": {stable_id: guided.reviewed_outputs[stable_id].to_dict() for stable_id in guided.output_order},
    }


def _provider_safe_deferred_constraint(
    constraint: (
        SubjectPresenceConstraint
        | OptionValueConstraint
        | ComponentCountConstraint
        | StatedGateRoutingConstraint
        | StatedPredicateConstraint
        | EdgeRouteConstraint
        | FailureRouteConstraint
    ),
) -> dict[str, object]:
    """Project one private constraint, exposing only operator-authored facts."""

    if type(constraint) is SubjectPresenceConstraint:
        return {
            "kind": constraint.kind,
            "subject": constraint.subject.to_dict(),
            "present": constraint.present,
        }
    if type(constraint) is OptionValueConstraint:
        value_type = {
            str: "string",
            int: "integer",
            float: "number",
            bool: "boolean",
            type(None): "null",
        }[type(constraint.value)]
        return {
            "kind": constraint.kind,
            "subject": constraint.subject.to_dict(),
            "operator": constraint.operator,
            "value_type": value_type,
            "value_present": constraint.value is not None,
        }
    if type(constraint) is ComponentCountConstraint:
        return {
            "kind": constraint.kind,
            "component_kind": constraint.component_kind,
            "plugin_kind": constraint.plugin_kind,
            "plugin_name": constraint.plugin_name,
            "operator": constraint.operator,
            "count": constraint.count,
        }
    if type(constraint) in {StatedPredicateConstraint, StatedGateRoutingConstraint}:
        return cast(dict[str, object], constraint.to_dict())
    if type(constraint) is EdgeRouteConstraint:
        return {
            "kind": constraint.kind,
            "from_subject": constraint.from_subject.to_dict(),
            "edge_type": constraint.edge_type,
            "to_subject": constraint.to_subject.to_dict(),
            "present": constraint.present,
        }
    if type(constraint) is FailureRouteConstraint:
        return {
            "kind": constraint.kind,
            "subject": constraint.subject.to_dict(),
            "failure_kind": constraint.failure_kind,
            "operator": constraint.operator,
            "target": constraint.target if constraint.target == "discard" else constraint.target.to_dict(),
        }
    raise AuditIntegrityError("guided deferred constraint is outside the provider-safe closed projection")


def guided_redacted_planner_context(guided: GuidedSession) -> dict[str, object]:
    """Build the closed provider-visible summary without option values or rows."""

    return {
        "schema": "guided.reviewed-planner-context.v1",
        "sources": [
            {
                "stable_id": stable_id,
                # Component names are server-authored routing identifiers
                # (also provider-visible via the current-state context, so no
                # new egress). Withholding them forces the planner to invent
                # names for on_success/edge references and dooms candidates to
                # "unknown node" rejections it cannot see through the closed
                # repair feedback (elspeth-859e2702dd).
                "name": source.name,
                "plugin": source.plugin,
                "observed_columns": list(source.observed_columns),
                # A reviewed source's schema mode and declared field names are
                # the same class of fact as an output's schema_mode /
                # required_fields below, and the planner needs them for the
                # same reason: without them a form-authored explicit schema
                # arrives as option_keys alone, so the planner cannot see which
                # fields exist and proposes topology that reads none of them
                # (or none at all). Names and modes only — never a declared
                # type, a path, or any other option value.
                "schema_mode": reviewed_schema_mode(schema),
                "declared_fields": list(reviewed_schema_declared_field_names(schema)),
                "option_keys": sorted(source.options),
                # Boolean only: the reference, path, and blob id stay server-side
                # (elspeth-0762539db5). Absence of the fact is not the same as an
                # unbound source, and the planner reads inline-data proposals
                # differently from storage-backed ones.
                "server_storage_bound": reviewed_source_is_blob_bound(source.options),
                "on_validation_failure": source.on_validation_failure,
            }
            for stable_id in guided.source_order
            for source in (guided.reviewed_sources[stable_id],)
            for schema in (source.options.get("schema"),)
        ],
        "outputs": [
            {
                "stable_id": stable_id,
                "name": output.name,
                "plugin": output.plugin,
                "required_fields": list(output.required_fields),
                "schema_mode": output.schema_mode,
                "option_keys": sorted(output.options),
                "on_write_failure": output.on_write_failure,
            }
            for stable_id in guided.output_order
            for output in (guided.reviewed_outputs[stable_id],)
        ],
        # Static usage line, never per-request data. Unlike freeform, the
        # staged surface hands the planner reviewed sink names up front, and
        # planners repeatedly wired fork-branch transforms straight to them
        # (guided session 04200b45: three coalesce_branch_unreachable repairs
        # all re-targeting the visible sink).
        "output_usage": (
            "Reviewed sink names are commit targets for the pipeline's FINAL producer only — "
            "never for branch transforms feeding a coalesce."
        ),
        "deferred_intents": [
            {
                "intent_id": intent.intent_id,
                "target_stage": intent.target_stage,
                "catalog_kind": intent.catalog_kind,
                "catalog_name": intent.catalog_name,
                "redacted_summary": intent.redacted_summary,
                "constraints": [_provider_safe_deferred_constraint(constraint) for constraint in intent.constraints],
            }
            for intent in guided.deferred_intents
        ],
    }


def guided_redacted_current_state_context(state: CompositionState) -> dict[str, object]:
    """Return provider-visible topology without any open option values."""

    return {
        "schema": "guided.current-state-context.v1",
        "version": state.version,
        "sources": [
            {
                "name": name,
                "plugin": source.plugin,
                "option_keys": sorted(source.options),
                "on_success": source.on_success,
                "on_validation_failure": source.on_validation_failure,
            }
            for name, source in state.sources.items()
        ],
        "nodes": [
            {
                "id": node.id,
                "node_type": node.node_type,
                "plugin": node.plugin,
                "option_keys": sorted(node.options),
                "input": node.input,
                "on_success": node.on_success,
                "on_error": node.on_error,
            }
            for node in state.nodes
        ],
        "outputs": [
            {
                "name": output.name,
                "plugin": output.plugin,
                "option_keys": sorted(output.options),
                "on_write_failure": output.on_write_failure,
            }
            for output in state.outputs
        ],
    }


def _sink_options_with_declared_required_fields(
    options: dict[str, JsonValue],
    declared_fields: Sequence[str],
) -> dict[str, JsonValue]:
    """Materialize reviewed declared output fields into the sink's schema contract.

    Step-2 field review captures ``SinkOutputResolved.required_fields``, but the
    reviewed sink OPTIONS never carried them, so both the composer sink-contract
    check and the runtime DAG validation (which key off ``options.schema``)
    silently abstained — the operator's declared contract was display-only (F3).
    Merge the declared fields into the sanctioned ``schema.required_fields``
    expression (contracts/schema.py) at the binder seam so one edit reaches
    candidate validation, the sealed proposal, committed state, YAML, and
    runtime.

    Rules:
    - empty ``declared_fields`` never reaches this helper (options stay
      byte-identical upstream);
    - author-typed ``schema.required_fields`` is MERGED (union, author order
      first), never overwritten;
    - a malformed schema block or malformed ``required_fields`` value is left
      untouched — candidate validation owns rejecting it
      (``contract_config_invalid``), and rewriting it here would mask the
      defect.
    """
    schema_key = "schema" if "schema" in options else ("schema_config" if "schema_config" in options else "schema")
    raw_schema = options.get(schema_key)
    if raw_schema is None:
        schema: dict[str, JsonValue] = {"mode": "observed"}
    elif type(raw_schema) is dict:
        schema = raw_schema
    else:
        return options
    existing = schema.get("required_fields")
    if existing is None:
        authored: list[str] = []
    elif type(existing) is list and all(type(item) is str for item in existing):
        authored = cast(list[str], existing)
    else:
        return options
    merged: list[JsonValue] = [*authored, *(field for field in declared_fields if field not in authored)]
    schema["required_fields"] = merged
    options[schema_key] = schema
    return options


def guided_reviewed_sink_options(reviewed_output: SinkOutputResolved) -> dict[str, JsonValue]:
    """Return one reviewed sink's options with its declared contract materialized.

    The single seam every pipeline carrying a reviewed output must pass
    through: the planner-authored candidate binder below, and the
    server-synthesized zero-transform sketch in
    ``ComposerServiceImpl.plan_guided_pipeline``. Those two seams had diverged —
    the sketch merged nothing, so step-2's declared output fields never reached
    ``options.schema.required_fields`` and the sink-contract check skipped
    (R2-F4). Both call ``guided_reviewed_sink_options`` now so a future third
    pipeline builder cannot re-open the same gap.
    """
    if type(reviewed_output) is not SinkOutputResolved:
        raise TypeError("reviewed_output must be an exact SinkOutputResolved")
    options = cast(dict[str, JsonValue], deep_thaw(reviewed_output.options))
    if not reviewed_output.required_fields:
        # Empty declared fields never reach the merge helper: the options stay
        # byte-identical, per its documented precondition.
        return options
    return _sink_options_with_declared_required_fields(options, reviewed_output.required_fields)


def guided_unproducible_output_fields(guided: GuidedSession) -> tuple[dict[str, JsonValue], ...]:
    """Name the declared output fields a zero-transform pipeline cannot produce.

    A pass-through pipeline emits exactly what the reviewed sources carry, so a
    declared sink field that appears in no source's observed columns and in no
    source's explicitly declared schema fields is unproducible without a
    transform. Candidate validation cannot be the guard here (R2-F4): before
    ``guided_reviewed_sink_options`` the sink carried no ``required_fields`` at
    all, so the sink-contract check skipped outright and sealed the sketch
    green. Merging them helps only when the producer PARTICIPATES in
    propagation — a blob-inspected source resolves an explicit ``flexible``
    schema and does, but a source whose schema stays ``observed`` abstains
    under ADR-007 and the check emits no contract at all — and even when it
    does fire it is an opaque ``sink_contract_violation`` the planner burns its
    repair budget on. The guided seam holds both halves of the fact BEFORE any
    pipeline is built, so it names the gap here and lets the caller act on it.

    Advisory shape only — this reports; the caller decides. The returned
    projection is provider-safe: every value is already in
    ``guided_redacted_planner_context`` (source ``observed_columns`` /
    ``declared_fields``, output ``required_fields``), so naming the gap adds no
    new egress. Values are plain JSON (sorted lists of ``str``) because the
    planner context is canonicalized before it reaches a provider.
    """
    if type(guided) is not GuidedSession:
        raise TypeError("guided must be an exact GuidedSession")
    available: set[str] = set()
    for stable_id in guided.source_order:
        source = guided.reviewed_sources[stable_id]
        available.update(source.observed_columns)
        available.update(reviewed_schema_declared_field_names(source.options.get("schema")))
    gaps: list[dict[str, JsonValue]] = []
    for stable_id in guided.output_order:
        output = guided.reviewed_outputs[stable_id]
        missing = sorted(set(output.required_fields) - available)
        if missing:
            gaps.append({"stable_id": stable_id, "fields": cast(JsonValue, missing)})
    return tuple(gaps)


def guided_unproducible_output_field_names(guided: GuidedSession) -> tuple[str, ...]:
    """Flatten :func:`guided_unproducible_output_fields` to sorted field names.

    The planner loop and the operator-visible failure both want "which fields
    is nothing producing", not "which sink declared them" — a zero-transform
    candidate is wrong for the union, and the repair is the same whichever sink
    asked. Derived from the per-output projection rather than recomputed so the
    two can never disagree about what the gap is.

    Every name here is a field the OPERATOR typed. Step-2 field review admits
    ``chosen`` only from ``_candidate_fields`` (the reviewed sources' observed
    columns) and forbids ``custom_inputs`` from overlapping them, so a name
    that survives the set difference came from ``custom_inputs`` verbatim.
    """
    names: set[str] = set()
    for gap in guided_unproducible_output_fields(guided):
        fields = gap["fields"]
        assert type(fields) is list  # built above as list[str]
        names.update(cast(list[str], fields))
    return tuple(sorted(names))


class GuidedCandidateBindingRejected(AuditIntegrityError):
    """Typed repairable candidate-shape rejection from the reviewed-component binder.

    Carries the closed ``error_code`` and the custody-safe ``connectivity``
    facts the planner loop projects into one budgeted repair turn
    (elspeth-572c642dbf). Every fact value is either a string the planner
    itself authored in the rejected candidate (aliases, node ids, connection
    names) or a reviewed sink name — the structural repair vocabulary the
    route-destination facts already disclose. The message keeps the
    ``guided planner candidate`` prefix so a catch path that does not know
    this type still classifies it as repairable, never a terminal 500.
    """

    def __init__(self, message: str, *, error_code: str, connectivity: Mapping[str, JsonValue]) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.connectivity: dict[str, JsonValue] = dict(connectivity)


def _guided_delta_rejection(
    error_code: str,
    *,
    facts: Mapping[str, JsonValue] | None = None,
) -> GuidedCandidateBindingRejected:
    return GuidedCandidateBindingRejected(
        "guided planner candidate delta violates reviewed mutation authority",
        error_code=error_code,
        connectivity=facts or {},
    )


def _canonical_schema_properties() -> Mapping[str, Any]:
    canonical = cast(dict[str, Any], deep_thaw(canonical_set_pipeline_schema()))
    properties = canonical.get("properties")
    if type(properties) is not dict:
        raise AuditIntegrityError("canonical pipeline schema lost its property inventory")
    return properties


def _closed_canonical_array(schema: object, *, name: str) -> Mapping[str, Any]:
    projected = cast(dict[str, Any], deep_thaw(schema))
    items = projected.get("items")
    if type(items) is not dict:
        raise AuditIntegrityError(f"canonical {name} schema lost its item contract")
    items["additionalProperties"] = False
    return projected


def _closed_canonical_metadata(schema: object) -> Mapping[str, Any]:
    projected = cast(dict[str, Any], deep_thaw(schema))
    projected["additionalProperties"] = False
    return projected


def _public_target_plugin_id(public_target: Mapping[str, Any]) -> str | None:
    plugin = public_target.get("plugin")
    if type(plugin) is str:
        return plugin
    if isinstance(plugin, Mapping) and plugin.get("kind") == "transform" and type(plugin.get("id")) is str:
        return cast(str, plugin["id"])
    return None


def _guided_node_patch_schema(
    canonical_node_properties: Mapping[str, Any],
    correction_target: GuidedCorrectionTarget,
) -> Mapping[str, Any]:
    """Project one selected node's public writable fields from canonical leaves."""

    public = correction_target.public_target
    behavior = public.get("behavior")
    behavior_kind = behavior.get("kind") if type(behavior) is dict else public.get("node_type")
    properties: dict[str, Any] = {
        "stable_id": {
            **deep_thaw(canonical_node_properties["id"]),
            "const": correction_target.requested.stable_id,
        }
    }
    public_fields_by_kind: dict[object, tuple[str, ...]] = {
        "transform": ("input", "on_success", "on_error"),
        "queue": ("input",),
        "gate": ("input", "on_error", "condition"),
        "aggregation": (
            "input",
            "on_success",
            "on_error",
            "trigger",
            "output_mode",
            "expected_output_count",
        ),
        "coalesce": ("input", "on_success", "policy", "merge", "timeout_seconds"),
        "row_union": ("input", "on_success", "timeout_seconds"),
    }
    fields = public_fields_by_kind.get(behavior_kind, ("input", "on_success", "on_error"))
    for field in fields:
        properties[field] = deep_thaw(canonical_node_properties[field])
    plugin_id = _public_target_plugin_id(public)
    option_keys = public_node_option_keys(plugin_id)
    if option_keys:
        options_schema = cast(dict[str, Any], deep_thaw(canonical_node_properties["options"]))
        options_schema["properties"] = {key: {} for key in option_keys}
        options_schema["additionalProperties"] = False
        properties["options"] = options_schema
    return {
        "type": "object",
        "properties": properties,
        "required": ["stable_id"],
        "additionalProperties": False,
    }


def guided_authorized_pipeline_schema(
    guided: GuidedSession,
    *,
    correction_target: GuidedCorrectionTarget | None,
) -> Mapping[str, Any]:
    """Derive the exact provider-writable guided terminal from canonical leaves.

    Initial reviewed-boundary planning owns topology only.  Correction
    contracts narrow further to the selected source/node/output owner and its
    incident routing.  Reviewed plugins, source/sink options, storage
    bindings, required fields, and failure policies are absent by
    construction.
    """
    if type(guided) is not GuidedSession:
        raise TypeError("guided must be an exact GuidedSession")
    if correction_target is not None and type(correction_target) is not GuidedCorrectionTarget:
        raise TypeError("correction_target must be an exact GuidedCorrectionTarget or None")
    canonical = _canonical_schema_properties()
    sources = cast(dict[str, Any], canonical["sources"])
    source_member = cast(dict[str, Any], sources["additionalProperties"])
    source_properties = cast(dict[str, Any], source_member["properties"])
    outputs = cast(dict[str, Any], canonical["outputs"])
    output_item = cast(dict[str, Any], outputs["items"])
    output_properties = cast(dict[str, Any], output_item["properties"])
    stable_id_schema = cast(dict[str, Any], deep_thaw(source_properties["plugin"]))

    def source_routes_schema(stable_ids: Sequence[str], *, exact_one: bool) -> Mapping[str, Any]:
        member: dict[str, Any] = {
            "type": "object",
            "properties": {
                "stable_id": {**stable_id_schema, "enum": list(stable_ids)},
                "on_success": deep_thaw(source_properties["on_success"]),
            },
            "required": ["stable_id", "on_success"],
            "additionalProperties": False,
        }
        count = 1 if exact_one else len(stable_ids)
        return {
            "type": "array",
            "items": member,
            "minItems": count,
            "maxItems": count,
        }

    def output_targets_schema(stable_ids: Sequence[str], *, exact_one: bool) -> Mapping[str, Any]:
        member = {
            "type": "object",
            "properties": {
                "stable_id": {
                    **deep_thaw(output_properties["sink_name"]),
                    "enum": list(stable_ids),
                }
            },
            "required": ["stable_id"],
            "additionalProperties": False,
        }
        count = 1 if exact_one else len(stable_ids)
        return {
            "type": "array",
            "items": member,
            "minItems": count,
            "maxItems": count,
        }

    nodes_schema = _closed_canonical_array(canonical["nodes"], name="nodes")
    edges_schema = _closed_canonical_array(canonical["edges"], name="edges")
    if correction_target is None:
        properties: dict[str, Any] = {
            "source_routes": source_routes_schema(guided.source_order, exact_one=False),
            "nodes": nodes_schema,
            "edges": edges_schema,
            "output_targets": output_targets_schema(guided.output_order, exact_one=False),
            "metadata": _closed_canonical_metadata(canonical["metadata"]),
        }
        required = ["source_routes", "nodes", "edges", "output_targets"]
    elif correction_target.requested.kind == "edge":
        edge_item = cast(dict[str, Any], edges_schema["items"])
        edge_properties = cast(dict[str, Any], edge_item["properties"])
        properties = {
            "edge_patch": {
                "type": "object",
                "properties": {
                    "stable_id": {
                        **deep_thaw(edge_properties["id"]),
                        "const": correction_target.requested.stable_id,
                    },
                    "to_node": deep_thaw(edge_properties["to_node"]),
                },
                "required": ["stable_id", "to_node"],
                "additionalProperties": False,
            }
        }
        required = ["edge_patch"]
    elif correction_target.owner_kind == "source":
        stable_ids = tuple(
            stable_id for stable_id in guided.source_order if guided.reviewed_sources[stable_id].name == correction_target.owner_key
        )
        if len(stable_ids) != 1:
            raise AuditIntegrityError("guided correction source owner does not resolve exactly once")
        properties = {
            "source_routes": source_routes_schema(stable_ids, exact_one=True),
            # Source routing corrections may add topology, but existing node
            # identities remain server-owned by the materializer.
            "nodes": nodes_schema,
            "edges": edges_schema,
        }
        required = ["source_routes", "nodes", "edges"]
    elif correction_target.owner_kind == "node":
        node_item = cast(dict[str, Any], nodes_schema["items"])
        node_properties = cast(dict[str, Any], node_item["properties"])
        properties = {
            "node_patch": _guided_node_patch_schema(node_properties, correction_target),
            "edges": edges_schema,
        }
        required = ["node_patch", "edges"]
    else:
        stable_ids = tuple(
            stable_id for stable_id in guided.output_order if guided.reviewed_outputs[stable_id].name == correction_target.owner_key
        )
        if len(stable_ids) != 1:
            raise AuditIntegrityError("guided correction output owner does not resolve exactly once")
        properties = {
            "output_targets": output_targets_schema(stable_ids, exact_one=True),
            "edges": edges_schema,
        }
        required = ["output_targets", "edges"]
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _exact_delta_members(value: object, *, allowed: set[str]) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) - allowed:
        raise _guided_delta_rejection("guided_delta_authority_violation")
    return cast(dict[str, Any], deep_thaw(value))


def _stable_delta_entries(
    value: object,
    *,
    allowed_keys: set[str],
    known_ids: frozenset[str],
) -> dict[str, Mapping[str, Any]]:
    if type(value) is not list:
        raise _guided_delta_rejection("guided_delta_authority_violation")
    entries: dict[str, Mapping[str, Any]] = {}
    for raw in value:
        entry = _exact_delta_members(raw, allowed=allowed_keys)
        stable_id = entry.get("stable_id")
        if type(stable_id) is not str or stable_id not in known_ids:
            raise _guided_delta_rejection(
                "guided_delta_unknown_stable_id",
                facts={"stable_id": cast(JsonValue, stable_id if type(stable_id) is str else "invalid")},
            )
        if stable_id in entries:
            raise _guided_delta_rejection(
                "guided_delta_duplicate_stable_id",
                facts={"stable_id": stable_id},
            )
        entries[stable_id] = entry
    return entries


def _require_reviewed_failure_routes(guided: GuidedSession) -> None:
    sink_names = {guided.reviewed_outputs[stable_id].name for stable_id in guided.output_order}
    unresolved: set[str] = set()
    for stable_id in guided.source_order:
        route = guided.reviewed_sources[stable_id].on_validation_failure
        if route not in {None, "discard"} and route not in sink_names:
            unresolved.add(route)
    for stable_id in guided.output_order:
        route = guided.reviewed_outputs[stable_id].on_write_failure
        if route not in {None, "discard"} and route not in sink_names:
            unresolved.add(route)
    if unresolved:
        raise _guided_delta_rejection(
            "guided_delta_reviewed_failure_route_required",
            facts={"routes": cast(JsonValue, sorted(unresolved))},
        )


def _incident_edges(
    value: object,
    *,
    owners: frozenset[str],
) -> list[Mapping[str, Any]]:
    if type(value) is not list:
        raise _guided_delta_rejection("guided_delta_authority_violation")
    edges: list[Mapping[str, Any]] = []
    ids: set[str] = set()
    for raw in value:
        edge = _exact_delta_members(raw, allowed={"id", "from_node", "to_node", "edge_type", "label"})
        edge_id = edge.get("id")
        if type(edge_id) is not str or edge_id in ids:
            raise _guided_delta_rejection("guided_delta_duplicate_stable_id")
        ids.add(edge_id)
        if edge.get("from_node") not in owners and edge.get("to_node") not in owners:
            raise _guided_delta_rejection(
                "guided_delta_nonincident_route",
                facts={"edge_id": edge_id},
            )
        edges.append(edge)
    return edges


def _replace_incident_edges(
    pipeline: MutableMapping[str, Any],
    *,
    owners: frozenset[str],
    replacements: Sequence[Mapping[str, Any]],
) -> None:
    raw_edges = pipeline.get("edges")
    if type(raw_edges) is not list:
        raise AuditIntegrityError("guided correction predecessor edges are malformed")
    retained = [
        edge for edge in raw_edges if type(edge) is not dict or (edge.get("from_node") not in owners and edge.get("to_node") not in owners)
    ]
    pipeline["edges"] = [*retained, *replacements]


def _merge_incident_edge_patches(
    pipeline: MutableMapping[str, Any],
    *,
    owners: frozenset[str],
    patches: Sequence[Mapping[str, Any]],
) -> None:
    """Overlay explicit incident edge IDs while preserving every omitted edge byte."""

    raw_edges = pipeline.get("edges")
    if type(raw_edges) is not list:
        raise AuditIntegrityError("guided correction predecessor edges are malformed")
    positions: dict[str, int] = {}
    for index, raw in enumerate(raw_edges):
        if type(raw) is not dict or type(raw.get("id")) is not str:
            raise AuditIntegrityError("guided correction predecessor edge is malformed")
        edge_id = cast(str, raw["id"])
        if edge_id in positions:
            raise AuditIntegrityError("guided correction predecessor duplicated an edge identity")
        positions[edge_id] = index
    for patch in patches:
        edge_id = cast(str, patch["id"])
        if edge_id not in positions:
            raw_edges.append(deep_thaw(patch))
            positions[edge_id] = len(raw_edges) - 1
            continue
        previous = raw_edges[positions[edge_id]]
        assert type(previous) is dict
        if previous.get("from_node") not in owners and previous.get("to_node") not in owners:
            raise _guided_delta_rejection(
                "guided_delta_nonincident_route",
                facts={"edge_id": edge_id},
            )
        raw_edges[positions[edge_id]] = deep_thaw(patch)


def _pipeline_edge_owner(
    pipeline: MutableMapping[str, Any],
    *,
    owner_kind: Literal["source", "node", "output"],
    owner_key: str,
) -> MutableMapping[str, Any]:
    if owner_kind == "source":
        sources = pipeline.get("sources")
        if type(sources) is not dict or type(sources.get(owner_key)) is not dict:
            raise AuditIntegrityError("guided edge correction predecessor source is malformed")
        return cast(dict[str, Any], sources[owner_key])
    if owner_kind == "output":
        outputs = pipeline.get("outputs")
        if type(outputs) is not list:
            raise AuditIntegrityError("guided edge correction predecessor outputs are malformed")
        matches = [item for item in outputs if type(item) is dict and item.get("name") == owner_key]
        if len(matches) != 1:
            raise AuditIntegrityError("guided edge correction predecessor output does not resolve exactly once")
        return cast(dict[str, Any], matches[0])
    nodes = pipeline.get("nodes")
    if type(nodes) is not list:
        raise AuditIntegrityError("guided edge correction predecessor nodes are malformed")
    matches = [item for item in nodes if type(item) is dict and item.get("id") == owner_key]
    if len(matches) != 1:
        raise AuditIntegrityError("guided edge correction predecessor node does not resolve exactly once")
    return cast(dict[str, Any], matches[0])


def _apply_selected_edge_route_patch(
    pipeline: MutableMapping[str, Any],
    *,
    authority: GuidedCorrectionTarget,
    destination: str,
) -> None:
    routing = authority.edge_routing
    if routing is None:
        raise _guided_delta_rejection("guided_delta_authority_violation")
    owner = _pipeline_edge_owner(
        pipeline,
        owner_kind=authority.owner_kind,
        owner_key=authority.owner_key,
    )
    if routing.field == "routes":
        routes = owner.get("routes")
        if type(routes) is not dict or routing.route_key is None or routes.get(routing.route_key) != routing.before_destination:
            raise AuditIntegrityError("guided edge correction route authority is stale")
        routes[routing.route_key] = destination
        return
    if routing.field == "fork_to":
        branches = owner.get("fork_to")
        if (
            type(branches) is not list
            or routing.fork_index is None
            or routing.fork_index >= len(branches)
            or branches[routing.fork_index] != routing.before_destination
        ):
            raise AuditIntegrityError("guided edge correction fork authority is stale")
        branches[routing.fork_index] = destination
        return
    if owner.get(routing.field) != routing.before_destination:
        raise AuditIntegrityError("guided edge correction scalar authority is stale")
    owner[routing.field] = destination


def materialize_guided_authorized_candidate(
    delta: Mapping[str, Any],
    authority: GuidedCorrectionTarget | None,
    guided: GuidedSession,
    current_state: CompositionState,
) -> GuidedBoundPipeline:
    """Materialize one admitted guided delta into the canonical candidate path."""
    if type(delta) is not dict:
        raise _guided_delta_rejection("guided_delta_authority_violation")
    if authority is not None and type(authority) is not GuidedCorrectionTarget:
        raise TypeError("authority must be an exact GuidedCorrectionTarget or None")
    if type(guided) is not GuidedSession:
        raise TypeError("guided must be an exact GuidedSession")
    if type(current_state) is not CompositionState:
        raise TypeError("current_state must be an exact CompositionState")
    _require_reviewed_failure_routes(guided)
    source_ids = frozenset(guided.source_order)
    output_ids = frozenset(guided.output_order)

    if authority is None:
        admitted = _exact_delta_members(
            delta,
            allowed={"source_routes", "nodes", "edges", "output_targets", "metadata"},
        )
        if not {"source_routes", "nodes", "edges", "output_targets"} <= admitted.keys():
            raise _guided_delta_rejection("guided_delta_authority_violation")
        routes = _stable_delta_entries(
            admitted["source_routes"],
            allowed_keys={"stable_id", "on_success"},
            known_ids=source_ids,
        )
        targets = _stable_delta_entries(
            admitted["output_targets"],
            allowed_keys={"stable_id"},
            known_ids=output_ids,
        )
        if set(routes) != source_ids or set(targets) != output_ids:
            raise _guided_delta_rejection("guided_delta_authority_violation")
        if type(admitted["nodes"]) is not list or type(admitted["edges"]) is not list:
            raise _guided_delta_rejection("guided_delta_authority_violation")
        shell: dict[str, Any] = {
            "sources": {
                guided.reviewed_sources[stable_id].name: {
                    "plugin": guided.reviewed_sources[stable_id].plugin,
                    "options": {},
                    "on_success": routes[stable_id].get("on_success"),
                    "on_validation_failure": guided.reviewed_sources[stable_id].on_validation_failure,
                }
                for stable_id in guided.source_order
            },
            "nodes": deep_thaw(admitted["nodes"]),
            "edges": deep_thaw(admitted["edges"]),
            "outputs": [
                {
                    "sink_name": guided.reviewed_outputs[stable_id].name,
                    "plugin": guided.reviewed_outputs[stable_id].plugin,
                    "options": {},
                    "on_write_failure": guided.reviewed_outputs[stable_id].on_write_failure,
                }
                for stable_id in guided.output_order
            ],
        }
        if "metadata" in admitted:
            shell["metadata"] = admitted["metadata"]
        return bind_guided_reviewed_components(shell, guided)

    predecessor = cast(dict[str, Any], deep_thaw(current_state.to_dict()))
    predecessor.pop("version", None)
    if authority.requested.kind == "edge":
        admitted = _exact_delta_members(delta, allowed={"edge_patch"})
        if set(admitted) != {"edge_patch"}:
            raise _guided_delta_rejection("guided_delta_authority_violation")
        edge_patch = _exact_delta_members(admitted["edge_patch"], allowed={"stable_id", "to_node"})
        stable_id = edge_patch.get("stable_id")
        destination = edge_patch.get("to_node")
        if stable_id != authority.requested.stable_id:
            raise _guided_delta_rejection(
                "guided_delta_unknown_stable_id",
                facts={"stable_id": cast(JsonValue, stable_id if type(stable_id) is str else "invalid")},
            )
        if type(destination) is not str:
            raise _guided_delta_rejection("guided_delta_authority_violation")
        _apply_selected_edge_route_patch(
            predecessor,
            authority=authority,
            destination=destination,
        )
    elif authority.owner_kind == "source":
        admitted = _exact_delta_members(delta, allowed={"source_routes", "nodes", "edges"})
        if set(admitted) != {"source_routes", "nodes", "edges"}:
            raise _guided_delta_rejection("guided_delta_authority_violation")
        stable_ids = frozenset(
            stable_id for stable_id in guided.source_order if guided.reviewed_sources[stable_id].name == authority.owner_key
        )
        routes = _stable_delta_entries(
            admitted["source_routes"],
            allowed_keys={"stable_id", "on_success"},
            known_ids=stable_ids,
        )
        if set(routes) != stable_ids or type(admitted["nodes"]) is not list:
            raise _guided_delta_rejection("guided_delta_authority_violation")
        raw_sources = predecessor.get("sources")
        raw_nodes = predecessor.get("nodes")
        if type(raw_sources) is not dict or type(raw_nodes) is not list:
            raise AuditIntegrityError("guided source correction predecessor is malformed")
        route = routes[next(iter(stable_ids))].get("on_success")
        if type(route) is not str:
            raise _guided_delta_rejection("guided_delta_authority_violation")
        cast(dict[str, Any], raw_sources[authority.owner_key])["on_success"] = route
        existing_ids = {node.get("id") for node in raw_nodes if type(node) is dict}
        additions: list[Mapping[str, Any]] = []
        for raw_node in admitted["nodes"]:
            node = _exact_delta_members(
                raw_node,
                allowed={
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
                },
            )
            node_id = node.get("id")
            if type(node_id) is not str or node_id in existing_ids or any(item.get("id") == node_id for item in additions):
                raise _guided_delta_rejection("guided_delta_duplicate_stable_id")
            additions.append(node)
        raw_nodes.extend(additions)
        owners = frozenset({authority.owner_key, *(cast(str, node["id"]) for node in additions)})
        edges = _incident_edges(admitted["edges"], owners=owners)
        _replace_incident_edges(predecessor, owners=owners, replacements=edges)
    elif authority.owner_kind == "node":
        admitted = _exact_delta_members(delta, allowed={"node_patch", "edges"})
        if set(admitted) != {"node_patch", "edges"}:
            raise _guided_delta_rejection("guided_delta_authority_violation")
        canonical_nodes = cast(dict[str, Any], _canonical_schema_properties()["nodes"])
        canonical_node_item = cast(dict[str, Any], canonical_nodes["items"])
        canonical_node_properties = cast(dict[str, Any], canonical_node_item["properties"])
        patch_contract = _guided_node_patch_schema(canonical_node_properties, authority)
        patch_properties = cast(dict[str, Any], patch_contract["properties"])
        node_patch = _exact_delta_members(admitted["node_patch"], allowed=set(patch_properties))
        if node_patch.get("stable_id") != authority.requested.stable_id:
            raise _guided_delta_rejection("guided_delta_unknown_stable_id")
        raw_nodes = predecessor.get("nodes")
        if type(raw_nodes) is not list:
            raise AuditIntegrityError("guided node correction predecessor is malformed")
        positions = [index for index, item in enumerate(raw_nodes) if type(item) is dict and item.get("id") == authority.owner_key]
        if len(positions) != 1:
            raise _guided_delta_rejection("guided_delta_unknown_stable_id")
        private_node = cast(dict[str, Any], raw_nodes[positions[0]])
        for key, value in node_patch.items():
            if key == "stable_id":
                continue
            if key != "options":
                private_node[key] = deep_thaw(value)
                continue
            if type(value) is not dict or type(private_node.get("options")) is not dict:
                raise _guided_delta_rejection("guided_delta_authority_violation")
            allowed_option_keys = frozenset(public_node_option_keys(_public_target_plugin_id(authority.public_target)))
            if set(value) - allowed_option_keys:
                raise _guided_delta_rejection("guided_delta_authority_violation")
            private_options = cast(dict[str, Any], private_node["options"])
            for option_key, option_value in value.items():
                private_options[option_key] = deep_thaw(option_value)
        owners = frozenset({authority.owner_key})
        edges = _incident_edges(admitted["edges"], owners=owners)
        _merge_incident_edge_patches(predecessor, owners=owners, patches=edges)
    else:
        admitted = _exact_delta_members(delta, allowed={"output_targets", "edges"})
        if set(admitted) != {"output_targets", "edges"}:
            raise _guided_delta_rejection("guided_delta_authority_violation")
        stable_ids = frozenset(
            stable_id for stable_id in guided.output_order if guided.reviewed_outputs[stable_id].name == authority.owner_key
        )
        targets = _stable_delta_entries(
            admitted["output_targets"],
            allowed_keys={"stable_id"},
            known_ids=stable_ids,
        )
        if set(targets) != stable_ids:
            raise _guided_delta_rejection("guided_delta_authority_violation")
        edges = _incident_edges(admitted["edges"], owners=frozenset({authority.owner_key}))
        raw_sources = predecessor.get("sources")
        raw_nodes = predecessor.get("nodes")
        if type(raw_sources) is not dict or type(raw_nodes) is not list:
            raise AuditIntegrityError("guided output correction predecessor is malformed")
        for edge in edges:
            if edge.get("to_node") != authority.owner_key:
                raise _guided_delta_rejection("guided_delta_nonincident_route", facts={"edge_id": cast(JsonValue, edge["id"])})
            origin = edge.get("from_node")
            edge_type = edge.get("edge_type")
            if type(origin) is not str or type(edge_type) is not str:
                raise _guided_delta_rejection("guided_delta_authority_violation")
            if origin in raw_sources and edge_type == "on_success":
                cast(dict[str, Any], raw_sources[origin])["on_success"] = authority.owner_key
                continue
            positions = [index for index, item in enumerate(raw_nodes) if type(item) is dict and item.get("id") == origin]
            if len(positions) != 1 or edge_type not in {"on_success", "on_error"}:
                raise _guided_delta_rejection(
                    "guided_delta_unknown_reference",
                    facts={"from_node": origin, "edge_type": edge_type},
                )
            cast(dict[str, Any], raw_nodes[positions[0]])[edge_type] = authority.owner_key
        _replace_incident_edges(predecessor, owners=frozenset({authority.owner_key}), replacements=edges)
    # Source-route and output-reconnection candidates above were constructed
    # from the private predecessor by this materializer, not reconstructed by
    # the provider.  The ordinary binder restores reviewed source/sink
    # authority without undoing the one explicitly-authorized scalar route.
    return bind_guided_reviewed_components(predecessor, guided)


@observation_boundary(
    tier=3,
    source=(
        "guided-planner LLM-authored candidate topology, after server-side deep_thaw and (at the second call "
        "site) partial source/output rebinding; 'nodes' and node 'branches' are not yet shape-validated when "
        "this reads them"
    ),
    source_param="bound",
    suppresses=("R5",),
    invariant=(
        "returns only the node ids, connection names, and branch connection names actually present in "
        "well-formed list/dict entries under bound['nodes']; a candidate whose 'nodes' (or a node's "
        "'branches') is not the expected list/dict shape contributes nothing for that dimension instead of "
        "raising. This function never raises on malformed source_param shape. Its result feeding a caller's "
        "dangling-reference check narrows known targets (fail-closed there), but the same empty result "
        "feeding an alias-collision check widens what is treated as non-colliding (fail-open there) — this "
        "function itself makes no fail-closed guarantee; callers must not treat an empty/partial result as "
        "proof that no such names exist in the candidate."
    ),
)
def _candidate_topology_reference_names(bound: Mapping[str, Any]) -> tuple[set[str], set[str], set[str]]:
    """Collect the candidate's node ids, consumed connections, and branch values.

    Coalesce/row_union ``branches`` VALUES are consumption sites too: each
    names the connection a branch transform publishes via ``on_success`` and
    the join consumes. Those names appear in no node's ``input`` and are not
    node ids, so without them every legal fork->coalesce candidate's
    intermediate connections would read as dangling (guided session 1f7241de,
    2026-07-22, four identical ``coalesce_branch_unreachable`` rejections
    manufactured by the binder's own rewrite).
    """
    node_ids: set[str] = set()
    connection_names: set[str] = set()
    branch_connection_names: set[str] = set()
    topology_nodes = bound.get("nodes")
    if isinstance(topology_nodes, list):
        for topology_node in topology_nodes:
            if not isinstance(topology_node, dict):
                continue
            for key, into in (("id", node_ids), ("input", connection_names)):
                value = topology_node.get(key)
                if type(value) is str and value:
                    into.add(value)
            branches = topology_node.get("branches")
            if isinstance(branches, dict):
                branch_connection_names.update(value for value in branches.values() if type(value) is str)
            elif isinstance(branches, list):
                branch_connection_names.update(value for value in branches if type(value) is str)
    return node_ids, connection_names, branch_connection_names


def _predecessor_reference_names(predecessor: CompositionState) -> set[str]:
    """Every routing name the predecessor mentions anywhere.

    Route-target amnesty for the dangling check in both flows whose provider
    saw only the REDACTED predecessor (correction, and prose ``replace``):
    the redaction withholds ``routes``/``fork_to``/``branches``, so a name
    the predecessor mentions there may be honestly re-emitted by a model
    that cannot know its consumer — in the correction flow, restoration can
    additionally leave a predecessor connection unconsumed while the
    selected node's rewiring is adjudicated downstream. Any name the
    predecessor mentions anywhere is therefore admitted and its residual
    ambiguity left to validation. These names are consulted SERVER-SIDE
    only; the rejection's connectivity facts must never include them
    (custody: restored gate/coalesce structure is withheld from the
    provider).
    """
    names: set[str] = {
        *predecessor.sources,
        *(source.on_success for source in predecessor.sources.values()),
        *(output.name for output in predecessor.outputs),
    }
    for node in predecessor.nodes:
        for value in (node.id, node.input, node.on_success, node.on_error):
            if type(value) is str and value:
                names.add(value)
        if node.routes is not None:
            names.update(value for value in node.routes.values() if type(value) is str)
        if node.fork_to is not None:
            names.update(value for value in node.fork_to if type(value) is str)
        branches = node.branches
        if isinstance(branches, Mapping):
            names.update(value for value in branches.values() if type(value) is str)
        elif branches is not None:
            names.update(value for value in branches if type(value) is str)
    return names


def _effective_sink_success_producers(
    resolver: ProducerResolver,
    sink_name: str,
) -> tuple[ProducerEntry, ...]:
    """Resolve each direct sink producer through structural gates independently."""

    direct = resolver.sink_producers(sink_name)
    if not direct:
        walked = resolver.walk_to_real_producer(sink_name)
        return () if walked is None else (walked,)
    resolved: list[ProducerEntry] = []
    seen: set[str] = set()
    for producer in direct:
        actual = resolver.walk_entry_to_real_producer(producer)
        if actual is None:
            continue
        if actual.producer_id in seen:
            continue
        seen.add(actual.producer_id)
        resolved.append(actual)
    return tuple(resolved)


def _exact_field_mapper_retained_fields(node: NodeSpec) -> tuple[str, ...] | None:
    """Return one valid exact mapper projection, otherwise abstain for this branch."""

    if node.node_type != "transform" or node.plugin != "field_mapper":
        return None
    options = node.options
    if not isinstance(options, Mapping) or options.get("select_only") is not True:
        return None
    mapping = options.get("mapping")
    if not isinstance(mapping, Mapping):
        return None
    entries = tuple(mapping.items())
    if any(
        type(source_field) is not str or not source_field or type(target_field) is not str or not target_field
        for source_field, target_field in entries
    ):
        return None
    source_fields = tuple(source_field for source_field, _target_field in entries)
    target_fields = tuple(target_field for _source_field, target_field in entries)
    if len(set(source_fields)) != len(source_fields) or len(set(target_fields)) != len(target_fields):
        return None
    source_field_set = set(source_fields)
    if any(source_field != target_field and target_field in source_field_set for source_field, target_field in entries):
        # FieldMapperConfig rejects rename chains/cycles because in-place
        # application would make their result order-dependent. Projection
        # analysis must abstain on the same malformed shape rather than claim
        # a reviewed-contract conflict for a mapper the runtime cannot build.
        return None
    return target_fields


def _success_projection_node(node: NodeSpec) -> NodeSpec:
    """Keep only the node-kind routing fields that can carry successful rows."""

    if node.node_type == "gate":
        # Gates publish successful rows only through routes/fork_to. A generic
        # planner node can still carry a malformed on_success value; ordinary
        # candidate validation owns that shape rather than projection analysis.
        # The same ownership applies to a fork_to list with no route selecting
        # the reserved "fork" action: those destinations are dead at runtime.
        has_fork_route = node.routes is not None and any(destination == "fork" for destination in node.routes.values())
        return replace(
            node,
            on_success=None,
            on_error=None,
            fork_to=node.fork_to if has_fork_route else None,
        )
    # Every other runtime node publishes success through on_success (or the
    # coalesce/queue implicit id). The generic planner schema also admits gate
    # fields on them, but those fields cannot prove a successful sink path.
    return replace(node, on_error=None, routes=None, fork_to=None)


def _reviewed_output_projection_conflict_for_bound_candidate(
    bound: Mapping[str, Any],
    guided: GuidedSession,
) -> ReviewedOutputProjectionConflict | None:
    """Return missing reviewed fields proven by terminal exact mappers.

    The check deliberately abstains unless a terminal success producer,
    after walking structural gates, is exactly a transform/field_mapper with
    a well-formed unique string mapping and ``select_only is True``. A
    downstream pass-through or any non-exact/malformed mapper remains owned by
    ordinary field-contract propagation.
    """

    raw_nodes = bound.get("nodes")
    raw_sources = bound.get("sources")
    if type(raw_nodes) is not list or type(raw_sources) is not dict:
        return None
    if any(type(node) is not dict for node in raw_nodes):
        return None
    source_keys = {"plugin", "options", "on_success", "on_validation_failure"}
    if any(type(name) is not str or type(source) is not dict or set(source) != source_keys for name, source in raw_sources.items()):
        return None
    raw_outputs = bound.get("outputs")
    if type(raw_outputs) is not list or any(type(output) is not dict for output in raw_outputs):
        return None
    try:
        projection_state = CompositionState.from_dict(
            {
                "version": 1,
                "metadata": {"name": "Projection check", "description": ""},
                "sources": deep_thaw(raw_sources),
                "nodes": deep_thaw(raw_nodes),
                "edges": [],
                "outputs": [
                    {
                        "name": output["sink_name"],
                        "plugin": output["plugin"],
                        "options": deep_thaw(output["options"]),
                        "on_write_failure": output["on_write_failure"],
                    }
                    for output in raw_outputs
                ],
            }
        )
    except (KeyError, TypeError, ValueError):
        return None
    # ProducerResolver serves validators that need both success and error
    # topology. This check is narrower: only rows reaching a reviewed sink on
    # a success route prove what the successful output projection retains.
    success_nodes = tuple(_success_projection_node(node) for node in projection_state.nodes)
    resolver = ProducerResolver.build(
        source=None,
        sources=projection_state.sources,
        nodes=success_nodes,
        sink_names=frozenset(output.name for output in projection_state.outputs),
    )
    missing_fields: list[str] = []
    for stable_id in guided.output_order:
        reviewed_output = guided.reviewed_outputs[stable_id]
        producers = _effective_sink_success_producers(resolver, reviewed_output.name)
        for producer in producers:
            node = resolver.get_node(producer.producer_id)
            if type(node) is not NodeSpec:
                continue
            target_fields = _exact_field_mapper_retained_fields(node)
            if target_fields is None:
                continue
            conflict = reviewed_output_projection_conflict(
                retained_fields=target_fields,
                required_fields=tuple(reviewed_output.required_fields),
            )
            if conflict is not None:
                missing_fields.extend(field for field in conflict.missing_fields if field not in missing_fields)
    if not missing_fields:
        return None
    return ReviewedOutputProjectionConflict(tuple(missing_fields))


def bind_guided_reviewed_components(
    pipeline: Mapping[str, Any],
    guided: GuidedSession,
    *,
    predecessor: CompositionState | None = None,
    correction_target: GuidedCorrectionTarget | None = None,
    enforce_route_targets: bool = True,
    route_amnesty_predecessor: CompositionState | None = None,
) -> GuidedBoundPipeline:
    """Replace provider-authored component configuration with reviewed authority.

    The planner remains responsible for topology.  Source and output plugin
    configuration was already reviewed by the operator, so those private
    values are restored server-side after the terminal model call and before
    candidate validation or proposal sealing.  During an exact guided
    correction, pre-existing nodes outside the selected correction owner are
    also server-owned: their option values and structural behavior are absent
    from the provider context, so accepting a model-authored reconstruction
    would turn redaction into mutation authority.
    """

    if (predecessor is None) != (correction_target is None):
        raise ValueError("predecessor and correction_target must be supplied together")
    if predecessor is not None and type(predecessor) is not CompositionState:
        raise TypeError("predecessor must be an exact CompositionState or None")
    if correction_target is not None and type(correction_target) is not GuidedCorrectionTarget:
        raise TypeError("correction_target must be an exact GuidedCorrectionTarget or None")
    if route_amnesty_predecessor is not None and type(route_amnesty_predecessor) is not CompositionState:
        raise TypeError("route_amnesty_predecessor must be an exact CompositionState or None")
    if route_amnesty_predecessor is not None and predecessor is not None:
        raise ValueError("route_amnesty_predecessor is the amnesty-only seam; the correction flow's predecessor already grants it")

    bound = cast(dict[str, Any], deep_thaw(pipeline))
    raw_sources = bound.get("sources")
    if type(raw_sources) is not dict:
        singular = bound.get("source")
        if len(guided.source_order) != 1 or type(singular) is not dict:
            raise AuditIntegrityError("guided planner candidate does not identify reviewed sources")
        source_id = guided.source_order[0]
        source = guided.reviewed_sources[source_id]
        if singular.get("name", source.name) != source.name:
            raise AuditIntegrityError("guided planner candidate source name differs from reviewed authority")
        raw_sources = {source.name: singular}
        bound.pop("source", None)
    expected_source_names = [guided.reviewed_sources[stable_id].name for stable_id in guided.source_order]
    if list(raw_sources) != expected_source_names:
        raise AuditIntegrityError("guided planner candidate sources differ from reviewed authority")
    rebound_sources: dict[str, GuidedBoundSource] = {}
    for stable_id in guided.source_order:
        reviewed = guided.reviewed_sources[stable_id]
        candidate = raw_sources[reviewed.name]
        if type(candidate) is not dict or type(candidate.get("on_success")) is not str:
            raise AuditIntegrityError("guided planner candidate source topology is malformed")
        rebound_sources[reviewed.name] = {
            "plugin": reviewed.plugin,
            "options": deep_thaw(reviewed.options),
            "on_success": candidate["on_success"],
            "on_validation_failure": reviewed.on_validation_failure,
        }
    bound["sources"] = rebound_sources

    raw_outputs = bound.get("outputs")
    if type(raw_outputs) is not list:
        raise AuditIntegrityError("guided planner candidate outputs are malformed")
    expected_output_names = [guided.reviewed_outputs[stable_id].name for stable_id in guided.output_order]
    # The planner is NOT given the reviewed output NAMES (only stable_id + plugin),
    # so it authors its own output name and wires sibling on_success/on_error to it.
    # Enforce STRUCTURAL authority (one candidate dict per reviewed output, in order
    # — plugin-by-position is validated separately) rather than an unsatisfiable NAME
    # equality, then remap the planner-invented output name to the reviewed authority
    # and rewrite every reference so the topology stays wired.
    if len(raw_outputs) != len(expected_output_names) or any(type(item) is not dict for item in raw_outputs):
        raise AuditIntegrityError("guided planner candidate outputs differ from reviewed authority")
    node_ids, connection_names, branch_connection_names = _candidate_topology_reference_names(bound)
    # Alias integrity BEFORE any rewrite (elspeth-572c642dbf): the rename map
    # below rewrites every reference matching an alias, so an alias shared by
    # two outputs (last-write-wins), reusing a sibling reviewed output's name,
    # or shadowing a node id / consumed connection / branch value would
    # retarget references that never meant that sink — silently converting an
    # invalid plan into a valid but semantically different pipeline. Ambiguous
    # aliasing is the planner's authoring slip: reject it as one typed
    # repairable candidate rejection instead of guessing.
    seen_aliases: set[str] = set()
    colliding_aliases: set[str] = set()
    topology_reference_names = node_ids | connection_names | branch_connection_names
    for index, stable_id in enumerate(guided.output_order):
        candidate = raw_outputs[index]
        assert type(candidate) is dict
        candidate_name = candidate.get("sink_name", candidate.get("name"))
        if type(candidate_name) is not str:
            continue
        if candidate_name in seen_aliases:
            colliding_aliases.add(candidate_name)
        seen_aliases.add(candidate_name)
        if candidate_name == guided.reviewed_outputs[stable_id].name:
            continue
        if candidate_name in expected_output_names or candidate_name in topology_reference_names:
            colliding_aliases.add(candidate_name)
    if colliding_aliases:
        raise GuidedCandidateBindingRejected(
            "guided planner candidate output aliases collide with sibling outputs or topology names",
            error_code="guided_output_alias_collision",
            connectivity={"colliding_aliases": cast(JsonValue, sorted(colliding_aliases))},
        )
    # The reverse direction: the planner never sees reviewed sink NAMES, so it
    # can innocently author a node id / connection / branch value equal to one.
    # The rename below then injects that reviewed name as a sink, and the DAG
    # builder resolves route targets against sink names BEFORE node/connection
    # names — every reference meant for the planner's node would silently
    # deliver rows to the sink and skip it, building green where an unshadowed
    # name fails loudly as an unknown target. The alias-equality skip above
    # makes this unreachable for the forward check, so guard it here. The
    # prose AMEND binder opts out (enforce_route_targets=False) to classify
    # the same shape under its own closed repair dispositions; REPLACE has no
    # dispositions of its own and enforces like the plain flow.
    shadowed_reviewed_names = (
        sorted(name for name in expected_output_names if name in topology_reference_names) if enforce_route_targets else []
    )
    if shadowed_reviewed_names:
        raise GuidedCandidateBindingRejected(
            "guided planner candidate topology names shadow reviewed sink names",
            error_code="guided_reviewed_name_shadowed",
            connectivity={"shadowed_reviewed_names": cast(JsonValue, shadowed_reviewed_names)},
        )
    output_rename: dict[str, str] = {}
    rebound_outputs: list[GuidedBoundOutput] = []
    for index, stable_id in enumerate(guided.output_order):
        reviewed_output = guided.reviewed_outputs[stable_id]
        candidate = raw_outputs[index]
        assert type(candidate) is dict
        candidate_name = candidate.get("sink_name", candidate.get("name"))
        if type(candidate_name) is str and candidate_name != reviewed_output.name:
            output_rename[candidate_name] = reviewed_output.name
        rebound_options = guided_reviewed_sink_options(reviewed_output)
        rebound_outputs.append(
            {
                "sink_name": reviewed_output.name,
                "plugin": reviewed_output.plugin,
                "options": rebound_options,
                "on_write_failure": reviewed_output.on_write_failure,
            }
        )
    bound["outputs"] = rebound_outputs
    if output_rename:
        # Outputs are terminal sinks referenced BY NAME in on_success/on_error
        # routing; rewrite every sibling reference to the renamed reviewed output
        # so the topology stays wired after the name is restored to authority.
        sources_map = bound.get("sources")
        if isinstance(sources_map, dict):
            for member in sources_map.values():
                if isinstance(member, dict) and member.get("on_success") in output_rename:
                    member["on_success"] = output_rename[member["on_success"]]
        singular_source = bound.get("source")
        if isinstance(singular_source, dict) and singular_source.get("on_success") in output_rename:
            singular_source["on_success"] = output_rename[singular_source["on_success"]]
        topology_nodes = bound.get("nodes")
        if isinstance(topology_nodes, list):
            for topology_node in topology_nodes:
                if not isinstance(topology_node, dict):
                    continue
                for edge_key in ("on_success", "on_error"):
                    if topology_node.get(edge_key) in output_rename:
                        topology_node[edge_key] = output_rename[topology_node[edge_key]]
                # Gate routes{} values and fork_to[] entries are sink-reference
                # positions too: the DAG builder resolves both against sink
                # names before deferring to connection names, so an invented
                # output name here is as live a reference as on_success.
                gate_routes = topology_node.get("routes")
                if isinstance(gate_routes, dict):
                    for route_label, route_target in gate_routes.items():
                        if type(route_target) is str and route_target in output_rename:
                            gate_routes[route_label] = output_rename[route_target]
                gate_fork_to = topology_node.get("fork_to")
                if isinstance(gate_fork_to, list):
                    topology_node["fork_to"] = [
                        output_rename[branch] if type(branch) is str and branch in output_rename else branch for branch in gate_fork_to
                    ]
        topology_edges = bound.get("edges")
        if isinstance(topology_edges, list):
            for topology_edge in topology_edges:
                if not isinstance(topology_edge, dict):
                    continue
                for endpoint_key in ("from_node", "to_node"):
                    if topology_edge.get(endpoint_key) in output_rename:
                        topology_edge[endpoint_key] = output_rename[topology_edge[endpoint_key]]

    if predecessor is not None and correction_target is not None:
        raw_nodes = bound.get("nodes")
        if type(raw_nodes) is not list:
            raise AuditIntegrityError("guided planner candidate nodes are malformed")
        selected_node_id = correction_target.owner_key if correction_target.owner_kind == "node" else None
        predecessor_nodes = predecessor.to_dict()["nodes"]
        for private_node in predecessor_nodes:
            private_node_id = private_node["id"]
            positions = [
                index
                for index, candidate_node in enumerate(raw_nodes)
                if type(candidate_node) is dict and candidate_node.get("id") == private_node_id
            ]
            if len(positions) != 1:
                selected = private_node_id == selected_node_id
                qualifier = "selected" if selected else "unselected"
                raise AuditIntegrityError(f"guided planner candidate changed a {qualifier} predecessor node identity")
            position = positions[0]
            if private_node_id != selected_node_id:
                raw_nodes[position] = private_node
                continue
            candidate_node = raw_nodes[position]
            if type(candidate_node) is not dict or type(candidate_node.get("options")) is not dict:
                # Message must carry the "guided planner candidate" prefix:
                # the planner loop repairs (rather than terminalizes) binder
                # AuditIntegrityErrors by that prefix, and a model emitting
                # the selected node without an options dict is an authoring
                # slip, not an integrity breach (elspeth-d923304d18).
                raise GuidedCandidateBindingRejected(
                    "guided planner candidate selected node options are malformed",
                    error_code="guided_delta_authority_violation",
                    connectivity={},
                )
            if candidate_node.get("node_type") != private_node.get("node_type") or candidate_node.get("plugin") != private_node.get(
                "plugin"
            ):
                raise AuditIntegrityError("guided planner candidate changed selected predecessor node type or plugin")
            private_options = cast(dict[str, Any], deep_thaw(private_node["options"]))
            candidate_options = cast(dict[str, Any], candidate_node["options"])
            for key in public_node_option_keys(cast(str | None, private_node.get("plugin"))):
                if key in candidate_options:
                    private_options[key] = deep_thaw(candidate_options[key])
                else:
                    private_options.pop(key, None)
            candidate_node["options"] = private_options
    projection_conflict = _reviewed_output_projection_conflict_for_bound_candidate(bound, guided)
    if projection_conflict is not None:
        raise GuidedCandidateBindingRejected(
            "guided planner candidate exact projection omits reviewed output fields",
            error_code=projection_conflict.error_code,
            connectivity={"missing_fields": cast(JsonValue, list(projection_conflict.missing_fields))},
        )
    # Residual dangling sink references. Observed planner slip: the outputs
    # and edges use the reviewed name correctly, but one stale invented name
    # survives in a routing field — the rename map is then empty and the
    # rewrite above never runs. Only an explicitly recorded candidate-output
    # alias is provably the sink (elspeth-572c642dbf); the old unknown->sink
    # rewrite could not distinguish a stale alias from a misspelled
    # intermediate connection or an omitted consumer, so it converted invalid
    # plans into valid but semantically different pipelines. Reject instead,
    # carrying the facts a one-turn repair needs. Validation cannot own this
    # rejection: the binder always rewrites source/output CONFIG, so
    # source-attributed validation entries project config-masked — no
    # connectivity facts — and the repair would be blind.
    #
    # Enforcement spans every flow where the model owns the routing it wrote.
    # The PLAIN planning flow checks the full candidate. The CORRECTION flow
    # checks the post-restoration result with predecessor-name amnesty:
    # restoration can legitimately leave a predecessor connection unconsumed
    # while the selected node's rewiring is adjudicated downstream, so every
    # name the predecessor mentions anywhere is admitted and left to
    # validation — but a name in NEITHER the bound topology NOR the
    # predecessor is provably not withheld structure, and skipping it here
    # burned the correction repair loop to REPAIR_EXHAUSTED on the same
    # config-masked feedback the plain flow repairs in one turn. The prose
    # AMEND binder alone opts out (enforce_route_targets=False): its
    # adjudication rebuilds routing from predecessor authority into one
    # closed repair disposition. REPLACE enforces with the SAME amnesty
    # (``route_amnesty_predecessor``): although its explicit destructive
    # authority means the model owns the topology it staged, the model only
    # ever saw the redacted predecessor — routes{}/fork_to[]/branches are
    # withheld — so a faithful re-emission of a fork/coalesce predecessor
    # carries branch connections nothing visible consumes. Rejecting those
    # names steered the repair prompt into mangling honest branch wiring and
    # burned to REPAIR_EXHAUSTED (the 1f7241de failure class); a
    # predecessor-mentioned name is left to validation, while a name in
    # NEITHER the candidate NOR the predecessor stays provably invented and
    # is rejected with one-turn repair facts.
    #
    # Gate routes{}/fork_to[] deliberately stay out of the check: a value
    # outside known_targets is ambiguous there (stale sink name vs. a
    # not-yet-consumed connection, e.g. a predecessor route whose consumer
    # the candidate does not carry — the 1f7241de failure class). The RENAME
    # map above still retargets them exactly; residual routes/fork_to
    # ambiguity belongs to validation. A dangling edge from_node likewise
    # stays: it is never a sink reference. "discard" is the legal drop-route
    # sentinel, not a reference.
    if not enforce_route_targets:
        return cast(GuidedBoundPipeline, bound)
    # Custody split for the facts below: known_targets may consult restored
    # (withheld) structure, but ``consumable_connections`` crossing back to
    # the provider is built from the PRE-restoration candidate sets computed
    # above — strings the model itself authored.
    candidate_consumable_connections = connection_names | branch_connection_names
    node_ids, connection_names, branch_connection_names = _candidate_topology_reference_names(bound)
    known_targets = set(expected_output_names) | node_ids | connection_names | branch_connection_names | {"discard"}
    amnesty_predecessor = predecessor if predecessor is not None else route_amnesty_predecessor
    if amnesty_predecessor is not None:
        known_targets |= _predecessor_reference_names(amnesty_predecessor)
    dangling_references: set[str] = set()

    def _collect_dangling(member: Mapping[str, Any], key: str) -> None:
        value = member.get(key)
        if type(value) is str and value and value not in known_targets:
            dangling_references.add(value)

    for member in bound["sources"].values():
        _collect_dangling(cast(dict[str, JsonValue], member), "on_success")
    topology_nodes = bound.get("nodes")
    if isinstance(topology_nodes, list):
        for topology_node in topology_nodes:
            if isinstance(topology_node, dict):
                for key in ("on_success", "on_error"):
                    if topology_node.get(key) is not None:
                        _collect_dangling(topology_node, key)
    topology_edges = bound.get("edges")
    if isinstance(topology_edges, list):
        for topology_edge in topology_edges:
            if isinstance(topology_edge, dict):
                _collect_dangling(topology_edge, "to_node")
    if dangling_references:
        raise GuidedCandidateBindingRejected(
            "guided planner candidate routing references unknown destinations",
            error_code="guided_route_target_unknown",
            connectivity={
                "dangling_references": cast(JsonValue, sorted(dangling_references)),
                "declared_sinks": cast(JsonValue, sorted(expected_output_names)),
                "consumable_connections": cast(JsonValue, sorted(candidate_consumable_connections)),
            },
        )
    return cast(GuidedBoundPipeline, bound)


def bind_guided_prose_revision_candidate(
    pipeline: Mapping[str, Any],
    guided: GuidedSession,
    *,
    authority: GuidedRevisionAuthority,
) -> GuidedRevisionBindingResult:
    """Bind an unscoped prose revision without granting hidden-state custody.

    The provider sees node identity and topology but not private option values
    or the full control semantics of gates/barriers.  In conservative ``amend``
    mode every predecessor node is therefore reconstructed from private
    authority.  Only ``input``/``on_success`` rewiring that actually names a
    newly inserted node's connection is admitted.  Any attempted removal,
    duplication, type/plugin substitution, or protected-field change is
    restored server-side and returned as one closed repair disposition.

    ``replace`` is intentionally simpler: its explicit destructive authority
    permits node replacement/removal, while the ordinary guided binder still
    restores the reviewed source and output contracts.
    """

    if type(authority) is not GuidedRevisionAuthority:
        raise TypeError("authority must be an exact GuidedRevisionAuthority")
    # ``amend`` opts out of the binder's route-target rejection: its
    # reconstruction below rebuilds routing from predecessor authority into
    # one closed repair disposition. ``replace`` stages the bound candidate
    # with NO further adjudication, so it takes the rejection — without it a
    # stale invented sink alias fell through to config-masked validation and
    # burned the repair loop to REPAIR_EXHAUSTED — but WITH predecessor-name
    # amnesty: the replace provider sees the same redacted context as the
    # correction flow (routes/fork_to/branches withheld), so a faithful
    # re-emission of a fork/coalesce predecessor names branch connections it
    # was never shown the consumers of. Only names in NEITHER the candidate
    # NOR the predecessor are provably invented and rejected.
    bound = bind_guided_reviewed_components(
        pipeline,
        guided,
        enforce_route_targets=authority.mode == "replace",
        route_amnesty_predecessor=authority.predecessor if authority.mode == "replace" else None,
    )
    if authority.mode == "replace":
        return GuidedRevisionBindingResult(pipeline=bound, rejection_code=None)

    predecessor = authority.predecessor
    predecessor_dict = predecessor.to_dict()
    predecessor_nodes = cast(list[dict[str, Any]], predecessor_dict["nodes"])
    predecessor_by_id = {node["id"]: node for node in predecessor_nodes}
    raw_nodes = bound.get("nodes")
    if type(raw_nodes) is not list:
        raise AuditIntegrityError("guided planner candidate nodes are malformed")

    candidate_nodes = [node for node in raw_nodes if type(node) is dict]
    malformed_node = len(candidate_nodes) != len(raw_nodes)
    added_nodes = [node for node in candidate_nodes if node.get("id") not in predecessor_by_id]

    def connection_values(node: Mapping[str, Any]) -> set[str]:
        values: set[str] = set()
        node_id = node.get("id")
        if type(node_id) is str:
            values.add(node_id)
        for key in ("input", "on_success"):
            value = node.get(key)
            if type(value) is str:
                values.add(value)
        for key in ("routes", "branches"):
            value = node.get(key)
            if isinstance(value, Mapping):
                values.update(item for item in value.values() if type(item) is str)
            elif type(value) is list:
                values.update(item for item in value if type(item) is str)
        fork_to = node.get("fork_to")
        if type(fork_to) is list:
            values.update(item for item in fork_to if type(item) is str)
        return values

    predecessor_connections = {
        *predecessor.sources,
        *(source.on_success for source in predecessor.sources.values()),
        *(output.name for output in predecessor.outputs),
    }
    for node in predecessor_nodes:
        predecessor_connections.update(connection_values(node))
    # Existing members may be rewired only through a genuinely new insertion
    # port.  Values merely mentioned by a new node (an old source/output/node,
    # a route destination, or a fork branch) do not widen authority.  ELSPETH
    # topology connects producers/consumers by input/on_success channel names,
    # which need not equal a node id, so admit the new node's direct ports as
    # well as its id, but only when that value is absent from the predecessor.
    insertion_connections = {
        value
        for node in added_nodes
        for key in ("id", "input", "on_success")
        if type(value := node.get(key)) is str and value not in predecessor_connections
    }

    violated = malformed_node
    predecessor_order = [node["id"] for node in predecessor_nodes]
    candidate_existing_order = [
        node_id for node in candidate_nodes if type(node_id := node.get("id")) is str and node_id in predecessor_by_id
    ]
    if candidate_existing_order != predecessor_order:
        violated = True
    rebuilt_by_id: dict[str, dict[str, Any]] = {}
    for private_node in predecessor_nodes:
        node_id = private_node["id"]
        matches = [node for node in candidate_nodes if node.get("id") == node_id]
        if len(matches) != 1:
            violated = True
            rebuilt_by_id[node_id] = cast(dict[str, Any], deep_thaw(private_node))
            continue
        candidate_node = matches[0]
        if candidate_node.get("node_type") != private_node.get("node_type"):
            violated = True
        if "plugin" in candidate_node and candidate_node.get("plugin") != private_node.get("plugin"):
            violated = True
        if set(candidate_node) - set(private_node):
            violated = True
        protected_keys = set(private_node) - {"id", "node_type", "plugin", "input", "on_success"}
        if any(key in candidate_node and candidate_node[key] != private_node[key] for key in protected_keys):
            violated = True

        rebuilt = cast(dict[str, Any], deep_thaw(private_node))
        for rewiring_key in ("input", "on_success"):
            if rewiring_key not in candidate_node:
                continue
            candidate_value = candidate_node[rewiring_key]
            if candidate_value == private_node.get(rewiring_key):
                continue
            if type(candidate_value) is str and candidate_value in insertion_connections:
                rebuilt[rewiring_key] = candidate_value
            else:
                violated = True
        rebuilt_by_id[node_id] = rebuilt

    # Preserve the provider's insertion order while replacing every old node
    # occurrence with exactly one server-owned reconstruction. Missing old
    # nodes are appended in predecessor order only on the rejected candidate;
    # this keeps validation safe without making the repair disposition depend
    # on a malformed partial topology.
    rebuilt_nodes: list[dict[str, Any]] = []
    emitted_existing: set[str] = set()
    for node in candidate_nodes:
        node_id = node.get("id")
        if type(node_id) is str and node_id in rebuilt_by_id:
            if node_id not in emitted_existing:
                rebuilt_nodes.append(rebuilt_by_id[node_id])
                emitted_existing.add(node_id)
            continue
        rebuilt_nodes.append(node)
    for private_node in predecessor_nodes:
        if private_node["id"] not in emitted_existing:
            rebuilt_nodes.append(rebuilt_by_id[private_node["id"]])
    if violated:
        # A rejected reconstruction never stages, but keeping its server-side
        # validation input deterministic avoids secondary topology errors
        # obscuring the one closed amend-contract disposition.
        rebuilt_nodes = [cast(dict[str, Any], deep_thaw(node)) for node in predecessor_nodes] + added_nodes
    bound["nodes"] = rebuilt_nodes

    predecessor_sources = cast(dict[str, dict[str, Any]], predecessor_dict["sources"])
    for source_name, predecessor_source in predecessor_sources.items():
        candidate_source = bound["sources"].get(source_name)
        if candidate_source is None:
            violated = True
            continue
        candidate_success = candidate_source["on_success"]
        predecessor_success = predecessor_source["on_success"]
        if candidate_success != predecessor_success and candidate_success not in insertion_connections:
            violated = True
            candidate_source["on_success"] = predecessor_success

    return GuidedRevisionBindingResult(
        pipeline=bound,
        rejection_code="guided_amend_contract_violation" if violated else None,
    )


def require_guided_prose_revision_successor(
    successor: CompositionState,
    guided: GuidedSession,
    *,
    authority: GuidedRevisionAuthority,
) -> None:
    """Recheck a service-returned revision against route-held authority.

    The production service binds and rejects candidates inside its repair
    loop.  The route repeats the closed contract before staging so a
    substituted service cannot omit that boundary, return unbound reviewed
    fields, or supersede an amend predecessor unchanged.
    """

    if type(successor) is not CompositionState:
        raise TypeError("successor must be an exact CompositionState")
    binding = bind_guided_prose_revision_candidate(successor.to_dict(), guided, authority=authority)
    if binding.rejection_code is not None:
        raise AuditIntegrityError("guided prose revision successor violates amend authority")
    predecessor = authority.predecessor
    if authority.mode == "amend":
        successor_nodes = {node["id"]: node for node in successor.to_dict()["nodes"]}
        bound_nodes = {node["id"]: node for node in binding.pipeline["nodes"] if type(node) is dict and type(node.get("id")) is str}
        for predecessor_node in predecessor.to_dict()["nodes"]:
            node_id = predecessor_node["id"]
            if successor_nodes.get(node_id) != bound_nodes.get(node_id):
                raise AuditIntegrityError("guided prose revision successor differs from bound node authority")
    if set(successor.sources) != set(predecessor.sources):
        raise AuditIntegrityError("guided prose revision successor changed reviewed source identity")
    for source_name, predecessor_source in predecessor.sources.items():
        successor_source = successor.sources[source_name]
        if (
            successor_source.plugin != predecessor_source.plugin
            or successor_source.options != predecessor_source.options
            or successor_source.on_validation_failure != predecessor_source.on_validation_failure
        ):
            raise AuditIntegrityError("guided prose revision successor changed reviewed source authority")
    if successor.outputs != predecessor.outputs:
        raise AuditIntegrityError("guided prose revision successor changed reviewed output authority")
    if guided_revision_execution_hash(successor) == guided_revision_execution_hash(predecessor):
        raise AuditIntegrityError("guided prose revision successor is unchanged")


def _canonical_state_from_private_pipeline(raw: dict[str, JsonValue]) -> CompositionState:
    """Canonicalise a planner-authored private pipeline dict into a state.

    The set_pipeline tool schema leaves per-node ``plugin``/``on_success``/
    ``on_error``/``options`` and per-source ``options``/``on_validation_failure``
    optional (a coalesce is TOLD to omit ``on_success``); the canonical Spec
    ``from_dict`` constructors are strict. Apply the same defaults the
    freeform candidate builder applies so a schema-legal plan cannot die at
    this adapter.
    """
    if "source" in raw and "sources" not in raw:
        source = raw.pop("source")
        raw["sources"] = {"source": source} if source is not None else {}
    sources = raw.get("sources")
    if type(sources) is dict:
        for source_spec in sources.values():
            if type(source_spec) is dict:
                source_spec.setdefault("options", {})
                source_spec.setdefault("on_validation_failure", "discard")
    nodes = raw.get("nodes")
    if type(nodes) is list:
        for node in nodes:
            if type(node) is dict:
                node.setdefault("plugin", None)
                node.setdefault("on_success", None)
                # Mirror build_set_pipeline_candidate's derivation exactly: a
                # transform/aggregation with on_error omitted or blank derives
                # the "discard" error flow. The validated candidate state
                # carried that default, but the proposal seals the raw planner
                # dict — defaulting to None here drops the node_error edge at
                # projection and kills a validation-accepted plan at the wire
                # contract's exact success+error flow check.
                node["on_error"] = node.get("on_error") or ("discard" if node.get("node_type") in ("transform", "aggregation") else None)
                node.setdefault("options", {})
    outputs = raw.get("outputs")
    if type(outputs) is list:
        for output in outputs:
            if type(output) is dict and "sink_name" in output:
                output["name"] = output.pop("sink_name")
    edges = raw.get("edges")
    if type(edges) is list:
        for edge in edges:
            if type(edge) is dict:
                # set_pipeline's tool schema makes label optional and its
                # handler reads it with .get(); canonical EdgeSpec.from_dict
                # is strict, so apply the same default at this adapter.
                edge.setdefault("label", None)
    raw.setdefault("metadata", {"name": "Untitled Pipeline", "description": ""})
    raw["version"] = 1
    try:
        return CompositionState.from_dict(cast(dict[str, Any], raw))
    except (KeyError, TypeError, ValueError) as exc:
        raise AuditIntegrityError("guided proposal private pipeline is not canonical") from exc


def _state_from_proposal(proposal: PipelineProposal) -> CompositionState:
    return _canonical_state_from_private_pipeline(cast(dict[str, JsonValue], deep_thaw(proposal.pipeline)))


def guided_candidate_state(proposal: PipelineProposal) -> CompositionState:
    """Restore the immutable candidate named by a durable proposal.

    Wire review inspects this candidate, never the uncommitted composition
    checkpoint and never topology reconstructed from guided dialogue.
    """

    return _state_from_proposal(proposal)


def _component_target(kind: str, stable_id: str) -> dict[str, str]:
    return {"kind": kind, "stable_id": stable_id}


def _endpoint(kind: str, stable_id: str | None = None) -> dict[str, str]:
    result = {"kind": kind}
    if stable_id is not None:
        result["stable_id"] = stable_id
    return result


def _ordered_gate_routes(node: NodeSpec) -> tuple[tuple[str, str], ...]:
    """Return the protocol-canonical direct routes followed by fork routes."""

    assert node.node_type == "gate"
    routes = sorted((node.routes or {}).items(), key=lambda route: route[0])
    return (
        *((name, destination) for name, destination in routes if destination != "fork"),
        *((name, destination) for name, destination in routes if destination == "fork"),
    )


def _node_behavior(
    node: NodeSpec,
    *,
    route_aliases: Mapping[str, str],
    branch_aliases: Mapping[str, str],
    barrier_incoming_aliases: Sequence[str] | None = None,
) -> dict[str, object]:
    if node.node_type == "transform":
        return {"kind": "transform"}
    if node.node_type == "queue":
        return {"kind": "queue"}
    if node.node_type == "aggregation":
        trigger = dict(deep_thaw(node.trigger or {}))
        # Preserve the executable scalar semantics, never free-form prose.
        trigger_kinds = [
            name for name in ("count", "timeout", "condition") if trigger.get(name if name != "timeout" else "timeout_seconds") is not None
        ]
        count = trigger.get("count")
        timeout_seconds = trigger.get("timeout_seconds")
        return {
            "kind": "aggregation",
            "trigger_kinds": trigger_kinds,
            "count": str(count) if count is not None else None,
            "timeout_seconds": float(timeout_seconds) if timeout_seconds is not None else None,
            "output_mode": node.output_mode,
            "expected_output_count": (str(node.expected_output_count) if node.expected_output_count is not None else None),
        }
    if node.node_type in ("coalesce", "row_union"):
        # A correlated barrier's branch aliases must EQUAL, in order, the branch aliases on
        # its incoming flows (validate_payload, protocol.py). Those incoming edges
        # are emitted in edge_specs order — the branch-producer node order — which
        # the planner authors nondeterministically and independently of the
        # ``branches`` dict key order. Deriving the behavior aliases from the
        # coalesce's OWN incoming edges (passed in) makes incoming == behavior
        # true by construction, instead of hoping branches.keys() order happens to
        # match producer order. Each alias is still a fork-branch-name ordinal, so
        # the fork-origin trace (line 1810) is unaffected. Fall back to
        # branches.keys() only when no incoming aliases are supplied (a degenerate
        # coalesce with no branch producers, which candidate validation rejects
        # upstream anyway).
        if barrier_incoming_aliases is not None:
            aliases = list(barrier_incoming_aliases)
        else:
            branches = node.branches
            names = list(branches.keys()) if isinstance(branches, Mapping) else list(branches or ())
            aliases = [branch_aliases[name] for name in names]
        if node.node_type == "row_union":
            return {
                "kind": "row_union",
                "branch_aliases": aliases,
                "policy": "require_all",
                "timeout_seconds": node.timeout_seconds,
            }
        return {
            "kind": "coalesce",
            "branch_aliases": aliases,
            "policy": node.policy,
            "merge": node.merge,
            "timeout_seconds": node.timeout_seconds,
        }
    assert node.node_type == "gate"
    routes = _ordered_gate_routes(node)
    route_names = [name for name, _destination in routes]
    fork_routes = [name for name, destination in routes if destination == "fork"]
    fork_to = list(node.fork_to or ())
    return {
        "kind": "gate",
        # The authored predicate travels VERBATIM: without it the review
        # surfaces show only opaque route ordinals, so an inverted or
        # fabricated condition is invisible to the operator (F11). The
        # condition is already operator-visible in the Ready YAML — this is
        # a projection fix, not a new disclosure.
        "condition": node.condition,
        "route_aliases": [route_aliases[name] for name in route_names],
        # Bijective with route_aliases IN THE SAME ORDER (both derive from
        # _ordered_gate_routes, fork routes included): each server ordinal
        # alias is bound to its author-visible route key ("true"/"false" or
        # an author label) so review surfaces can say which branch is which.
        "routes": [{"alias": route_aliases[name], "key": name} for name in route_names],
        "fork_branches": [
            {
                "routes": [route_aliases[name] for name in fork_routes],
                "branch": branch_aliases[destination],
            }
            for destination in fork_to
        ],
    }


def _projection_ids_from_payload(payload: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    nodes = payload.get("nodes")
    graph = payload.get("graph")
    if type(nodes) is not list or type(graph) is not dict or type(graph.get("edges")) is not list:
        raise AuditIntegrityError("guided proposal projection has malformed stable-id containers")
    node_ids = [node.get("stable_id") for node in nodes if type(node) is dict]
    edge_ids = [edge.get("stable_id") for edge in graph["edges"] if type(edge) is dict]
    if len(node_ids) != len(nodes) or len(edge_ids) != len(graph["edges"]):
        raise AuditIntegrityError("guided proposal projection has malformed stable IDs")
    if not all(type(i) is str and i for i in node_ids):
        raise AuditIntegrityError("guided proposal projection has malformed node stable IDs")
    if not all(type(i) is str and i for i in edge_ids):
        raise AuditIntegrityError("guided proposal projection has malformed edge stable IDs")
    return cast(list[str], node_ids), cast(list[str], edge_ids)


def _build_projection(
    *,
    proposal_id: UUID,
    proposal: PipelineProposal,
    guided: GuidedSession,
    catalog_plugin_ids: Mapping[str, frozenset[str]],
    node_stable_ids: Sequence[str] | None,
    edge_stable_ids: Sequence[str] | None,
) -> ProposePipelinePayload:
    state = _state_from_proposal(proposal)
    if [state.sources[name].plugin for name in state.sources] != [
        guided.reviewed_sources[stable_id].plugin for stable_id in guided.source_order
    ]:
        raise AuditIntegrityError("guided proposal sources differ from reviewed authority")
    if [output.plugin for output in state.outputs] != [guided.reviewed_outputs[stable_id].plugin for stable_id in guided.output_order]:
        raise AuditIntegrityError("guided proposal outputs differ from reviewed authority")
    if list(state.sources) != [guided.reviewed_sources[stable_id].name for stable_id in guided.source_order]:
        raise AuditIntegrityError("guided proposal source names differ from reviewed authority")
    if [output.name for output in state.outputs] != [guided.reviewed_outputs[stable_id].name for stable_id in guided.output_order]:
        raise AuditIntegrityError("guided proposal output names differ from reviewed authority")

    resolved_node_ids = list(node_stable_ids or (str(uuid4()) for _ in state.nodes))
    if len(resolved_node_ids) != len(state.nodes):
        raise AuditIntegrityError("guided proposal projection node stable-id count mismatch")
    node_ids = {node.id: resolved_node_ids[index] for index, node in enumerate(state.nodes)}
    source_ids = {name: guided.source_order[index] for index, name in enumerate(state.sources)}
    output_ids = {output.name: guided.output_order[index] for index, output in enumerate(state.outputs)}

    route_keys: list[tuple[str, str]] = []
    branch_names: list[str] = []
    for node in state.nodes:
        routes = _ordered_gate_routes(node) if node.node_type == "gate" else ()
        for route, _destination in routes:
            route_keys.append((node_ids[node.id], route))
        for branch in node.fork_to or ():
            if branch not in branch_names:
                branch_names.append(branch)
        raw_branches = node.branches
        # A coalesce's branch identities are its branches KEYS (the fork branch
        # names == gate ``fork_to`` destinations), not its values (the
        # connections carrying each branch's data). Aliasing by value would mint
        # a branch alias with no authoritative gate_fork origin — unsatisfiable
        # at validate_payload. The keys are already added by the gate ``fork_to``
        # above, so this only dedups; a tuple ``branches`` lists names directly.
        branch_keys = list(raw_branches.keys()) if isinstance(raw_branches, Mapping) else list(raw_branches or ())
        for branch in branch_keys:
            if branch not in branch_names:
                branch_names.append(branch)
    route_aliases = {key: proposal_structural_label("route", index) for index, key in enumerate(route_keys)}
    branch_aliases = {name: proposal_structural_label("branch", index) for index, name in enumerate(branch_names)}

    # An edge INTO a correlated barrier arrives on a branch VALUE connection but must carry
    # the branch KEY's alias — validate_payload matches a coalesce's incoming
    # branch aliases against its behavior branch_aliases (keyed by the fork branch
    # name). Map each (coalesce id, value connection) to the key's alias so
    # add_targets can stamp the branch when routing a producer into the fan-in.
    barrier_branch_alias: dict[tuple[str, str], str] = {}
    for node in state.nodes:
        if node.node_type not in ("coalesce", "row_union"):
            continue
        raw_branches = node.branches
        branch_pairs = raw_branches.items() if isinstance(raw_branches, Mapping) else ((name, name) for name in (raw_branches or ()))
        for branch_key, branch_value in branch_pairs:
            if type(branch_value) is str and branch_value and branch_key in branch_aliases:
                barrier_branch_alias[(node_ids[node.id], branch_value)] = branch_aliases[branch_key]

    def gate_route_aliases(node: NodeSpec) -> dict[str, str]:
        assert node.node_type == "gate"
        return {route: route_aliases[(node_ids[node.id], route)] for route, _destination in _ordered_gate_routes(node)}

    try:
        consumers = canonical_connection_consumers(
            state,
            node_identities=node_ids,
            output_identities=output_ids,
        )
    except ValueError as exc:  # pragma: no cover - validated state and exact IDs own this invariant
        raise AuditIntegrityError("guided proposal canonical consumer identities are malformed") from exc

    edge_specs: list[tuple[dict[str, str], dict[str, str], dict[str, object]]] = []

    # A queue's connection name is its own id (``queue_node_contract_error``
    # enforces ``input == id``), so ``canonical_connection_consumers`` lists both
    # the queue itself (input side) and its one ordinary downstream node
    # (republish side) as consumers of that connection. Those two sides must be
    # separated in the wire projection: an external producer publishing into the
    # connection reaches only the fan-in point, while the queue's own
    # ``queue_continue`` republishes to the downstream node — never back to
    # itself. Collapsing them would either self-loop the queue or fan a producer
    # straight past it (elspeth-a5b86149d4).
    queue_stable_by_connection = {node.id: node_ids[node.id] for node in state.nodes if node.node_type == "queue"}

    def add_targets(origin: dict[str, str], connection: str | None, flow: dict[str, object]) -> None:
        if connection is None:
            return
        if connection == "discard":
            edge_specs.append((origin, _endpoint("discard"), flow))
            return
        destinations = consumers.get(connection, ())
        queue_stable = queue_stable_by_connection.get(connection)
        if queue_stable is not None:
            if origin.get("stable_id") == queue_stable:
                destinations = tuple(dest for dest in destinations if dest != ("node", queue_stable))
            else:
                destinations = (("node", queue_stable),)
        if not destinations:
            raise AuditIntegrityError("guided proposal connection has no canonical consumer")
        for kind, stable_id in destinations:
            edge_flow = flow
            # An edge into a correlated barrier via one of its branch connections must
            # carry that branch's alias (validate_payload rejects a branch-less
            # flow into a coalesce). The producer emitting the flow does not know
            # its consumer is a fan-in, so stamp the alias here per destination.
            if kind == "node":
                branch_alias = barrier_branch_alias.get((stable_id, connection))
                if branch_alias is not None:
                    edge_flow = {**flow, "branch": branch_alias}
            edge_specs.append((origin, _endpoint(kind, stable_id), edge_flow))

    for source_name, source in state.sources.items():
        origin = _endpoint("source", source_ids[source_name])
        add_targets(origin, source.on_success, {"kind": "source_success", "branch": None})
        add_targets(origin, source.on_validation_failure, {"kind": "source_validation_failure"})

    for node in state.nodes:
        origin = _endpoint("node", node_ids[node.id])
        if node.node_type == "gate":
            routes = _ordered_gate_routes(node)
            node_route_aliases = gate_route_aliases(node)
            fork_routes = [name for name, destination in routes if destination == "fork"]
            for route, destination in routes:
                if destination == "fork":
                    continue
                add_targets(
                    origin,
                    destination,
                    {"kind": "gate_route", "route": node_route_aliases[route], "branch": None},
                )
            for destination in node.fork_to or ():
                add_targets(
                    origin,
                    destination,
                    {
                        "kind": "gate_fork",
                        "routes": [node_route_aliases[route] for route in fork_routes],
                        "branch": branch_aliases[destination],
                    },
                )
        elif node.node_type == "queue":
            add_targets(origin, node.id, {"kind": "queue_continue", "branch": None})
        elif node.node_type == "coalesce":
            # A coalesce publishes its merged rows under its OWN node id —
            # downstream nodes consume it via input='<coalesce id>' — and, when
            # on_success is set, ALSO direct to that sink. Republish under the
            # node id (skipped when nothing consumes it, e.g. a coalesce whose
            # only output is a direct-to-sink on_success) so the merged-row edge
            # to the downstream field_mapper is not dropped.
            if node.id in consumers:
                add_targets(origin, node.id, {"kind": "coalesce_success", "branch": None})
            add_targets(origin, node.on_success, {"kind": "coalesce_success", "branch": None})
        elif node.node_type == "row_union":
            add_targets(origin, node.on_success, {"kind": "row_union_success", "branch": None})
        else:
            add_targets(origin, node.on_success, {"kind": "node_success", "branch": None})
            add_targets(origin, node.on_error, {"kind": "node_error"})

    for output in state.outputs:
        add_targets(
            _endpoint("output", output_ids[output.name]),
            output.on_write_failure,
            {"kind": "output_write_failure"},
        )

    # Row-union release order is the authored ``branches`` mapping order, not
    # the incidental order of its producer nodes. Keep the projection's incoming
    # flows in that exact order so the public behavior and protocol validation
    # preserve the runtime N-to-N release contract.
    for node in state.nodes:
        if node.node_type != "row_union" or not isinstance(node.branches, Mapping):
            continue
        stable_id = node_ids[node.id]
        alias_rank = {branch_aliases[branch_name]: index for index, branch_name in enumerate(node.branches)}
        positions = [index for index, (_origin, destination, _flow) in enumerate(edge_specs) if destination.get("stable_id") == stable_id]
        ordered = sorted(
            (edge_specs[index] for index in positions),
            key=lambda spec: alias_rank[cast(str, spec[2]["branch"])],
        )
        for index, spec in zip(positions, ordered, strict=True):
            edge_specs[index] = spec

    resolved_edge_ids = list(edge_stable_ids or (str(uuid4()) for _ in edge_specs))
    if len(resolved_edge_ids) != len(edge_specs):
        raise AuditIntegrityError("guided proposal projection edge stable-id count mismatch")
    edges: list[dict[str, Any]] = [
        {
            "stable_id": resolved_edge_ids[index],
            "from_endpoint": origin,
            "to_endpoint": destination,
            "flow": flow,
        }
        for index, (origin, destination, flow) in enumerate(edge_specs)
    ]
    # Branch aliases carried by each correlated barrier's incoming edges, in edge_specs
    # (= wire-edge = validator ``incoming_edges``) order. A barrier's behavior
    # branch_aliases is derived from THIS so it equals its incoming flows by
    # construction, regardless of the planner's authored branch/node ordering.
    barrier_stable_ids = {node_ids[node.id] for node in state.nodes if node.node_type in ("coalesce", "row_union")}
    barrier_incoming_branch_aliases: dict[str, list[str]] = {}
    for _edge_origin, edge_destination, edge_flow in edge_specs:
        destination_id = edge_destination.get("stable_id")
        branch_alias = edge_flow.get("branch")
        if destination_id in barrier_stable_ids and isinstance(branch_alias, str) and branch_alias:
            barrier_incoming_branch_aliases.setdefault(destination_id, []).append(branch_alias)
    nodes: list[dict[str, Any]] = [
        {
            "stable_id": node_ids[node.id],
            "label": proposal_component_label("node", index),
            "node_type": node.node_type,
            "plugin": ({"kind": "transform", "id": node.plugin} if node.plugin is not None else None),
            "behavior": _node_behavior(
                node,
                route_aliases=gate_route_aliases(node) if node.node_type == "gate" else {},
                branch_aliases=branch_aliases,
                barrier_incoming_aliases=(
                    barrier_incoming_branch_aliases.get(node_ids[node.id]) if node.node_type in ("coalesce", "row_union") else None
                ),
            ),
            # Allowlisted key options as display text (R2-F3). Same closed
            # server-owned vocabulary the wire review projects, so the proposal
            # card and the wiring card describe a node identically.
            "node_options_summary": node_options_summary(node.plugin, node.options),
        }
        for index, node in enumerate(state.nodes)
    ]
    sources: list[dict[str, Any]] = [
        {
            "stable_id": source_ids[name],
            "label": proposal_component_label("source", index),
            "plugin": {"kind": "source", "id": source.plugin},
        }
        for index, (name, source) in enumerate(state.sources.items())
    ]
    outputs: list[dict[str, Any]] = [
        {
            "stable_id": output_ids[output.name],
            "label": proposal_component_label("output", index),
            "plugin": {"kind": "sink", "id": output.plugin},
        }
        for index, output in enumerate(state.outputs)
    ]
    payload = cast(
        ProposePipelinePayload,
        {
            "proposal_id": str(proposal_id),
            "draft_hash": proposal.draft_hash,
            "supersedes_draft_hash": proposal.supersedes_draft_hash,
            "summary": PROPOSAL_SUMMARY_TEMPLATE,
            "rationale": PROPOSAL_RATIONALE_TEMPLATE,
            "component_counts": {
                "sources": len(sources),
                "nodes": len(nodes),
                "edges": len(edges),
                "outputs": len(outputs),
            },
            "blockers": [],
            "graph": {"sources": sources, "edges": edges},
            "nodes": nodes,
            "outputs": outputs,
            "edit_targets": [
                *(_component_target("source", source["stable_id"]) for source in sources),
                *(_component_target("node", node["stable_id"]) for node in nodes),
                *(_component_target("edge", edge["stable_id"]) for edge in edges),
                *(_component_target("output", output["stable_id"]) for output in outputs),
            ],
        },
    )
    # The guided route's terminal-failure slog logs only exc_class, so a
    # projection AuditIntegrityError (which check of validate_payload fired, for
    # which node/edge shape) was a blind guess on a live 5xx. Emit the validator
    # error text plus a STRUCTURAL kind-summary — node ids/types/plugin-names and
    # edge flow-kinds/branch-aliases only, never options or draft content, which
    # the closed redacted projection payload does not carry anyway — so the next
    # unknown projection failure is diagnosable from the log.
    if (error := validate_payload(TurnType.PROPOSE_PIPELINE, payload)) is not None:
        slog.error(
            "composer.guided_projection_invalid",
            proposal_id=str(proposal_id),
            error=error,
            **_projection_kind_summary(payload),
        )
        raise AuditIntegrityError(f"guided proposal projection is invalid: {error}")
    if (error := validate_proposal_catalog_refs(payload, catalog_plugin_ids)) is not None:
        slog.error(
            "composer.guided_projection_catalog_binding_failed",
            proposal_id=str(proposal_id),
            error=error,
            **_projection_kind_summary(payload),
        )
        raise AuditIntegrityError(f"guided proposal catalog binding failed: {error}")
    return payload


def _projection_kind_summary(payload: Mapping[str, Any]) -> _ProjectionKindSummary:
    """Structural (Tier-3-safe) node/edge kind summary for projection failure logs.

    The PROPOSE_PIPELINE projection is already the closed, redacted wire shape —
    it carries no prompts or draft content, and no options beyond the closed
    ``_NODE_OPTION_SUMMARY_ALLOWLIST`` display pairs, only catalog plugin ids,
    node/flow kinds, and structural aliases. Project just the kinds and aliases
    (never the option summary) so a projection failure names the offending
    shape (e.g. a coalesce whose branch aliases do not match its incoming flow
    order) without touching private authored values.
    """
    nodes = payload["nodes"] if isinstance(payload.get("nodes"), list) else []
    graph = payload["graph"] if isinstance(payload.get("graph"), Mapping) else {}
    edges = graph["edges"] if isinstance(graph.get("edges"), list) else []
    node_kinds: list[_ProjectionNodeKindSummary] = [
        {
            "stable_id": node.get("stable_id"),
            "node_type": node.get("node_type"),
            "plugin": (node["plugin"].get("id") if isinstance(node.get("plugin"), Mapping) else None),
            "behavior": node["behavior"].get("kind") if isinstance(node.get("behavior"), Mapping) else None,
            "branch_aliases": (
                node["behavior"].get("branch_aliases")
                if isinstance(node.get("behavior"), Mapping) and node["behavior"].get("kind") in ("coalesce", "row_union")
                else None
            ),
        }
        for node in nodes
        if isinstance(node, Mapping)
    ]
    edge_flows: list[_ProjectionEdgeFlowSummary] = [
        {
            "from": edge["from_endpoint"].get("kind") if isinstance(edge.get("from_endpoint"), Mapping) else None,
            "to": edge["to_endpoint"].get("kind") if isinstance(edge.get("to_endpoint"), Mapping) else None,
            "flow": edge["flow"].get("kind") if isinstance(edge.get("flow"), Mapping) else None,
            "branch": edge["flow"].get("branch") if isinstance(edge.get("flow"), Mapping) else None,
        }
        for edge in edges
        if isinstance(edge, Mapping)
    ]
    return {"node_kinds": node_kinds, "edge_flows": edge_flows}


def build_guided_proposal_projection(
    *,
    proposal_id: UUID,
    proposal: PipelineProposal,
    guided: GuidedSession,
    catalog_plugin_ids: Mapping[str, frozenset[str]],
) -> ProposePipelinePayload:
    """Allocate and return one safe immutable-proposal projection."""

    return _build_projection(
        proposal_id=proposal_id,
        proposal=proposal,
        guided=guided,
        catalog_plugin_ids=catalog_plugin_ids,
        node_stable_ids=None,
        edge_stable_ids=None,
    )


def verify_guided_proposal_projection(
    *,
    payload: Mapping[str, Any],
    proposal_id: UUID,
    proposal: PipelineProposal,
    guided: GuidedSession,
    catalog_plugin_ids: Mapping[str, frozenset[str]],
) -> None:
    """Recompute all safe semantics while retaining persisted stable IDs."""

    node_ids, edge_ids = _projection_ids_from_payload(payload)
    expected = _build_projection(
        proposal_id=proposal_id,
        proposal=proposal,
        guided=guided,
        catalog_plugin_ids=catalog_plugin_ids,
        node_stable_ids=node_ids,
        edge_stable_ids=edge_ids,
    )
    if deep_thaw(payload) != expected:
        raise AuditIntegrityError("guided proposal projection differs from private proposal authority")


def verified_remaining_deferred_intents(
    *,
    guided: GuidedSession,
    proposal: PipelineProposal,
) -> tuple[DeferredStageIntent, ...]:
    """Verify mechanically covered constraints and return the exact remainder."""

    state = _state_from_proposal(proposal)
    try:
        covered_ordered = evaluate_deferred_intent_coverage(
            candidate=state,
            reviewed_guided=guided,
            claimed_intent_ids=proposal.covered_deferred_intent_ids,
        )
    except DeferredIntentClaimError as exc:
        raise AuditIntegrityError("guided proposal does not mechanically satisfy a covered deferred constraint") from exc
    covered = set(covered_ordered)
    return tuple(intent for intent in guided.deferred_intents if intent.intent_id not in covered)


__all__ = [
    "GuidedCandidateBindingRejected",
    "GuidedCorrectionTarget",
    "GuidedRevisionAuthority",
    "GuidedRevisionBindingResult",
    "bind_guided_prose_revision_candidate",
    "bind_guided_reviewed_components",
    "build_guided_proposal_projection",
    "guided_authorized_pipeline_schema",
    "guided_candidate_state",
    "guided_private_reviewed_facts",
    "guided_redacted_current_state_context",
    "guided_redacted_planner_context",
    "guided_reviewed_sink_options",
    "guided_revision_execution_hash",
    "guided_unproducible_output_field_names",
    "guided_unproducible_output_fields",
    "materialize_guided_authorized_candidate",
    "require_guided_correction_target_changed",
    "require_guided_proposal_correction_target_changed",
    "require_guided_prose_revision_successor",
    "resolve_guided_correction_target",
    "resolve_guided_proposal_correction_target",
    "verified_remaining_deferred_intents",
    "verify_guided_proposal_projection",
]
