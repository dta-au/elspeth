from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal

from pydantic import ConfigDict, TypeAdapter, ValidationError
from pydantic.dataclasses import dataclass

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.advisor_output import (
    ContinuationAdvisorDecision,
    ContinuationEvidenceRef,
    PrescribedContinuationAction,
    ReadContinuationAction,
    ReviewContinuationAction,
)

if TYPE_CHECKING:
    from elspeth.web.composer.continuation import ContinuationActionSource, ContinuationFrameStack
    from elspeth.web.composer.continuation_codec import PrivateChunk


class ContinuationProtocolError(ValueError):
    pass


_ACTION_CODEC: TypeAdapter[PrescribedContinuationAction] = TypeAdapter(PrescribedContinuationAction)


def decode_prescribed_continuation_action(encoded: str) -> PrescribedContinuationAction:
    from elspeth.contracts.hashing import canonical_json

    parsed, error = parse_json_strict(encoded)
    if error is not None:
        raise AuditIntegrityError("prescribed continuation action is not strict JSON")
    try:
        return _ACTION_CODEC.validate_json(canonical_json(parsed), strict=True)
    except ValidationError as exc:
        raise AuditIntegrityError("prescribed continuation action violates its owned codec") from exc


@dataclass(frozen=True, slots=True, config=ConfigDict(extra="forbid", strict=True))
class ContinuationObligation:
    obligation_id: str
    parent_id: str | None
    description: str
    original_message_id: str
    start: int
    end: int
    status: Literal["open", "blocked", "satisfied"] = "open"


@dataclass(frozen=True, slots=True, config=ConfigDict(extra="forbid", strict=True))
class ActionExecutionReceipt:
    action_key: str
    owner_receipt_id: str
    disposition: Literal["prepared", "dispatched", "committed", "refused", "unknown"]


@dataclass(frozen=True, slots=True, config=ConfigDict(extra="forbid", strict=True))
class ContinuationFrontier:
    obligations: tuple[ContinuationObligation, ...]
    evidence: tuple[ContinuationEvidenceRef, ...]
    pending_review_bindings: tuple[str, ...]
    action_receipts: tuple[ActionExecutionReceipt, ...]
    graph_hash: str


_FRONTIER_CODEC = TypeAdapter(ContinuationFrontier)


def encode_continuation_frontier(frontier: ContinuationFrontier) -> str:
    from elspeth.contracts.hashing import canonical_json

    if type(frontier) is not ContinuationFrontier:
        raise TypeError("frontier codec requires its owned product value")
    return canonical_json(_FRONTIER_CODEC.dump_python(frontier, mode="json", warnings="error"))


def decode_continuation_frontier(encoded: str) -> ContinuationFrontier:
    from elspeth.contracts.hashing import canonical_json

    parsed, error = parse_json_strict(encoded)
    if error is not None:
        raise AuditIntegrityError("continuation frontier is not strict JSON")
    try:
        return _FRONTIER_CODEC.validate_json(canonical_json(parsed), strict=True)
    except ValidationError as exc:
        raise AuditIntegrityError("continuation frontier violates its owned codec") from exc


def initial_continuation_frontier(
    *,
    request: str,
    original_message_id: str,
    graph_hash: str,
) -> ContinuationFrontier:
    if not request or not original_message_id:
        raise ContinuationProtocolError("continuation needs its complete original request")
    root = ContinuationObligation(
        "request",
        None,
        request,
        original_message_id,
        0,
        len(request),
    )
    return ContinuationFrontier((root,), (), (), (), graph_hash)


