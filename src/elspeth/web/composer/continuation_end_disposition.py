"""Shared END completion DATA; durable SQL pass proof remains mandatory."""

from typing import Literal

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.advisor_audit import AdvisorCheckpointPassRecord, AdvisorGraphReviewIdentity
from elspeth.web.composer.advisor_decision import AdvisorGateBlocked, AdvisorGatePassed
from elspeth.web.composer.no_tool_policy import state_is_structurally_empty
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.execution.completion_gates import completion_gate_fingerprint
from elspeth.web.execution.schemas import ValidationReadiness, ValidationResult


def classify_end_review_result(result: ComposerResult, *, publication_binding: str | None) -> Literal["clean", "blocked"]:
    """Classify actual shared-gate DATA without granting publication authority.

    A clean classification must still be joined to its original durable model
    pass by the SQL writer and cold reader. Empty graphs require a genuine prose
    publication review; they do not acquire a fabricated graph-pass fact.
    """
    if type(result) is not ComposerResult:
        raise TypeError("END disposition requires its actual shared-gate result")
    decision, identity, preflight = result.advisor_gate_decision, result.graph_review_identity, result.runtime_preflight
    if identity is not None and type(identity) is not AdvisorGraphReviewIdentity:
        raise AuditIntegrityError("END result changed its actual review identity")
    if publication_binding is not None and (
        type(publication_binding) is not str
        or len(publication_binding) != 64
        or any(character not in "0123456789abcdef" for character in publication_binding)
    ):
        raise AuditIntegrityError("END result changed its actual publication binding")
    if identity is not None and publication_binding is not None and identity.binding != publication_binding:
        raise AuditIntegrityError("END result changed its original bound review")
    if type(decision) is AdvisorGateBlocked or (
        type(preflight) is ValidationResult
        and type(preflight.readiness) is ValidationReadiness
        and not preflight.readiness.completion_ready
    ):
        return "blocked"
    if (
        type(identity) is AdvisorGraphReviewIdentity
        and type(publication_binding) is str
        and identity.source == "model"
        and identity.for_graph == completion_gate_fingerprint(result.state)
    ):
        if state_is_structurally_empty(result.state):
            if decision is None and preflight is None:
                return "clean"
        elif (
            type(decision) is AdvisorGatePassed
            and decision.for_graph == identity.for_graph
            and type(preflight) is ValidationResult
            and type(preflight.readiness) is ValidationReadiness
            and preflight.readiness.completion_ready
        ):
            return "clean"
    raise AuditIntegrityError("END result lacks a closed actual completion disposition")


def require_end_publication_pass(record: AdvisorCheckpointPassRecord, *, publication_binding: str) -> None:
    """Validate DATA read from the original durable accepted model END pass."""
    if (
        type(record) is not AdvisorCheckpointPassRecord
        or record.phase != "end"
        or record.source != "model"
        or record.verdict != "clean"
        or record.publication_verdict != "ADMIT"
        or record.publication_binding != publication_binding
        or record.provider_attempts < 1
    ):
        raise AuditIntegrityError("END publication lost its original accepted physical model pass")
