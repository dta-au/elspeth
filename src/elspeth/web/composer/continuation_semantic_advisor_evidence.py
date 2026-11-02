"""Source-derived semantic evidence and whole-request coverage DATA.

This module grants no SQL custody, read execution, review publication or Stop.
Its caller must obtain the paid decision through the current fenced SQL reader.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.advisor_output import ContinuationEvidenceRef, ReadContinuationAction
from elspeth.web.composer.continuation import EndFrame, LogicalRequestBinding, PrivateChunkRef
from elspeth.web.composer.continuation_end_values import EndContinuationCandidate, continuation_candidate_id
from elspeth.web.composer.continuation_progress import (
    ContinuationObligation,
    canonicalize_continuation_obligations,
    proposed_obligations,
)
from elspeth.web.composer.tools._registry import _BLOB_DISCOVERY_TOOLS, _DISCOVERY_TOOLS, _SECRET_DISCOVERY_TOOLS

if TYPE_CHECKING:
    from elspeth.web.sessions.continuation_owner_results import ContinuationReadOwnerResult
    from elspeth.web.sessions.continuation_semantic_actions import SemanticActionAdmission, SemanticReadCommitted
    from elspeth.web.sessions.continuation_semantic_advisor import SemanticAdvisorPaidDecisionData


def _sha256(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _covered_spans(obligations: tuple[ContinuationObligation, ...]) -> tuple[tuple[int, int], ...]:
    spans = sorted((item.start, item.end) for item in obligations if item.status in ("blocked", "satisfied"))
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return tuple(merged)


def _manifest_read_names(encoded: str) -> frozenset[str]:
    raw, error = parse_json_strict(encoded)
    if error is not None or type(raw) is not list or canonical_json(raw) != encoded:
        raise AuditIntegrityError("semantic read tools lost the actual ordered manifest")
    names: list[str] = []
    for item in raw:
        if type(item) is not dict or item.get("type") != "function" or type(item.get("function")) is not dict:
            raise AuditIntegrityError("semantic read tools lost a declared function")
        name = item["function"].get("name")
        if type(name) is not str or not name:
            raise AuditIntegrityError("semantic read tools lost a declared name")
        names.append(name)
    if len(names) != len(set(names)):
        raise AuditIntegrityError("semantic tool manifest repeated a declared name")
    registered = frozenset((*_DISCOVERY_TOOLS, *_BLOB_DISCOVERY_TOOLS, *_SECRET_DISCOVERY_TOOLS))
    return frozenset(names) & registered - {"preview_pipeline"}


@dataclass(frozen=True, slots=True)
class SemanticWholeRequestCoverage:
    original_message_id: str
    original_length: int
    required_spans: tuple[tuple[str, int, int, str], ...]
    covered_spans: tuple[tuple[int, int], ...]
    complete: bool
    candidate_ref: str | None
    candidate_source: PrivateChunkRef | None
    end_candidate: EndContinuationCandidate | None
    evidence_binding: str


@dataclass(frozen=True, slots=True)
class SemanticAdvisorEvidenceData:
    references: tuple[ContinuationEvidenceRef, ...]
    permitted_read_tools: frozenset[str]
    coverage: SemanticWholeRequestCoverage

    def retained_evidence(self) -> dict[str, ContinuationEvidenceRef]:
        by_id: dict[str, ContinuationEvidenceRef] = {}
        for item in self.references:
            prior = by_id.setdefault(item.receipt_id, item)
            if prior != item:
                raise AuditIntegrityError("semantic evidence receipt changed its actual source")
        return by_id


@dataclass(frozen=True, slots=True)
class SemanticReadEvidenceData:
    """Stable read facts plus the distinct full original-result custody digest."""

    reference: ContinuationEvidenceRef
    semantic_result_json: str
    original_result_sha256: str


@dataclass(frozen=True, slots=True)
class SemanticReadValueData:
    """The same actual read values for semantics, with distinct full custody."""

    semantic_result_json: str
    content_hash: str
    original_result_sha256: str


def semantic_read_value_data(result: ContinuationReadOwnerResult, *, env_ref_names: frozenset[str] = frozenset()) -> SemanticReadValueData:
    """Project source-valued read output without its prescribing paid-call ID."""
    from elspeth.web.sessions.continuation_owner_results import ContinuationReadOwnerResult, re_admit_read_owner_result

    if type(result) is not ContinuationReadOwnerResult or type(env_ref_names) is not frozenset:
        raise TypeError("semantic read values require an owned original result and environment scope")
    re_admit_read_owner_result(result, env_ref_names=env_ref_names)
    arguments, error = parse_json_strict(result.arguments_json)
    if error is not None or type(arguments) is not dict or canonical_json(arguments) != result.arguments_json:
        raise AuditIntegrityError("semantic read values lost their original canonical arguments")
    semantic_result = {
        "schema": "composer.semantic-read-result.v1",
        "tool_name": result.tool_name,
        "arguments": arguments,
        "response_json": result.response_json,
        "tool_result_json": result.tool_result_json,
        "blob_versions": [item.model_dump(mode="json") for item in result.blob_versions],
        "attachment_inspections": [item.model_dump(mode="json") for item in result.attachment_inspections],
    }
    return SemanticReadValueData(
        semantic_result_json=canonical_json(semantic_result),
        content_hash=stable_hash(semantic_result),
        original_result_sha256=stable_hash(result.model_dump(mode="json")),
    )


def semantic_read_scope_binding(logical: LogicalRequestBinding, actor_user_id: str, action: ReadContinuationAction) -> str:
    """Bind stable request, policy, graph and read arguments, never paid call history."""
    if type(logical) is not LogicalRequestBinding or type(actor_user_id) is not str or not actor_user_id:
        raise TypeError("semantic read scope requires its original logical request and actor")
    if type(action) is not ReadContinuationAction:
        raise TypeError("semantic read scope requires its original nominal prescribed read")
    arguments, error = parse_json_strict(action.arguments_json)
    if error is not None or type(arguments) is not dict or canonical_json(arguments) != action.arguments_json:
        raise AuditIntegrityError("semantic read scope lost its original canonical arguments")
    return stable_hash(
        {
            "schema": "composer.semantic-read-scope.v1",
            "logical": logical.model_dump(mode="json"),
            "actor_user_id": actor_user_id,
            "input_hash": action.input_hash,
            "graph_hash": action.graph_hash,
            "policy_hash": action.policy_hash,
            "tool_manifest_hash": action.tool_manifest_hash,
            "tool_name": action.tool_name,
            "arguments": arguments,
        }
    )


def derive_semantic_read_evidence(admission: SemanticActionAdmission, committed: SemanticReadCommitted) -> SemanticReadEvidenceData:
    """Project one cold-proved read into source-valued Advisor DATA.

    The caller must reprove the committed SQL effect before using this DATA.
    Physical prescribing-call identity remains in the full custody digest, but
    cannot make an unchanged read result a novel semantic action premise.
    """
    from elspeth.web.sessions.continuation_owner_results import ContinuationReadOwnerResult
    from elspeth.web.sessions.continuation_semantic_actions import SemanticActionAdmission, SemanticReadCommitted

    if type(admission) is not SemanticActionAdmission or type(committed) is not SemanticReadCommitted:
        raise TypeError("semantic read evidence requires actual nominal admission and committed result DATA")
    action, result = admission.action, committed.original_result
    if type(action) is not ReadContinuationAction or type(result) is not ContinuationReadOwnerResult:
        raise AuditIntegrityError("semantic read evidence lost its original prescribed read")
    arguments, error = parse_json_strict(action.arguments_json)
    if (
        error is not None
        or type(arguments) is not dict
        or result.tool_name != action.tool_name
        or result.arguments_json != canonical_json(arguments)
        or result.prescribing_provider_call_id != admission.paid.audit_call.call_id
        or admission.action_key is None
        or committed.owner_effect_key != f"read:{admission.action_key}"
        or committed.receipt.action_key != admission.action_key
        or committed.receipt.owner_receipt_id != committed.owner_effect_key
        or committed.receipt.disposition != "committed"
    ):
        raise AuditIntegrityError("semantic read evidence changed its original paid action, effect or receipt")
    values = semantic_read_value_data(result, env_ref_names=admission.paid.invocation.source.env_ref_names)
    reference = ContinuationEvidenceRef(
        receipt_id=committed.owner_effect_key,
        source_kind="read",
        content_hash=values.content_hash,
        original_attempt_id=None,
        coverage_binding=semantic_read_scope_binding(
            admission.paid.paid_root.logical, admission.paid.invocation.source.actor_user_id, action
        ),
    )
    return SemanticReadEvidenceData(
        reference=reference,
        semantic_result_json=values.semantic_result_json,
        original_result_sha256=values.original_result_sha256,
    )


def derive_paid_semantic_evidence(
    paid: SemanticAdvisorPaidDecisionData, *, end_candidate: EndContinuationCandidate | None = None
) -> SemanticAdvisorEvidenceData:
    """Derive refs and coverage from one SQL-proved original paid decision."""
    from elspeth.web.sessions.continuation_semantic_advisor import SemanticAdvisorPaidDecisionData

    if type(paid) is not SemanticAdvisorPaidDecisionData:
        raise TypeError("semantic evidence requires the original nominal paid decision")
    paid.invocation.verify()
    source = paid.invocation.source
    invocation = paid.invocation.invocation
    call_id = paid.audit_call.call_id
    if type(call_id) is not str or not call_id or type(invocation.coverage_binding) is not str or not invocation.coverage_binding:
        raise AuditIntegrityError("semantic evidence lost its original paid call or source binding")
    if (
        paid.decision.decision_id != call_id
        or source.logical_json != canonical_json(paid.paid_root.logical.model_dump(mode="json"))
        or paid.paid_root.logical.session_id != str(paid.session_id)
        or paid.paid_root.logical.operation_id != paid.operation_id
    ):
        raise AuditIntegrityError("semantic evidence changed its original request or paid call")
    parent = paid.invocation.frontier
    request = source.original_user_message
    root = next((item for item in parent.obligations if item.obligation_id == "request"), None)
    if (
        root is None
        or root.parent_id is not None
        or root.original_message_id != paid.paid_root.logical.original_user_message_id
        or (root.start, root.end) != (0, len(request))
        or root.description != request
    ):
        raise AuditIntegrityError("semantic coverage lost the whole original human request")
    decision = canonicalize_continuation_obligations(paid.decision, parent)
    obligations = proposed_obligations(decision, parent)
    covered = _covered_spans(obligations)
    complete = (
        covered == ((0, len(request)),) and all(item.status != "open" for item in obligations) and "request" in decision.obligation_ids
    )
    candidate_ref: str | None = None
    candidate_source: PrivateChunkRef | None = None
    end_frames = tuple(frame for frame in paid.paid_root.frames if type(frame) is EndFrame)
    if end_frames:
        if len(end_frames) != 1 or type(end_candidate) is not EndContinuationCandidate:
            raise AuditIntegrityError("semantic review lost its one actual retained END candidate")
        end_frame = end_frames[0]
        if end_frame.candidate != end_candidate_ref(end_candidate):
            raise AuditIntegrityError("semantic review changed its selected END candidate")
        if (end_candidate.state, end_candidate.evidence, end_candidate.iteration) != (
            source.state.reference,
            source.evidence.reference,
            source.iteration.reference,
        ):
            raise AuditIntegrityError("semantic review changed its original END candidate sources")
        candidate_ref = continuation_candidate_id(end_candidate)
        candidate_source = end_frame.candidate
    elif end_candidate is not None:
        raise AuditIntegrityError("semantic evidence invented an END candidate")
    references = (
        *parent.evidence,
        ContinuationEvidenceRef(
            receipt_id=source.checkpoint_id,
            source_kind="graph",
            content_hash=_sha256(source.state.canonical),
            original_attempt_id=None,
            coverage_binding=invocation.coverage_binding,
        ),
        ContinuationEvidenceRef(
            receipt_id=call_id,
            source_kind="provider",
            content_hash=_sha256(canonical_json(paid.audit_call.to_dict())),
            original_attempt_id=call_id,
            coverage_binding=invocation.coverage_binding,
        ),
    )
    result = SemanticAdvisorEvidenceData(
        references=references,
        permitted_read_tools=_manifest_read_names(source.tool_manifest_json),
        coverage=SemanticWholeRequestCoverage(
            original_message_id=root.original_message_id,
            original_length=len(request),
            required_spans=tuple((item.original_message_id, item.start, item.end, item.status) for item in obligations),
            covered_spans=covered,
            complete=complete,
            candidate_ref=candidate_ref,
            candidate_source=candidate_source,
            end_candidate=end_candidate,
            evidence_binding=invocation.coverage_binding,
        ),
    )
    result.retained_evidence()
    return result


def end_candidate_ref(candidate: EndContinuationCandidate) -> PrivateChunkRef:
    from elspeth.web.composer.continuation_end_values import encode_end_candidate

    return encode_end_candidate(candidate).reference