def canonicalize_continuation_obligations(
    decision: ContinuationAdvisorDecision,
    frontier: ContinuationFrontier,
) -> ContinuationAdvisorDecision:
    known = {item.obligation_id: item for item in frontier.obligations}
    proposals = {item.obligation_id: item for item in decision.obligations}
    if len(proposals) != len(decision.obligations):
        raise ContinuationProtocolError("Advisor repeated an obligation reference")
    aliases: dict[str, str] = {}
    visiting: set[str] = set()

    def resolve(reference: str) -> str:
        if reference in known:
            return reference
        if reference in aliases:
            return aliases[reference]
        if reference not in proposals:
            return reference
        if reference in visiting:
            raise ContinuationProtocolError("obligation parentage is cyclic")
        visiting.add(reference)
        proposal = proposals[reference]
        if proposal.parent_id is None:
            raise ContinuationProtocolError("new obligation lacks its existing request parent")
        parent = resolve(proposal.parent_id)
        canonical = "obligation:" + stable_hash(
            {
                "original_message_id": proposal.original_message_id,
                "start": proposal.start,
                "end": proposal.end,
                "parent": parent,
            }
        )
        visiting.remove(reference)
        aliases[reference] = canonical
        return canonical

    normalized = tuple(
        item.model_copy(
            update={
                "obligation_id": resolve(item.obligation_id),
                "parent_id": None if item.parent_id is None else resolve(item.parent_id),
            }
        )
        for item in decision.obligations
    )
    if len({item.obligation_id for item in normalized}) != len(normalized):
        raise ContinuationProtocolError("multiple obligation labels name the same immutable source span")
    action = decision.prescribed_action
    if action is not None:
        resolved = tuple(resolve(key) for key in action.obligation_ids)
        if len(set(resolved)) != len(resolved):
            raise ContinuationProtocolError("Advisor repeated an action obligation reference")
        action = action.model_copy(update={"obligation_ids": resolved})
    return decision.model_copy(
        update={
            "obligations": normalized,
            "obligation_ids": tuple(resolve(key) for key in decision.obligation_ids),
            "remaining_actionable_ids": tuple(resolve(key) for key in decision.remaining_actionable_ids),
            "prescribed_action": action,
        }
    )


def proposed_obligations(
    decision: ContinuationAdvisorDecision,
    frontier: ContinuationFrontier,
) -> tuple[ContinuationObligation, ...]:
    current = {item.obligation_id: item for item in frontier.obligations}
    if len(current) != len(frontier.obligations) or "request" not in current:
        raise ContinuationProtocolError("frontier lacks a unique whole-request obligation")
    root = current["request"]
    updates = {item.obligation_id: item for item in decision.obligations}
    if len(updates) != len(decision.obligations):
        raise ContinuationProtocolError("Advisor repeated an obligation identity")
    for update in decision.obligations:
        if (
            update.original_message_id != root.original_message_id
            or not 0 <= update.start < update.end <= root.end
            or (update.obligation_id == "request" and (update.parent_id is not None or update.start != 0 or update.end != root.end))
            or (update.obligation_id != "request" and update.parent_id is None)
        ):
            raise ContinuationProtocolError("obligation lost its original human source")
        prior = current.get(update.obligation_id)
        if prior is not None and (
            prior.parent_id,
            prior.original_message_id,
            prior.start,
            prior.end,
        ) != (update.parent_id, update.original_message_id, update.start, update.end):
            raise ContinuationProtocolError("an existing obligation changed its source binding")
        current[update.obligation_id] = ContinuationObligation(
            update.obligation_id,
            update.parent_id,
            update.description,
            update.original_message_id,
            update.start,
            update.end,
            update.status,
        )
    for item in current.values():
        seen = {item.obligation_id}
        parent = item.parent_id
        while parent is not None:
            if parent in seen or parent not in current:
                raise ContinuationProtocolError("obligation parentage is cyclic or missing")
            seen.add(parent)
            ancestor = current[parent]
            if not ancestor.start <= item.start < item.end <= ancestor.end:
                raise ContinuationProtocolError("child obligation exceeds its immutable parent source span")
            parent = current[parent].parent_id
    source_bindings = {(item.original_message_id, item.start, item.end) for item in current.values()}
    if len(source_bindings) != len(current):
        raise ContinuationProtocolError("duplicate immutable source binding")
    return tuple(current.values())


def current_action_receipts(
    history: tuple[ActionExecutionReceipt, ...],
) -> tuple[ActionExecutionReceipt, ...]:
    latest: dict[str, ActionExecutionReceipt] = {}
    transitions = {
        "prepared": {"dispatched", "committed", "refused", "unknown"},
        "dispatched": {"committed", "refused", "unknown"},
        "unknown": {"committed", "refused"},
        "committed": set(),
        "refused": set(),
    }
    for receipt in history:
        prior = latest.get(receipt.action_key)
        if (
            prior is not None
            and prior != receipt
            and (prior.owner_receipt_id != receipt.owner_receipt_id or receipt.disposition not in transitions[prior.disposition])
        ):
            raise AuditIntegrityError("action custody history changed or moved backwards")
        latest[receipt.action_key] = receipt
    return tuple(latest.values())


