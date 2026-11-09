"""Bounded private facts, independent of dispatch and provider admission."""

from __future__ import annotations

import errno
from dataclasses import replace
from types import GetSetDescriptorType, MemberDescriptorType
from typing import cast

from pydantic import JsonValue
from rfc8785 import CanonicalizationError

from elspeth.contracts.composer_audit import PrivateFailureFacts, PrivateReceivedDiagnostic
from elspeth.contracts.credential_material import REDACTED_CREDENTIAL_TEXT, find_credential_material
from elspeth.core.canonical import canonical_json
from elspeth.web.composer.diagnostic_evidence import _decode


def _safe_text(value: object, limit: int) -> str | None:
    if type(value) is not str or len(value) > limit:
        return None
    try:
        if len(value.encode("utf-8")) > limit:
            return None
    except UnicodeError:
        return None
    return REDACTED_CREDENTIAL_TEXT if find_credential_material(value) is not None else value


def capture_failure_facts(exc: BaseException) -> PrivateFailureFacts:
    """Capture a bounded private graph from builtin exception storage only.

    Node relation is first discovery; explicit edges describe all captured
    relationships. Suppressed context and group membership are provenance,
    not a selection of the operative cause. Reserve topology before text.
    """
    nodes: list[JsonValue] = []
    edges: list[JsonValue] = []
    seen: dict[int, int] = {}
    active: set[int] = set()
    reasons: set[str] = set()
    text_slots: list[tuple[dict[str, JsonValue], str]] = []
    omitted_edges = 0

    def visit(current: BaseException, relation: str, depth: int) -> None:
        nonlocal omitted_edges
        node_id = len(nodes)
        seen[id(current)] = node_id
        active.add(id(current))
        name = type.__dict__["__name__"].__get__(type(current))
        safe_name = _safe_text(name, 128) or "unavailable"
        if len(canonical_json(safe_name).encode("utf-8")) > 160:
            safe_name = "unavailable"
        arguments: list[JsonValue] = []
        node: dict[str, JsonValue] = {
            "id": node_id,
            "relation": relation,
            "class": safe_name,
            "arguments": arguments,
            "suppressed_context": BaseException.__dict__["__suppress_context__"].__get__(current),
        }
        args = BaseException.__dict__["args"].__get__(current)
        if type(args) is tuple:
            for argument in args[:4]:
                safe = _safe_text(argument, 2048)
                # Filter the whole bounded original before fitting any text.
                disposition = (
                    "unavailable" if safe is None else ("filtered" if find_credential_material(argument) is not None else "retained")
                )
                slot: dict[str, JsonValue] = {"disposition": disposition, "text": None}
                arguments.append(slot)
                if safe is not None:
                    slot["text_omitted"] = True
                    text_slots.append((slot, safe))
            node["omitted_arguments"] = max(0, len(args) - 4)
        if any(base is OSError for base in type.__dict__["__mro__"].__get__(type(current))[:16]):
            number = OSError.__dict__["errno"].__get__(current)
            if type(number) is int and number in errno.errorcode:
                node["errno"] = number
                node["errno_name"] = errno.errorcode[number]
        nodes.append(node)
        cause = BaseException.__dict__["__cause__"].__get__(current)
        context = BaseException.__dict__["__context__"].__get__(current)
        candidates: list[tuple[str, BaseException, int | None]] = []
        if cause is not None:
            candidates.append(("cause", cause, None))
        if context is not None:
            candidates.append(("context", context, None))
        try:
            members = BaseExceptionGroup.__dict__["exceptions"].__get__(current)
        except TypeError:
            # The builtin descriptor refuses nongroups without subclass hooks.
            members = ()
        if members:
            node["omitted_members"] = max(0, len(members) - 8)
            if len(members) > 8:
                reasons.add("group_member_limit")
            candidates.extend(("group_member", member, index) for index, member in enumerate(members[:8]))
        for index, (edge_relation, target, member_index) in enumerate(candidates):
            if len(edges) == 16:
                omitted: dict[str, JsonValue] = {
                    "cause": sum(item[0] == "cause" for item in candidates[index:]),
                    "context": sum(item[0] == "context" for item in candidates[index:]),
                    "group_member": sum(item[0] == "group_member" for item in candidates[index:]),
                }
                node["omitted_relations"] = omitted
                omitted_edges += len(candidates) - index
                reasons.add("edge_limit")
                break
            edge: dict[str, JsonValue] = {"from": node_id, "to": None, "relation": edge_relation}
            if member_index is not None:
                edge["member_index"] = member_index
            target_identity = id(target)
            # Existing targets require neither a new node nor deeper traversal.
            if target_identity in seen:
                edge["to"] = seen[target_identity]
                edge["disposition"] = "cycle" if target_identity in active else "shared"
                if target_identity in active:
                    reasons.add("cycle")
                edges.append(edge)
            elif depth == 4:
                edge["disposition"] = "depth_limit"
                reasons.add("depth_limit")
                edges.append(edge)
            elif len(nodes) == 8:
                edge["disposition"] = "node_limit"
                reasons.add("node_limit")
                edges.append(edge)
            else:
                edge["to"] = len(nodes)
                edge["disposition"] = "captured"
                edges.append(edge)
                visit(target, edge_relation, depth + 1)
        active.remove(id(current))

    visit(exc, "exception", 0)
    if "node_limit" in reasons or "depth_limit" in reasons:
        termination = "node_limit"
    elif "edge_limit" in reasons:
        termination = "edge_limit"
    elif "group_member_limit" in reasons:
        termination = "group_member_limit"
    else:
        termination = "cycle" if "cycle" in reasons else "complete"
    facts: dict[str, JsonValue] = {
        "record_type": "failure_facts",
        "nodes": nodes,
        "edges": edges,
        "termination": termination,
        "termination_reasons": cast("list[JsonValue]", sorted(reasons)),
        "omitted_edges": omitted_edges,
        "trimmed_arguments": len(text_slots),
    }
    trimmed = len(text_slots)
    for slot, text in text_slots:
        slot["text"] = text
        del slot["text_omitted"]
        facts["trimmed_arguments"] = trimmed - 1
        if len(canonical_json(facts).encode("utf-8")) <= 8192:
            trimmed -= 1
        else:
            slot["text"] = None
            slot["text_omitted"] = True
            facts["trimmed_arguments"] = trimmed
    return PrivateFailureFacts(canonical_json(facts))


