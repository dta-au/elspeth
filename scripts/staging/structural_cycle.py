"""The structural-cycle workload for the Composer edit battery, and its verifier.

This is the large-graph stress workload a customer measured on their
deployment, carried into the battery so the same edits can be sent and
judged the same way. Each ten-step cycle edits the routing around one
*anchor* step, between its *upstream* (a source or step) and its
*downstream* (a step or sink) -- a :class:`Position`:

1. add a passthrough before the anchor, 2. one after it, 3. a second before,
4. a second after, 5. swap the before pair, 6. swap the after pair, then
7-10. remove all four, the last step also restoring the original routing
values. Cycle 1 names its nodes ``compact_*``, later cycles ``cycleN_*``.

The customer's fixture was one source, one LLM node and a sink, so their
one position was ``source -> capacity_probe -> results``. Given several
positions (from a generated fixture's manifest), cycle ``k`` edits around
position ``k`` modulo their number, so the edits land in different places of
a realistic graph.

The instruction wording is the workload that was measured and is pinned by
``tests/unit/scripts/staging/test_structural_cycle.py``; change it only with
the customer's workload.

The workload is built in code rather than shipped as a plan file: it is a
formula over the step number and three component names that the operator
supplies per run, and a plan file would have to repeat it sixty times per set
of names. The built plan is hashed like a plan file, so a resumed attempt
with other names or another step count is refused.

The verifier (:func:`verify_state` and :func:`operation_codes`) reports the
same error codes as the customer's runner, generalised from their one node to
any graph: every node, source and output the step did not name must equal
the starting graph, and a named one may differ only in the wiring the step
had to touch. Codes from the state check:

``node_inventory_mismatch``, ``original_llm_content_changed``,
``source_content_changed``, ``sink_content_changed``, ``metadata_changed``,
``routing_mismatch``, ``unexpected_auxiliary_plugin``,
``routing_does_not_reach_sink``, ``baseline_routing_not_restored``,
``runtime_validation_failed``.

Codes from the turn: ``operation_not_completed``,
``operation_result_unavailable``, ``publication_rejected``,
``no_published_reply``, ``additional_system_notice_requires_review`` and
``end_review_not_clean_and_admitted``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from scripts.staging.edit_plan import Plan
from scripts.staging.opslib import JsonObject, OperationFailed, UsageError

VERIFIER = "structural_cycle"
MAX_STEPS = 60
DEFAULT_STEPS = 30
CYCLE_LENGTH = 10
DEFAULT_NAMES = {"source": "source", "anchor": "capacity_probe", "sink": "results"}
INSTRUCTION_SUFFIX = (
    " Apply only this structural edit and its wiring. New passthrough nodes use schema.mode=observed. "
    "Preserve existing prompts, queries, files, source options and sink options. Do not run the pipeline."
)
CHECKPOINT_NOTICE_PREFIX = "Checkpoint save status:"
# A component name lands in chat text and in JSON pointers; keep it to plain identifier characters.
_COMPONENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,127}")

# The names a cycle gives the nodes it adds.
_CYCLE_NODE = re.compile(r"(compact|cycle[0-9]+)_(before|after)_[ab]")

type EndReviewEvidence = Literal["required", "if-recorded"]


@dataclass(frozen=True, slots=True)
class Position:
    """Where a cycle edits: ``upstream -> anchor -> downstream``.

    The upstream is a source or a node; the downstream a node or, with
    ``downstream_is_sink``, an output.
    """

    upstream: str
    anchor: str
    downstream: str
    downstream_is_sink: bool

    def as_json(self) -> JsonObject:
        return {
            "upstream": self.upstream,
            "anchor": self.anchor,
            "downstream": self.downstream,
            "downstream_is_sink": self.downstream_is_sink,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Position:
        return cls(value["upstream"], value["anchor"], value["downstream"], value["downstream_is_sink"] is True)


def _was(pointer: str) -> str:
    # Rendered by resolve_edits from the attempt's starting graph as JSON text, as the customer's runner does.
    return "{was:" + pointer + "}"


def planned_step(number: int, position: Position) -> tuple[str, list[str]]:
    """The instruction for step ``number`` (from 1) and the node order it must leave between upstream and downstream.

    The wording is the customer's template with the position's names put in;
    a downstream output is called "the <name> sink" where the template says
    so, a downstream node just "<name>". The last step of a cycle names the
    original routing values as ``{was:...}`` fields, filled in from the
    attempt's starting graph.
    """
    source, anchor, sink = position.upstream, position.anchor, position.downstream
    sink_phrase = f"the {sink} sink" if position.downstream_is_sink else sink
    cycle, index = divmod(number - 1, CYCLE_LENGTH)
    prefix = "compact" if cycle == 0 else f"cycle{cycle}"
    before_a, before_b = prefix + "_before_a", prefix + "_before_b"
    after_a, after_b = prefix + "_after_a", prefix + "_after_b"
    original_input = _was(f"/nodes/{anchor}/input")
    original_output = _was(f"/nodes/{anchor}/on_success")
    steps = [
        (f"Add a passthrough transform named {before_a} between {source} and {anchor}.", [before_a, anchor]),
        (f"Add a passthrough transform named {after_a} between {anchor} and {sink_phrase}.", [before_a, anchor, after_a]),
        (f"Add a passthrough transform named {before_b} between {source} and {before_a}.", [before_b, before_a, anchor, after_a]),
        (
            f"Add a passthrough transform named {after_b} between {after_a} and {sink_phrase}.",
            [before_b, before_a, anchor, after_a, after_b],
        ),
        (
            f"Swap {before_a} and {before_b} so {source} flows through {before_a}, {before_b}, then {anchor}.",
            [before_a, before_b, anchor, after_a, after_b],
        ),
        (
            f"Swap {after_a} and {after_b} so {anchor} flows through {after_b}, {after_a}, then {sink}.",
            [before_a, before_b, anchor, after_b, after_a],
        ),
        (f"Remove {before_a} and reconnect {source} directly to {before_b}.", [before_b, anchor, after_b, after_a]),
        (f"Remove {after_a} and reconnect {after_b} directly to {sink}.", [before_b, anchor, after_b]),
        (f"Remove {before_b} and reconnect {source} directly to {anchor}.", [anchor, after_b]),
        (
            f"Remove {after_b} and reconnect {anchor} directly to {sink}. Restore "
            f"{source}.on_success={original_input}, {anchor}.input={original_input}, "
            f"and {anchor}.on_success={original_output}.",
            [anchor],
        ),
    ]
    instruction, order = steps[index]
    return instruction + INSTRUCTION_SUFFIX, order


def check_positions(positions: Sequence[Position]) -> None:
    if not positions:
        raise UsageError("the workload needs at least one position")
    for position in positions:
        for label, value in (("upstream", position.upstream), ("anchor", position.anchor), ("downstream", position.downstream)):
            if not _COMPONENT_NAME.fullmatch(value):
                raise UsageError(f"position {label} must be a component name of letters, digits, _ and - (got {value!r})")
        if len({position.upstream, position.anchor, position.downstream}) != 3:
            raise UsageError("a position's upstream, anchor and downstream must be three different names")


def structural_cycle_plan(*, steps: int, positions: Sequence[Position]) -> Plan:
    """The workload as a battery plan of ``steps`` edits.

    Cycle ``k`` (ten steps) edits around ``positions[k % len(positions)]``.
    Each edit carries the node ``order`` it must leave and its ``position``.
    """
    if not 1 <= steps <= MAX_STEPS:
        raise UsageError(f"--steps must be between 1 and {MAX_STEPS}")
    check_positions(positions)
    edits: list[JsonObject] = []
    for number in range(1, steps + 1):
        position = positions[((number - 1) // CYCLE_LENGTH) % len(positions)]
        message, order = planned_step(number, position)
        edits.append({"id": f"cycle-step-{number}", "message": message, "expect": [], "order": order, "position": position.as_json()})
    verifier_args: JsonObject = {"positions": [position.as_json() for position in positions]}
    identity = json.dumps({"verifier": VERIFIER, "args": verifier_args, "edits": edits}, sort_keys=True).encode()
    return Plan(
        name=f"{VERIFIER}_{steps}",
        sha256=hashlib.sha256(identity).hexdigest(),
        forbidden_tools=(),
        targets={},
        edits=tuple(edits),
        verifier=VERIFIER,
        verifier_args=verifier_args,
    )


# ── the fixture ────────────────────────────────────────────────────────────


def _route_out(state: Mapping[str, Any], name: str) -> object:
    """The connection an upstream source or node sends its rows to (``None`` if it has none)."""
    if name in state["sources"]:
        return _field(state["sources"][name], "on_success")
    nodes = {node["id"]: node for node in state["nodes"]}
    return _field(nodes[name] if name in nodes else None, "on_success")


def _route_in(state: Mapping[str, Any], position: Position) -> object:
    """The connection the downstream takes rows from: the sink's own name, or the node's ``input``."""
    if position.downstream_is_sink:
        return position.downstream
    nodes = {node["id"]: node for node in state["nodes"]}
    return _field(nodes[position.downstream] if position.downstream in nodes else None, "input")


def check_fixture(state: Mapping[str, Any], *, positions: Sequence[Position]) -> JsonObject:
    """Refuse a graph the workload cannot run on; return its counts.

    Each position's upstream (a source or node) must route straight into its
    anchor (a transform), and the anchor straight into its downstream (a
    node, or an output when the position says so); the graph must be valid
    and hold no node that a cycle would add.
    """
    nodes = {node["id"]: node for node in state["nodes"]}
    outputs = {output["name"] for output in state["outputs"]}
    added = [name for name in nodes if _CYCLE_NODE.fullmatch(name)]
    if added:
        raise OperationFailed(f"the graph already holds nodes a cycle adds: {', '.join(sorted(added))}; start from a fresh copy")
    for position in positions:
        if position.upstream not in state["sources"] and position.upstream not in nodes:
            raise OperationFailed(f"position upstream {position.upstream} is neither a source nor a node of the graph")
        if position.anchor not in nodes or nodes[position.anchor]["node_type"] != "transform":
            raise OperationFailed(f"position anchor {position.anchor} is not a transform node of the graph")
        if (position.downstream in outputs) is not position.downstream_is_sink or (
            not position.downstream_is_sink and position.downstream not in nodes
        ):
            kind = "an output" if position.downstream_is_sink else "a node"
            raise OperationFailed(f"position downstream {position.downstream} is not {kind} of the graph")
        anchor = nodes[position.anchor]
        if _route_out(state, position.upstream) != anchor["input"] or anchor["on_success"] != _route_in(state, position):
            raise OperationFailed(f"the graph must route {position.upstream} straight through {position.anchor} to {position.downstream}")
    if state["is_valid"] is not True:
        raise OperationFailed("validate the fixture and resolve its prompt and file reviews before starting")
    llm = [node for node in state["nodes"] if node["plugin"] == "llm"]
    return {
        "sources": len(state["sources"]),
        "nodes": len(state["nodes"]),
        "llm_nodes": len(llm),
        "sinks": len(state["outputs"]),
        "prompt_queries": sum(
            len(node["options"]["queries"]) for node in llm if isinstance(_field(node["options"], "queries"), (list, dict))
        ),
    }


def graph_counts(state: Mapping[str, Any]) -> JsonObject:
    return {"sources": len(state["sources"]), "nodes": len(state["nodes"]), "sinks": len(state["outputs"])}


# ── the verifier ───────────────────────────────────────────────────────────


def _without(component: Mapping[str, Any] | None, keys: Sequence[str]) -> JsonObject | None:
    if component is None:
        return None
    return {key: value for key, value in component.items() if key not in keys}


def _field(component: Mapping[str, Any] | None, key: str) -> Any:
    return component[key] if component is not None and key in component else None


def verify_state(baseline: Mapping[str, Any], current: Mapping[str, Any], order: Sequence[str], position: Position) -> list[str]:
    """The state codes for a graph that should hold ``order`` between the position's upstream and downstream.

    The customer's verifier, generalised from one node to any graph: every
    node, source and output the step did not name must equal the starting
    graph, and a named one may differ only in the wiring the step had to
    touch (the upstream's ``on_success``, the anchor's ``input`` and
    ``on_success``, a downstream node's ``input``). A changed node is
    reported as ``original_llm_content_changed``, whichever node it is.
    Returns the codes sorted and distinct.
    """
    errors: set[str] = set()
    up, anchor, down = position.upstream, position.anchor, position.downstream
    nodes = {node["id"]: node for node in current["nodes"]}
    baseline_nodes = {node["id"]: node for node in baseline["nodes"]}
    if set(nodes) != set(baseline_nodes) | set(order):
        errors.add("node_inventory_mismatch")
    wiring = {up: ("on_success",), anchor: ("input", "on_success")}
    if not position.downstream_is_sink:
        wiring[down] = ("input",)
    for name, original in baseline_nodes.items():
        allowed = wiring[name] if name in wiring else ()
        if _without(original, allowed) != (_without(nodes[name] if name in nodes else None, allowed) or {}):
            errors.add("original_llm_content_changed")
    sources_changed = set(current["sources"]) != set(baseline["sources"])
    for name, original in baseline["sources"].items():
        allowed = ("on_success",) if name == up else ()
        if _without(original, allowed) != (_without(current["sources"][name] if name in current["sources"] else None, allowed) or {}):
            sources_changed = True
    if sources_changed:
        errors.add("source_content_changed")
    if current["outputs"] != baseline["outputs"]:
        errors.add("sink_content_changed")
    if current["metadata"] != baseline["metadata"]:
        errors.add("metadata_changed")
    route = _route_out(current, up)
    for name in order:
        node = nodes[name] if name in nodes else None
        if node is None or _field(node, "input") != route:
            errors.add("routing_mismatch")
            break
        if name != anchor and (_field(node, "plugin") != "passthrough" or _field(node, "node_type") != "transform"):
            errors.add("unexpected_auxiliary_plugin")
        route = _field(node, "on_success")
    if route != _route_in(current, position):
        errors.add("routing_does_not_reach_sink")
    if list(order) == [anchor] and (
        _route_out(current, up) != _route_out(baseline, up)
        or _field(nodes[anchor] if anchor in nodes else None, "input") != baseline_nodes[anchor]["input"]
        or _field(nodes[anchor] if anchor in nodes else None, "on_success") != baseline_nodes[anchor]["on_success"]
        or _route_in(current, position) != _route_in(baseline, position)
    ):
        errors.add("baseline_routing_not_restored")
    if current["is_valid"] is not True:
        errors.add("runtime_validation_failed")
    return sorted(errors)


def operation_codes(
    operation: Mapping[str, Any], end_reviews: Sequence[Mapping[str, Any]], *, end_review_evidence: EndReviewEvidence
) -> tuple[list[str], list[str]]:
    """``(codes, warnings)`` for the finished turn: its status, its published reply and its last end review.

    With ``end_review_evidence="if-recorded"`` a turn whose end review was not
    returned by the server is a warning (``end_review_not_recorded``), not a
    failure; a recorded review must still be clean and admitted.
    """
    codes: set[str] = set()
    warnings: list[str] = []
    if operation["status"] != "completed":
        codes.add("operation_not_completed")
    if not end_reviews:
        if end_review_evidence == "required":
            codes.add("end_review_not_clean_and_admitted")
        else:
            warnings.append("end_review_not_recorded")
    elif end_reviews[-1]["verdict"] != "clean" or end_reviews[-1]["publication_verdict"] != "ADMIT":
        codes.add("end_review_not_clean_and_admitted")
    result = operation["result"] if "result" in operation else None
    if type(result) is not dict:
        codes.add("operation_result_unavailable")
        return sorted(codes), warnings
    message = result["message"] if "message" in result and type(result["message"]) is dict else {}
    if "rejection" in message and message["rejection"] is not None:
        codes.add("publication_rejected")
    segments = message["segments"] if "segments" in message and type(message["segments"]) is list else []

    def text_of(segment: object, kind: str) -> str | None:
        if type(segment) is not dict or "kind" not in segment or segment["kind"] != kind:
            return None
        content = segment["content"] if "content" in segment else ""
        return content if type(content) is str else ""

    if not any((text_of(segment, "text") or "").strip() for segment in segments):
        codes.add("no_published_reply")
    notices = [text_of(segment, "trusted_system_notice") for segment in segments]
    if any(notice is not None and not notice.startswith(CHECKPOINT_NOTICE_PREFIX) for notice in notices):
        codes.add("additional_system_notice_requires_review")
    return sorted(codes), warnings