def continuation_action_key(
    action: PrescribedContinuationAction,
    *,
    retained_evidence: Mapping[str, ContinuationEvidenceRef] | None = None,
) -> str:
    payload = action.model_dump(mode="json")
    payload["obligation_ids"] = sorted(action.obligation_ids)
    premise_evidence = set()
    for receipt_id in action.evidence_ids:
        if retained_evidence is None or receipt_id not in retained_evidence:
            raise ContinuationProtocolError("action premise lacks an actual retained evidence receipt")
        item = retained_evidence[receipt_id]
        if item.source_kind != "provider":
            premise_evidence.add((item.source_kind, item.content_hash, item.coverage_binding))
    payload["evidence_ids"] = sorted(premise_evidence)
    if isinstance(action, ReadContinuationAction):
        arguments, error = parse_json_strict(action.arguments_json)
        if error is not None or type(arguments) is not dict:
            raise ContinuationProtocolError("read action arguments must be one strict object")
        payload["arguments_json"] = arguments
    elif isinstance(action, ReviewContinuationAction):
        # The exact candidate text identity, graph and normalized actual
        # evidence remain. A newer physical review receipt is not novelty.
        payload.pop("evidence_binding")
    return stable_hash(payload)


def require_bound_continuation_decision(
    decision: ContinuationAdvisorDecision,
    *,
    actual_advisor_call_id: str,
    frontier: ContinuationFrontier,
    retained_evidence: Mapping[str, ContinuationEvidenceRef],
    input_hash: str,
    policy_hash: str,
    tool_manifest_hash: str,
    permitted_read_tools: frozenset[str],
    complete_required_coverage: bool,
) -> None:
    decision = canonicalize_continuation_obligations(decision, frontier)
    if not actual_advisor_call_id or decision.decision_id != actual_advisor_call_id:
        raise ContinuationProtocolError("decision lacks its actual Advisor call")
    proposed = proposed_obligations(decision, frontier)
    obligations = {item.obligation_id for item in proposed}
    selected = set(decision.obligation_ids)
    remainder = set(decision.remaining_actionable_ids)
    if not selected <= obligations or not remainder <= obligations:
        raise ContinuationProtocolError("decision names an unknown obligation")
    if len(selected) != len(decision.obligation_ids) or len(remainder) != len(decision.remaining_actionable_ids):
        raise ContinuationProtocolError("decision repeated an obligation reference")
    if any(item.status != "open" and item.obligation_id in remainder for item in proposed):
        raise ContinuationProtocolError("actionable remainder contains a closed obligation")
    open_ids = {item.obligation_id for item in proposed if item.status == "open"}
    if remainder != open_ids:
        raise ContinuationProtocolError("decision omitted an open obligation from its remainder")
    for evidence in decision.evidence:
        if retained_evidence.get(evidence.receipt_id) != evidence:
            raise ContinuationProtocolError("decision names unretained evidence")
    action = decision.prescribed_action
    if action is None:
        if decision.scope == "whole_request":
            if "request" not in selected or not complete_required_coverage or frontier.pending_review_bindings:
                raise ContinuationProtocolError("whole stop lacks complete reviewed scope")
            if any(
                receipt.disposition in ("prepared", "dispatched", "unknown")
                for receipt in current_action_receipts(frontier.action_receipts)
            ):
                raise ContinuationProtocolError("whole stop hides unresolved action custody")
            if any(item.status == "open" for item in proposed):
                raise ContinuationProtocolError("whole stop hides an open obligation")
        elif remainder:
            raise ContinuationProtocolError("subset stop leaves actionable work without a continuation action")
        return
    if not set(action.obligation_ids) <= obligations or not set(action.evidence_ids) <= retained_evidence.keys():
        raise ContinuationProtocolError("action lacks actual obligation/evidence binding")
    if (action.input_hash, action.graph_hash, action.policy_hash, action.tool_manifest_hash) != (
        input_hash,
        frontier.graph_hash,
        policy_hash,
        tool_manifest_hash,
    ):
        raise ContinuationProtocolError("action premise changed")
    if isinstance(action, ReadContinuationAction) and action.tool_name not in permitted_read_tools:
        raise ContinuationProtocolError("Advisor cannot prescribe a mutation as a read")
    key = continuation_action_key(action, retained_evidence=retained_evidence)
    if any(receipt.action_key == key for receipt in frontier.action_receipts):
        raise ContinuationProtocolError("unchanged continuation action has already been prepared")