def _snapshot(value: object) -> dict[object, object] | None:
    """Admit exact dictionaries or a builtin-owned instance dictionary slot."""
    if type(value) is dict:
        return cast(dict[object, object], value)
    cls = type(value)
    lineage = type.__dict__["__mro__"].__get__(cls)
    for base in lineage[:16]:
        fields = type.__dict__["__dict__"].__get__(base)
        if len(fields) > 256:
            return None
        for key, descriptor in fields.items():
            if type(key) is not str or key != "__dict__":
                continue
            if type(descriptor) is not GetSetDescriptorType and type(descriptor) is not MemberDescriptorType:
                return None
            try:
                captured = descriptor.__get__(value, cls)
            except (TypeError, AttributeError):
                return None
            return cast(dict[object, object], captured) if type(captured) is dict else None
    return None


def _field(value: object, name: str) -> object:
    fields = _snapshot(value)
    if fields is None or len(fields) > 64:
        return None
    for key, item in fields.items():
        if type(key) is str and key == name:
            return item
    return None


def capture_received_response(
    response: object, *, namespace: str, ordinal: int, known_tools: frozenset[str]
) -> tuple[PrivateReceivedDiagnostic, ...]:
    """Capture at most 32 slots and 1MiB argument input without semantic admission."""
    choices = _field(response, "choices")
    calls = None
    shape = "unavailable_envelope"
    if (type(choices) is list or type(choices) is tuple) and len(cast("list[object] | tuple[object, ...]", choices)) == 1:
        calls = _field(_field(cast("list[object] | tuple[object, ...]", choices)[0], "message"), "tool_calls")
        shape = ("present_calls" if calls else "empty_calls") if type(calls) is list or type(calls) is tuple else "unavailable_calls"
    if type(calls) is not list and type(calls) is not tuple:
        calls = ()
    admitted_calls = cast("list[object] | tuple[object, ...]", calls)
    remaining = 1024 * 1024
    canonical_remaining = 1024 * 1024
    records: list[PrivateReceivedDiagnostic] = []
    for index, call in enumerate(admitted_calls[:32]):
        function = _field(call, "function")
        name = _safe_text(_field(function, "name"), 128)
        known_name = name if name in known_tools else "unknown_tool"
        provider_id = _safe_text(_field(call, "id"), 256)
        raw = _field(function, "arguments")
        canonical = None
        reason = "unsupported"
        if type(raw) is str and len(raw) <= remaining:
            try:
                size = len(raw.encode("utf-8"))
            except UnicodeError:
                raw = None
                size = remaining + 1
            if size <= remaining:
                remaining -= size
                try:
                    assert raw is not None
                    decoded = _decode(raw)
                    if known_name == "set_pipeline" and type(decoded) is dict and set(decoded) == {"pipeline"}:
                        decoded = decoded["pipeline"]
                    canonical = canonical_json(decoded)
                    reason = "decoded"
                    canonical_size = len(canonical.encode("utf-8"))
                    if canonical_size > canonical_remaining:
                        canonical = None
                        raw = None
                        reason = "canonical_bounds"
                    else:
                        canonical_remaining -= canonical_size
                except (ValueError, TypeError, CanonicalizationError):
                    reason = "invalid_json"
            else:
                raw = None
                reason = "aggregate_bounds"
        else:
            raw = None
            reason = "aggregate_bounds" if type(_field(function, "arguments")) is str else "unsupported"
        identity = f"planner-response:{namespace}:{ordinal}:{index}"
        facts = {
            "record_type": "received_tool_arguments",
            "planner_call_ordinal": ordinal,
            "slot_index": index,
            "physical_call_id": None,
            "provider_tool_call_id": provider_id,
            "provider_tool_name": name,
            "provider_tool_name_disposition": "unavailable"
            if name is None
            else ("filtered" if name == REDACTED_CREDENTIAL_TEXT else "retained"),
            "provider_tool_call_id_disposition": "unavailable"
            if provider_id is None
            else ("filtered" if provider_id == REDACTED_CREDENTIAL_TEXT else "retained"),
            "argument_disposition": reason,
            "disposition": "received_not_admitted",
            "refusal_code": "not_classified",
        }
        records.append(PrivateReceivedDiagnostic(identity, identity, known_name, canonical, canonical_json(facts), raw))
    if len(admitted_calls) > 32 or not admitted_calls:
        identity = f"planner-response:{namespace}:{ordinal}:summary"
        records.append(
            PrivateReceivedDiagnostic(
                identity,
                identity,
                "composer_response_summary",
                None,
                canonical_json(
                    {"record_type": "received_response_summary", "omitted_slots": max(0, len(admitted_calls) - 32), "shape": shape}
                ),
                None,
            )
        )
    return tuple(records)


def bind_received_response(
    records: tuple[PrivateReceivedDiagnostic, ...], *, physical_call_id: str | None, refusal_code: str | None
) -> tuple[PrivateReceivedDiagnostic, ...]:
    bound = []
    for record in records:
        facts = _decode(record.result_canonical)
        assert type(facts) is dict
        facts["physical_call_id"] = physical_call_id
        facts["disposition"] = "received_not_admitted" if refusal_code is not None else "admitted_receipt"
        facts["refusal_code"] = refusal_code
        bound.append(replace(record, result_canonical=canonical_json(facts)))
    return tuple(bound)
