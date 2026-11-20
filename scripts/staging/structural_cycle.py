"""The structural-cycle workload for the Composer edit battery, and its verifier.

This is the large-graph stress workload a customer measured on their deployment,
carried into the battery so the same edits can be sent and judged the same
way. The fixture is one source, one LLM node (the *anchor*) and a main-flow
sink; the size comes from the LLM node's prompt and query configuration.
Each ten-step cycle edits the routing around the anchor only:

1. add a passthrough before the anchor, 2. one after it, 3. a second before,
4. a second after, 5. swap the before pair, 6. swap the after pair, then
7-10. remove all four, the last step also restoring the original routing
values. Cycle 1 names its nodes ``compact_*``, later cycles ``cycleN_*``.

The instruction wording is the workload that was measured and is pinned by
``tests/unit/scripts/staging/test_structural_cycle.py``; change it only with
the customer's workload.

The workload is built in code rather than shipped as a plan file: it is a
formula over the step number and three component names that the operator
supplies per run, and a plan file would have to repeat it sixty times per set
of names. The built plan is hashed like a plan file, so a resumed attempt
with other names or another step count is refused.

The verifier (:func:`verify_state` and :func:`operation_codes`) reports the
same error codes as the customer's runner. Codes from the state check:

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

type EndReviewEvidence = Literal["required", "if-recorded"]


def _was(pointer: str) -> str:
    # Rendered by resolve_edits from the attempt's starting graph as JSON text, as the customer's runner does.
    return "{was:" + pointer + "}"


def planned_step(number: int, source: str, anchor: str, sink: str) -> tuple[str, list[str]]:
    """The instruction for step ``number`` (from 1) and the node order it must leave between source and sink.

    The last step of a cycle names the original routing values as
    ``{was:...}`` fields, filled in from the attempt's starting graph.
    """
    cycle, index = divmod(number - 1, CYCLE_LENGTH)
    prefix = "compact" if cycle == 0 else f"cycle{cycle}"
    before_a, before_b = prefix + "_before_a", prefix + "_before_b"
    after_a, after_b = prefix + "_after_a", prefix + "_after_b"
    original_input = _was(f"/nodes/{anchor}/input")
    original_output = _was(f"/nodes/{anchor}/on_success")
    steps = [
        (f"Add a passthrough transform named {before_a} between {source} and {anchor}.", [before_a, anchor]),
        (f"Add a passthrough transform named {after_a} between {anchor} and the {sink} sink.", [before_a, anchor, after_a]),
        (f"Add a passthrough transform named {before_b} between {source} and {before_a}.", [before_b, before_a, anchor, after_a]),
        (
            f"Add a passthrough transform named {after_b} between {after_a} and the {sink} sink.",
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


def check_names(source: str, anchor: str, sink: str) -> None:
    for flag, value in (("--source", source), ("--anchor", anchor), ("--sink", sink)):
        if not _COMPONENT_NAME.fullmatch(value):
            raise UsageError(f"{flag} must be a component name of letters, digits, _ and - (got {value!r})")
    if len({source, anchor, sink}) != 3:
        raise UsageError("--source, --anchor and --sink must be three different names")


def structural_cycle_plan(*, steps: int, source: str, anchor: str, sink: str) -> Plan:
    """The workload as a battery plan of ``steps`` edits; each edit carries the node ``order`` it must leave."""
    if not 1 <= steps <= MAX_STEPS:
        raise UsageError(f"--steps must be between 1 and {MAX_STEPS}")
    check_names(source, anchor, sink)
    edits: list[JsonObject] = []
    for number in range(1, steps + 1):
        message, order = planned_step(number, source, anchor, sink)
        edits.append({"id": f"cycle-step-{number}", "message": message, "expect": [], "order": order})
    verifier_args = {"source": source, "anchor": anchor, "sink": sink}
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


def check_fixture(state: Mapping[str, Any], *, source: str, anchor: str, sink: str) -> JsonObject:
    """Refuse a graph that is not the workload's fixture; return its counts.

    One LLM node with the anchor id, exactly the named source, the named sink
    among the outputs, a valid graph, and the source routed straight through
    the LLM node to the sink.
    """
    nodes = state["nodes"]
    if len(nodes) != 1 or nodes[0]["id"] != anchor or nodes[0]["plugin"] != "llm":
        raise OperationFailed(f"the fixture must have exactly one node: an LLM node with the id {anchor} (--anchor)")
    if set(state["sources"]) != {source} or not any(output["name"] == sink for output in state["outputs"]):
        raise OperationFailed(f"the fixture must have the one source {source} (--source) and the sink {sink} (--sink)")
    if state["is_valid"] is not True:
        raise OperationFailed("validate the fixture and resolve its prompt and file reviews before starting")
    if state["sources"][source]["on_success"] != nodes[0]["input"] or nodes[0]["on_success"] != sink:
        raise OperationFailed(f"the fixture must route {source} straight through {anchor} to {sink}")
    queries = nodes[0]["options"]["queries"] if "queries" in nodes[0]["options"] else None
    return {
        "sources": len(state["sources"]),
        "nodes": len(nodes),
        "sinks": len(state["outputs"]),
        "prompt_queries": len(queries) if isinstance(queries, (list, dict)) else 0,
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


def verify_state(
    baseline: Mapping[str, Any], current: Mapping[str, Any], order: Sequence[str], *, source: str, anchor: str, sink: str
) -> list[str]:
    """The state codes for a graph that should hold ``order`` between source and sink (sorted, distinct)."""
    errors: set[str] = set()
    nodes = {node["id"]: node for node in current["nodes"]}
    if set(nodes) != set(order):
        errors.add("node_inventory_mismatch")
    original_anchor = baseline["nodes"][0]
    actual_anchor = nodes[anchor] if anchor in nodes else None
    if _without(original_anchor, ("input", "on_success")) != (_without(actual_anchor, ("input", "on_success")) or {}):
        errors.add("original_llm_content_changed")
    actual_source = current["sources"][source] if source in current["sources"] else None
    if _without(baseline["sources"][source], ("on_success",)) != (_without(actual_source, ("on_success",)) or {}) or set(
        current["sources"]
    ) != {source}:
        errors.add("source_content_changed")
    if current["outputs"] != baseline["outputs"]:
        errors.add("sink_content_changed")
    if current["metadata"] != baseline["metadata"]:
        errors.add("metadata_changed")
    route = _field(actual_source, "on_success")
    for name in order:
        node = nodes[name] if name in nodes else None
        if node is None or _field(node, "input") != route:
            errors.add("routing_mismatch")
            break
        if name != anchor and (_field(node, "plugin") != "passthrough" or _field(node, "node_type") != "transform"):
            errors.add("unexpected_auxiliary_plugin")
        route = _field(node, "on_success")
    if route != sink:
        errors.add("routing_does_not_reach_sink")
    if list(order) == [anchor] and (
        _field(actual_source, "on_success") != baseline["sources"][source]["on_success"]
        or _field(actual_anchor, "input") != original_anchor["input"]
        or _field(actual_anchor, "on_success") != original_anchor["on_success"]
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