def apply_bound_continuation_decision(
    decision: ContinuationAdvisorDecision,
    *,
    actual_advisor_call_id: str,
    frontier: ContinuationFrontier,
    retained_evidence: Mapping[str, ContinuationEvidenceRef],
    input_hash: str,
    policy_hash: str,
    tool_manifest_hash: str,
    permitted_read_tools: frozenset[str],
    complete_required_coverage: bool,
) -> ContinuationFrontier:
    decision = canonicalize_continuation_obligations(decision, frontier)
    require_bound_continuation_decision(
        decision,
        actual_advisor_call_id=actual_advisor_call_id,
        frontier=frontier,
        retained_evidence=retained_evidence,
        input_hash=input_hash,
        policy_hash=policy_hash,
        tool_manifest_hash=tool_manifest_hash,
        permitted_read_tools=permitted_read_tools,
        complete_required_coverage=complete_required_coverage,
    )
    from dataclasses import replace

    merged = {item.receipt_id: item for item in frontier.evidence}
    merged.update((item.receipt_id, item) for item in decision.evidence)
    return replace(
        frontier,
        obligations=proposed_obligations(decision, frontier),
        evidence=tuple(merged.values()),
    )


def record_action_receipt(
    frontier: ContinuationFrontier,
    receipt: ActionExecutionReceipt,
    *,
    actual_owner_receipt_id: str,
) -> ContinuationFrontier:
    if not actual_owner_receipt_id or receipt.owner_receipt_id != actual_owner_receipt_id:
        raise AuditIntegrityError("continuation action lacks its owning execution receipt")
    from dataclasses import replace

    previous = tuple(item for item in current_action_receipts(frontier.action_receipts) if item.action_key == receipt.action_key)
    if previous and previous[-1].disposition in ("committed", "refused"):
        if previous[-1] == receipt:
            return frontier
        raise AuditIntegrityError("terminal action receipt changed")
    history = (*frontier.action_receipts, receipt)
    current_action_receipts(history)
    return replace(frontier, action_receipts=history)


def retain_advisor_action(
    stack: ContinuationFrameStack,
    decision: ContinuationAdvisorDecision,
    *,
    actual_advisor_call_id: str,
    frontier: ContinuationFrontier,
    actual_source: ContinuationActionSource,
    retained_evidence: Mapping[str, ContinuationEvidenceRef],
    input_hash: str,
    policy_hash: str,
    tool_manifest_hash: str,
    permitted_read_tools: frozenset[str],
    complete_required_coverage: bool,
) -> tuple[ContinuationFrameStack, PrivateChunk]:
    from elspeth.contracts.hashing import canonical_json
    from elspeth.web.composer.continuation import ContinuationActionSource
    from elspeth.web.composer.continuation_codec import encode_frontier_chunk

    if type(actual_source) is not ContinuationActionSource:
        raise TypeError("Advisor action retention requires its exact current SQL-verified source")
    decision = canonicalize_continuation_obligations(decision, frontier)
    if stack.active_action_json is not None:
        raise ContinuationProtocolError("a prior prescribed action still owns execution")
    if stack.semantic_frontier != encode_frontier_chunk(frontier).reference:
        raise AuditIntegrityError("Advisor frontier differs from the selected complete snapshot")
    accepted = apply_bound_continuation_decision(
        decision,
        actual_advisor_call_id=actual_advisor_call_id,
        frontier=frontier,
        retained_evidence=retained_evidence,
        input_hash=input_hash,
        policy_hash=policy_hash,
        tool_manifest_hash=tool_manifest_hash,
        permitted_read_tools=permitted_read_tools,
        complete_required_coverage=complete_required_coverage,
    )
    if (
        decision.decision == "STOP_CONFIRMED"
        and decision.scope == "whole_request"
        and (
            stack.pending_settlement_ids
            or stack.pending_owner_results
            or any(item.disposition in ("prepared", "dispatched", "unknown") for item in stack.effect_frontier)
        )
    ):
        raise ContinuationProtocolError("whole stop has unsettled actual execution")
    chunk = encode_frontier_chunk(accepted)
    action = decision.prescribed_action
    action_json = None if action is None else canonical_json(action.model_dump(mode="json"))
    return stack.model_copy(
        update={
            "semantic_frontier": chunk.reference,
            "active_action_json": action_json,
            "active_action_source": actual_source if action_json is not None else None,
        }
    ), chunk
