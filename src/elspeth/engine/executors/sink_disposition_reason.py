"""Prove readable Boolean routing reasons without inventing error outcomes."""

from __future__ import annotations

import json
from collections.abc import Mapping

from elspeth.contracts.audit import NodeStateCompleted
from elspeth.contracts.enums import NodeType, RoutingMode, TerminalOutcome, TerminalPath
from elspeth.contracts.errors import SinkEffectCapabilityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.contracts.secret_scrub import REDACTED_SECRET_TEXT, scrub_text_for_audit
from elspeth.contracts.sink_effects import SinkEffectErrorReason, SinkEffectGateRouteReason
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.scheduler.payload_codec import deserialize_row_payload, scrubbed_row_payload_json
from elspeth.engine.executors.sink_error_reason import resolve_sink_error_reason


def resolve_sink_disposition_reason(
    factory: RecorderFactory,
    *,
    run_id: str,
    token_id: str,
    row_id: str,
    sink_node_id: str,
    sink_name: str | None,
    outcome: TerminalOutcome,
    path: TerminalPath,
    error_hash: str | None,
    row: Mapping[str, object],
) -> SinkEffectErrorReason | SinkEffectGateRouteReason:
    """Resolve only actual failures or unique direct Boolean gate-to-sink moves.

    The scheduler's node cursor predates the final route and is not evidence
    of the selecting gate. Completed state, immutable run graph, routing event
    and reason payload must agree. Ambiguous retries refuse rather than guess.
    """
    if outcome is TerminalOutcome.FAILURE:
        return resolve_sink_error_reason(
            factory,
            run_id=run_id,
            token_id=token_id,
            row_id=row_id,
            sink_node_id=sink_node_id,
            sink_name=sink_name,
            outcome=outcome,
            path=path,
            error_hash=error_hash,
        )
    if (outcome, path) != (TerminalOutcome.SUCCESS, TerminalPath.GATE_ROUTED) or error_hash is not None:
        raise SinkEffectCapabilityError("Readable CSV disposition retention requires a durable failure or Boolean gate route")
    items = factory.query.get_sink_error_handoffs(run_id=run_id, token_id=token_id, row_id=row_id)
    if len(items) != 1:
        raise LandscapeRecordError("Readable CSV gate retention requires one exact durable sink handoff")
    item = items[0]
    if (
        item.run_id != run_id
        or item.token_id != token_id
        or item.row_id != row_id
        or not item.pending_sink_name
        or (sink_name is not None and item.pending_sink_name != sink_name)
        or item.pending_outcome != outcome.value
        or item.pending_path != path.value
        or item.pending_error_hash is not None
        or item.pending_error_message is not None
        or item.status not in (TokenWorkStatus.PENDING_SINK, TokenWorkStatus.LEASED, TokenWorkStatus.TERMINAL)
    ):
        raise LandscapeRecordError("Readable CSV gate retention handoff disagrees with its routing identity")
    row_hash = stable_hash(row)
    if item.status is TokenWorkStatus.TERMINAL:
        # Ordinary terminal transitions use the work-item anchor; pending-sink
        # completion and repair use the token anchor. Both are owned purge
        # representations, not substitutes for the gate's full row binding.
        if item.row_payload_json not in (scrubbed_row_payload_json(item.work_item_id), scrubbed_row_payload_json(item.token_id)):
            raise LandscapeRecordError("Readable CSV gate retention terminal handoff has divergent payload custody")
    elif stable_hash(deserialize_row_payload(item.row_payload_json).to_dict()) != row_hash:
        raise LandscapeRecordError("Readable CSV gate retention handoff carries a different row")
    source_row = factory.query.get_row(row_id)
    tokens = factory.query.get_tokens(row_id)
    if source_row is None or source_row.run_id != run_id or len([token for token in tokens if token.token_id == token_id]) != 1:
        raise LandscapeRecordError("Readable CSV gate retention lacks its exact source row and token")
    sink = factory.data_flow.get_node(sink_node_id, run_id)
    if sink is None or sink.run_id != run_id or sink.node_type is not NodeType.SINK:
        raise LandscapeRecordError("Readable CSV gate retention requires its exact registered sink")
    candidates = []
    for state in factory.query.get_node_states_for_token(token_id):
        if state.token_id != token_id:
            raise LandscapeRecordError("Readable CSV gate retention state belongs to another token")
        node = factory.data_flow.get_node(state.node_id, run_id)
        if node is None or node.run_id != run_id:
            raise LandscapeRecordError("Readable CSV gate retention state lacks its registered run node")
        if node.node_type is not NodeType.GATE:
            continue
        for event in factory.query.get_routing_events(state.state_id):
            edge = factory.data_flow.get_edge(event.edge_id)
            if edge.run_id != run_id or edge.from_node_id != node.node_id or event.state_id != state.state_id:
                raise LandscapeRecordError("Readable CSV gate retention event disagrees with its registered edge")
            if edge.to_node_id == sink_node_id:
                candidates.append((state, node, event, edge))
    if len(candidates) != 1:
        raise LandscapeRecordError("Readable CSV gate retention requires one unique selecting gate decision")
    state, node, event, edge = candidates[0]
    if (
        type(state) is not NodeStateCompleted
        or event.mode is not RoutingMode.MOVE
        or edge.default_mode is not RoutingMode.MOVE
        or edge.label not in ("true", "false")
        or state.input_hash != row_hash
        or state.output_hash != row_hash
        or state.context_after_json is None
    ):
        raise LandscapeRecordError("Readable CSV gate retention requires an unchanged row and completed Boolean move")
    context = json.loads(state.context_after_json)
    config = json.loads(node.config_json)
    if (
        type(context) is not dict
        or set(context) != {"condition", "result", "route_label"}
        or context["route_label"] != edge.label
        or context["result"] != ("True" if edge.label == "true" else "False")
        or type(config) is not dict
        or "condition" not in config
        or "routes" not in config
        or type(config["routes"]) is not dict
        or edge.label not in config["routes"]
        or config["routes"][edge.label] != item.pending_sink_name
        or context["condition"] != config["condition"]
        or stable_hash(config) != node.config_hash
        or not node.plugin_name.startswith("config_gate:")
    ):
        raise LandscapeRecordError("Readable CSV gate retention context disagrees with its Boolean route configuration")
    reason = {"condition": context["condition"], "result": edge.label}
    reason_bytes = canonical_json(reason).encode("utf-8")
    if event.reason_hash != stable_hash(reason) or event.reason_ref != event.reason_hash or factory.payload_store is None:
        raise LandscapeRecordError("Readable CSV gate retention lacks its exact durable routing reason")
    if factory.payload_store.retrieve(event.reason_ref) != reason_bytes:
        raise LandscapeRecordError("Readable CSV gate retention routing reason payload is divergent")
    gate_name = node.plugin_name.removeprefix("config_gate:")
    destination = item.pending_sink_name
    for name in (gate_name, destination):
        if (
            not name.strip()
            or len(name) > 256
            or any(ord(character) < 32 for character in name)
            or scrub_text_for_audit(name) != name
            or REDACTED_SECRET_TEXT in name
        ):
            raise SinkEffectCapabilityError("Readable CSV gate retention configured names are unavailable for disclosure")
    witness_hash = stable_hash(
        {
            "schema": "sink-boolean-gate-handoff-v1",
            "work_item_id": item.work_item_id,
            "run_id": run_id,
            "token_id": token_id,
            "row_id": row_id,
            "sink_node_id": sink_node_id,
            "sink_name": destination,
            "outcome": outcome.value,
            "path": path.value,
            "row_hash": row_hash,
            "state_id": state.state_id,
            "node_id": node.node_id,
            "config_hash": node.config_hash,
            "context": context,
            "event_id": event.event_id,
            "routing_group_id": event.routing_group_id,
            "ordinal": event.ordinal,
            "edge_id": edge.edge_id,
            "reason_hash": event.reason_hash,
        }
    )
    return SinkEffectGateRouteReason(
        text=f"Gate {gate_name} selected {edge.label} and routed this row to {destination}.",
        witness_id=item.work_item_id,
        witness_hash=witness_hash,
    )
