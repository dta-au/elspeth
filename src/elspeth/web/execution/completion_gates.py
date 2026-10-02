"""Durable composer completion-gate facts.

A completion gate (evidence-scoped completion advisory first, R2-F14) is an event that happened
during a compose turn — an LLM ruled, or failed to — not a property of the
pipeline graph, so readiness recomputes cannot rediscover it. This module
owns the persistence envelope for those facts inside ``composer_meta`` and
the read-side merge into a recomputed ``ValidationResult``:

- ``resolve_completion_gate_facts`` applies an explicit advisor decision;
- ``completion_gates_meta_from_facts`` serializes the effective facts;
- ``parse_completion_gates`` parses the envelope back off a
  ``composition_states`` row (Tier 1: our data, malformed shape raises);
- ``merge_completion_gates`` folds parsed facts into a fresh
  ``validate_state`` recompute, withholding ``completion_ready`` only.

Spec: docs-archive/specs/2026-08-01-composer-completion-gate-persistence-design.md.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, NotRequired, TypedDict

from elspeth.core.canonical import stable_hash
from elspeth.web.composer.advisor_decision import (
    AdvisorBlockCause,
    AdvisorGateBlocked,
    AdvisorGateDecision,
    AdvisorGatePassed,
    AdvisorSignoffGateFact,
)
from elspeth.web.composer.authority_hashing import project_composer_authority_payload
from elspeth.web.composer.state import CompositionState
from elspeth.web.execution.schemas import (
    ADVISOR_SIGNOFF_BLOCKED_CODE,
    CHECK_ADVISOR_SIGNOFF,
    CHECK_OUTCOME_SKIPPED_AFTER_FAILURE,
    VALIDATION_CHECK_NAMES,
    ValidationCheck,
    ValidationReadiness,
    ValidationReadinessBlocker,
    ValidationResult,
)
from elspeth.web.interpretation_state import INTERPRETATION_REVIEW_PENDING_CODE

COMPLETION_GATES_META_KEY: Final[str] = "completion_gates"
_ADVISOR_SIGNOFF_GATE_KEY: Final[str] = "advisor_signoff"
_GATE_STATUS_BLOCKED: Final[str] = "blocked"
# Versioned preimage domain so a future envelope change cannot collide with
# fingerprints already persisted under this schema.
_FINGERPRINT_SCHEMA: Final[str] = "elspeth.completion_gate_graph.v2"

# Wording for a gate fact carried forward (by ``merge_composer_meta_updates``)
# onto a graph the advisor never reviewed. The verdict is not repeated — the
# advisor's ruling is never attributed to content it did not see — but
# completion stays withheld until a compose turn obtains a current review.
ADVISOR_SIGNOFF_PENDING_DETAIL: Final[str] = (
    "The evidence-scoped completion advisory review has not covered this pipeline version. "
    "Composer completion remains withheld. Re-run the composer to obtain a current review."
)


class AdvisorSignoffGateDict(TypedDict):
    """Wire/persistence shape of one advisor sign-off gate fact."""

    status: str
    detail: str
    suggestion: str | None
    for_graph: str
    # elspeth-032ec69c41 (ruling 2026-09-22): the reviewer's own words, bounded
    # and sanitised at the parser (``ADVISOR_NOTE_MAX_CHARS``). REQUIRED and
    # nullable: null is the honest value for every blocker that has no
    # reviewer behind it, and an ABSENT key is writer drift the strict parser
    # refuses rather than defaults.
    note: str | None
    cause: str


class CompletionGatesDict(TypedDict):
    """Wire/persistence shape of the ``completion_gates`` envelope.

    A version-only mapping is the explicit "no gates withheld" value.
    """

    schema_version: int
    advisor_signoff: NotRequired[AdvisorSignoffGateDict]


@dataclass(frozen=True, slots=True)
class CompletionGateFacts:
    """Parsed ``completion_gates`` envelope. ``None`` fields = gate not withheld."""

    advisor_signoff: AdvisorSignoffGateFact | None


def advisor_signoff_check_failed(checks: Sequence[ValidationCheck]) -> bool:
    """True only for an ``advisor_signoff`` check the advisor path actually built and failed.

    The strict ledger emits every check DOWNSTREAM of a halted stage as
    ``passed=False`` with ``outcome_code=CHECK_OUTCOME_SKIPPED_AFTER_FAILURE``
    — including ``advisor_signoff`` (``validation._skipped_checks``). A skipped
    row means the gate was never reached, not that the advisory review failed;
    reading it as failure published the pending-handoff "did not clear" notice
    over a CLEAN advisor verdict (elspeth-fa18d54eef — observed live in
    sessions 39578c6f and 7afbc210). The advisor-path builders
    (``_advisor_signoff_pending_validation`` / ``_advisor_signoff_pending_handoff_validation``
    / :func:`_reconcile_advisor_check` below) all set ``outcome_code=None``,
    so the outcome code is the honest discriminator.
    """
    return any(
        check.name == CHECK_ADVISOR_SIGNOFF and not check.passed and check.outcome_code != CHECK_OUTCOME_SKIPPED_AFTER_FAILURE
        for check in checks
    )


def _reconcile_advisor_check(checks: Sequence[ValidationCheck], *, detail: str) -> list[ValidationCheck]:
    """Replace every prior advisor record with one canonically ordered failure."""
    replacement = ValidationCheck(
        name=CHECK_ADVISOR_SIGNOFF,
        passed=False,
        detail=detail,
        affected_nodes=(),
        outcome_code=None,
    )
    reconciled = [check for check in checks if check.name != CHECK_ADVISOR_SIGNOFF]
    advisor_rank = VALIDATION_CHECK_NAMES.index(CHECK_ADVISOR_SIGNOFF)
    for index, check in enumerate(reconciled):
        if VALIDATION_CHECK_NAMES.index(check.name) > advisor_rank:
            reconciled.insert(index, replacement)
            break
    else:
        reconciled.append(replacement)
    return reconciled


def _reconcile_advisor_blocker(
    blockers: Sequence[ValidationReadinessBlocker],
    *,
    detail: str,
    suggestion: str | None,
    note: str | None,
) -> list[ValidationReadinessBlocker]:
    """Replace duplicate advisor blockers while retaining the first slot."""
    replacement = ValidationReadinessBlocker(
        code=ADVISOR_SIGNOFF_BLOCKED_CODE,
        component_id="pipeline",
        component_type="pipeline",
        detail=detail,
        suggestion=suggestion,
        note=note,
    )
    reconciled: list[ValidationReadinessBlocker] = []
    replaced = False
    for blocker in blockers:
        if blocker.code != ADVISOR_SIGNOFF_BLOCKED_CODE:
            reconciled.append(blocker)
        elif not replaced:
            reconciled.append(replacement)
            replaced = True
    if not replaced:
        reconciled.append(replacement)
    return reconciled


def completion_gate_fingerprint(state: CompositionState) -> str:
    """Canonical fingerprint of the pipeline content a gate verdict applies to.

    Includes metadata: the advisor reviews the pipeline name and stated
    description alongside sources/nodes/edges/outputs. Only ``version`` is
    excluded from serialized pipeline content: a save alone does not change
    the evidence the advisor reviewed.

    The pipeline content goes through the Composer authority projection, so
    the order of the ``sources`` map and of mapping-form row_union and
    coalesce branches is bound: a verdict on one order is never carried onto
    a graph whose ingest or merge order changed.
    """
    state_d = state.to_dict()
    projected = project_composer_authority_payload(
        {
            "sources": state_d["sources"],
            "nodes": state_d["nodes"],
            "edges": state_d["edges"],
            "outputs": state_d["outputs"],
        }
    )
    return stable_hash(
        {
            "schema": _FINGERPRINT_SCHEMA,
            "metadata": state_d["metadata"],
            **projected,
        }
    )


def completion_gates_meta_from_facts(facts: CompletionGateFacts | None) -> CompletionGatesDict:
    """Re-serialize parsed gate facts into the persistence envelope.

    Used by save paths that carry NO advisor adjudication of their own
    (recovery persists after a failed compose turn) to carry a durable
    advisor fact forward verbatim instead of erasing it. The fact is parsed
    off the prior row first (``parse_completion_gates`` raises on
    corruption), then re-emitted unchanged; the embedded ``for_graph``
    fingerprint keeps the read side honest — ``merge_completion_gates``
    downgrades a carried fact to the pending wording the moment the graph
    no longer matches, so the verdict is never re-attributed to content the
    advisor did not see.
    """
    if facts is None or facts.advisor_signoff is None:
        return {"schema_version": 2}
    return {
        "schema_version": 2,
        "advisor_signoff": AdvisorSignoffGateDict(
            status=_GATE_STATUS_BLOCKED,
            detail=facts.advisor_signoff.detail,
            suggestion=facts.advisor_signoff.suggestion,
            for_graph=facts.advisor_signoff.for_graph,
            note=facts.advisor_signoff.note,
            cause=facts.advisor_signoff.cause.value,
        ),
    }


def resolve_completion_gate_facts(
    prior: CompletionGateFacts | None,
    decision: AdvisorGateDecision | None,
    state: CompositionState,
) -> CompletionGateFacts:
    """Apply only an explicit adjudication for the final graph.

    No decision preserves the prior fact, even on a changed graph: readers
    then display pending-review wording until an actual review supersedes it.
    """
    if decision is None:
        return prior if prior is not None else CompletionGateFacts(advisor_signoff=None)
    if isinstance(decision, AdvisorGatePassed):
        for_graph = decision.for_graph
        fact = None
    elif isinstance(decision, AdvisorGateBlocked):
        for_graph = decision.fact.for_graph
        fact = decision.fact
    else:
        raise TypeError(f"Unknown advisor gate decision: {type(decision).__name__}")
    if for_graph != completion_gate_fingerprint(state):
        raise ValueError("Advisor gate decision does not match the final graph fingerprint")
    return CompletionGateFacts(advisor_signoff=fact)


def completion_gate_decision_changes(
    prior: CompletionGateFacts | None,
    decision: AdvisorGateDecision | None,
    state: CompositionState,
) -> bool:
    """Whether an explicit decision changes the normalized durable facts."""
    effective = resolve_completion_gate_facts(prior, decision, state)
    if decision is None:
        return False
    previous = prior if prior is not None else CompletionGateFacts(advisor_signoff=None)
    return effective != previous


def parse_completion_gates(
    composer_meta: Mapping[str, Any] | None,
) -> CompletionGateFacts | None:
    """Parse the persisted envelope off a ``composition_states`` row.

    Returns ``None`` when no envelope was written (for example, fork/revert
    paths) — the recompute answers alone. A version-only mapping is an
    explicit "no gates withheld". Tier 1: this is our own persisted data, so
    a malformed shape means corruption or writer drift — raise, never skip
    the gate.

    Nested envelopes are canonical JSON dicts or the exact MappingProxyType
    produced by freezing a CompositionStateRecord. Arbitrary Mapping
    implementations are not part of either owned representation.
    """
    if composer_meta is None:
        return None
    # Sentinel probe: never-reviewed and fork/revert rows can lack the key.
    if COMPLETION_GATES_META_KEY not in composer_meta:
        return None
    raw = composer_meta[COMPLETION_GATES_META_KEY]
    if type(raw) not in (dict, MappingProxyType):
        raise ValueError(f"Tier 1: composer_meta.completion_gates is {type(raw).__name__}, expected a dict or frozen dict")
    version = raw["schema_version"] if "schema_version" in raw else None
    if type(version) is not int or version != 2:
        raise ValueError("Tier 1: completion_gates.schema_version must be 2")
    unknown = set(raw) - {_ADVISOR_SIGNOFF_GATE_KEY, "schema_version"}
    if unknown:
        raise ValueError(f"Tier 1: composer_meta.completion_gates has unknown gate keys {sorted(unknown)!r}")
    # Sentinel probe: the writer persists a version-only envelope after a
    # clean review, so a missing gate key means "not withheld".
    if _ADVISOR_SIGNOFF_GATE_KEY not in raw:
        return CompletionGateFacts(advisor_signoff=None)
    raw_signoff = raw[_ADVISOR_SIGNOFF_GATE_KEY]
    if type(raw_signoff) not in (dict, MappingProxyType):
        raise ValueError(f"Tier 1: completion_gates.advisor_signoff is {type(raw_signoff).__name__}, expected a dict or frozen dict")
    # From here every probe is membership-then-assert: an absent field reads as
    # ``None`` and falls into the same raise as a malformed one, so no absence
    # is ever silently defaulted.
    status = raw_signoff["status"] if "status" in raw_signoff else None
    if type(status) is not str or status != _GATE_STATUS_BLOCKED:
        raise ValueError(f"Tier 1: completion_gates.advisor_signoff.status is {status!r}, expected {_GATE_STATUS_BLOCKED!r}")
    detail = raw_signoff["detail"] if "detail" in raw_signoff else None
    if type(detail) is not str or not detail:
        raise ValueError("Tier 1: completion_gates.advisor_signoff.detail must be a non-empty string")
    for_graph = raw_signoff["for_graph"] if "for_graph" in raw_signoff else None
    if type(for_graph) is not str or not for_graph:
        raise ValueError("Tier 1: completion_gates.advisor_signoff.for_graph must be a non-empty string")
    if "suggestion" not in raw_signoff:
        raise ValueError("Tier 1: completion_gates.advisor_signoff.suggestion is required")
    suggestion = raw_signoff["suggestion"]
    if suggestion is not None and type(suggestion) is not str:
        raise ValueError("Tier 1: completion_gates.advisor_signoff.suggestion must be a string or null")
    if "note" not in raw_signoff:
        raise ValueError("Tier 1: completion_gates.advisor_signoff.note is required")
    note = raw_signoff["note"]
    if note is not None and (type(note) is not str or not note):
        raise ValueError("Tier 1: completion_gates.advisor_signoff.note must be a non-empty string or null")
    allowed_fields = {"status", "detail", "suggestion", "for_graph", "note", "cause"}
    cause_raw = raw_signoff["cause"] if "cause" in raw_signoff else None
    if type(cause_raw) is not str:
        raise ValueError("Tier 1: completion_gates.advisor_signoff.cause must be a string")
    try:
        cause = AdvisorBlockCause(cause_raw)
    except ValueError as exc:
        raise ValueError(f"Tier 1: completion_gates.advisor_signoff.cause is unknown: {cause_raw!r}") from exc
    if set(raw_signoff) - allowed_fields:
        raise ValueError("Tier 1: completion_gates.advisor_signoff has unknown fields")
    return CompletionGateFacts(
        advisor_signoff=AdvisorSignoffGateFact(detail=detail, suggestion=suggestion, for_graph=for_graph, note=note, cause=cause)
    )


def merge_completion_gates(
    result: ValidationResult,
    facts: CompletionGateFacts | None,
    state: CompositionState,
) -> ValidationResult:
    """Merge persisted gate facts into a freshly recomputed result.

    Withholds ``completion_ready`` only — ``is_valid``, ``authoring_valid``
    and ``execution_ready`` stay exactly as the recompute found them, the
    same posture as R2-F14's in-turn ``_advisor_signoff_pending_validation``
    (composer/service.py). A fact whose ``for_graph`` no longer matches the
    state's content is reported with pending wording instead of the persisted
    verdict. A resolvable interpretation handoff keeps its readiness shape;
    its advisor failure is a check only, matching the in-turn projection.
    """
    if facts is None or facts.advisor_signoff is None:
        return result
    fact = facts.advisor_signoff
    current = fact.for_graph == completion_gate_fingerprint(state)
    detail = fact.detail if current else ADVISOR_SIGNOFF_PENDING_DETAIL
    # Mirror no_tool_policy.is_pending_interpretation_handoff without importing
    # that module: its tools registry imports this module through preflight.
    if (
        result.readiness.authoring_valid
        and result.readiness.completion_ready
        and not result.readiness.execution_ready
        and any(blocker.code == INTERPRETATION_REVIEW_PENDING_CODE for blocker in result.readiness.blockers)
    ):
        # Completion-ready denotes the review-card handoff here, not a passed
        # advisor. Retain the durable fact so it blocks ordinary readiness once
        # those cards resolve; only an explicit CLEAN decision clears it.
        handoff_detail = (
            "The evidence-scoped completion advisory review has not cleared. "
            "Resolve the pending interpretation review cards; the advisory review remains outstanding."
        )
        return result.model_copy(update={"checks": _reconcile_advisor_check(result.checks, detail=handoff_detail)})
    return result.model_copy(
        update={
            "checks": _reconcile_advisor_check(result.checks, detail=detail),
            "readiness": ValidationReadiness(
                authoring_valid=result.readiness.authoring_valid,
                execution_ready=result.readiness.execution_ready,
                completion_ready=False,
                blockers=_reconcile_advisor_blocker(
                    result.readiness.blockers,
                    detail=detail,
                    suggestion=fact.suggestion if current else None,
                    # The reviewer's words described the graph it reviewed; on
                    # a changed graph they are dropped with the verdict they
                    # came from, exactly like ``suggestion``.
                    note=fact.note if current else None,
                ),
            ),
        }
    )


def advisor_block_covers_unchanged_graph(
    facts: CompletionGateFacts | None,
    state: CompositionState,
    *,
    initial_version: int,
) -> bool:
    """True when this turn changed nothing AND the advisor already blocked this exact graph.

    Only a classified graph rejection supports explanation-only turns.
    Transient and message-scoped failures need fresh review.
    """
    if state.version != initial_version:
        return False
    if facts is None or facts.advisor_signoff is None:
        return False
    if facts.advisor_signoff.cause is not AdvisorBlockCause.GRAPH_REJECTED:
        return False
    return facts.advisor_signoff.for_graph == completion_gate_fingerprint(state)
