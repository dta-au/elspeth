"""Durable composer completion-gate facts.

A completion gate (evidence-scoped completion advisory first, R2-F14) is an event that happened
during a compose turn — an LLM ruled, or failed to — not a property of the
pipeline graph, so readiness recomputes cannot rediscover it. This module
owns the persistence envelope for those facts inside ``composer_meta`` and
the read-side merge into a recomputed ``ValidationResult``:

- ``completion_gates_meta_value`` derives the envelope a compose save
  persists (writer side, ``_state_data_from_composer_state``);
- ``parse_completion_gates`` parses the envelope back off a
  ``composition_states`` row (Tier 1: our data, malformed shape raises);
- ``merge_completion_gates`` folds parsed facts into a fresh
  ``validate_state`` recompute, withholding ``completion_ready`` only.

Spec: docs-archive/specs/2026-08-01-composer-completion-gate-persistence-design.md.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, TypedDict

from elspeth.core.canonical import stable_hash
from elspeth.web.composer.state import CompositionState
from elspeth.web.execution.schemas import (
    ADVISOR_SIGNOFF_BLOCKED_CODE,
    CHECK_ADVISOR_SIGNOFF,
    VALIDATION_CHECK_NAMES,
    ValidationCheck,
    ValidationReadiness,
    ValidationReadinessBlocker,
    ValidationResult,
)

COMPLETION_GATES_META_KEY: Final[str] = "completion_gates"
_ADVISOR_SIGNOFF_GATE_KEY: Final[str] = "advisor_signoff"
_GATE_STATUS_BLOCKED: Final[str] = "blocked"
# Versioned preimage domain so a future envelope change cannot collide with
# fingerprints already persisted under this schema.
_FINGERPRINT_SCHEMA: Final[str] = "elspeth.completion_gate_graph.v1"

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
    for_graph: str


class CompletionGatesDict(TypedDict, total=False):
    """Wire/persistence shape of the ``completion_gates`` envelope.

    ``total=False``: an empty mapping is the explicit "no gates withheld"
    value the writer persists on every clean compose turn.
    """

    advisor_signoff: AdvisorSignoffGateDict


@dataclass(frozen=True, slots=True)
class AdvisorSignoffGateFact:
    """One persisted advisor sign-off outcome, bound to the reviewed graph."""

    detail: str
    for_graph: str


@dataclass(frozen=True, slots=True)
class CompletionGateFacts:
    """Parsed ``completion_gates`` envelope. ``None`` fields = gate not withheld."""

    advisor_signoff: AdvisorSignoffGateFact | None


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
) -> list[ValidationReadinessBlocker]:
    """Replace duplicate advisor blockers while retaining the first slot."""
    replacement = ValidationReadinessBlocker(
        code=ADVISOR_SIGNOFF_BLOCKED_CODE,
        component_id="pipeline",
        component_type="pipeline",
        detail=detail,
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
    """Canonical fingerprint of the graph content a gate verdict applies to.

    ``metadata`` and ``version`` are deliberately excluded: a rename or a
    version bump does not invalidate an advisor verdict; a change to
    sources/nodes/edges/outputs — the content the advisor reviewed — does.
    """
    state_d = state.to_dict()
    return stable_hash(
        {
            "schema": _FINGERPRINT_SCHEMA,
            "sources": state_d["sources"],
            "nodes": state_d["nodes"],
            "edges": state_d["edges"],
            "outputs": state_d["outputs"],
        }
    )


def completion_gates_meta_value(
    runtime_preflight: ValidationResult | None,
    state: CompositionState,
) -> CompletionGatesDict:
    """Derive the ``completion_gates`` value a compose save persists.

    Always returns a mapping (possibly empty): the writer overwrites the key
    on every compose-preflight save, so a stale blocked fact cannot survive a
    clean turn and ``merge_composer_meta_updates`` needs no deletion
    semantics. ``None`` (preflight crashed or never ran) yields ``{}`` —
    those saves persist ``is_valid=False`` and carry no gate verdict.
    """
    if runtime_preflight is None:
        return {}
    blocked = [blocker for blocker in runtime_preflight.readiness.blockers if blocker.code == ADVISOR_SIGNOFF_BLOCKED_CODE]
    if not blocked:
        return {}
    return {
        "advisor_signoff": AdvisorSignoffGateDict(
            status=_GATE_STATUS_BLOCKED,
            detail=blocked[0].detail,
            for_graph=completion_gate_fingerprint(state),
        )
    }


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
        return {}
    return {
        "advisor_signoff": AdvisorSignoffGateDict(
            status=_GATE_STATUS_BLOCKED,
            detail=facts.advisor_signoff.detail,
            for_graph=facts.advisor_signoff.for_graph,
        )
    }


def parse_completion_gates(
    composer_meta: Mapping[str, Any] | None,
) -> CompletionGateFacts | None:
    """Parse the persisted envelope off a ``composition_states`` row.

    Returns ``None`` when no envelope was ever written (historical rows,
    fork/revert paths) — the recompute answers alone. An empty mapping is an
    explicit "no gates withheld". Tier 1: this is our own persisted data, so
    a malformed shape means corruption or writer drift — raise, never skip
    the gate.

    Tier-model adjudication note (R1 membership / ``.get()`` / R5
    ``isinstance()`` below): this function is the ADR-032 parse point for a
    JSON-round-tripped envelope read back off our own ``composition_states``
    row. The membership checks are sentinel probes for keys whose ABSENCE is
    a legal, meaningful state (`no envelope written` / `no gate withheld`) —
    never a silent default. The remaining ``.get()`` calls are required-field
    reads: every present-but-malformed shape raises, and the two
    ``isinstance(..., Mapping)`` checks are shape validation that constructs
    the owned ``CompletionGateFacts`` type, not defensive masking of a code
    bug. If this function ever stops raising on malformed present values, or
    starts being fed anything other than the ``composer_meta`` mapping of a
    ``composition_states`` record, that reasoning is invalid and the
    suppressions should be withdrawn.
    """
    if composer_meta is None:
        return None
    # Sentinel probe: rows written before this envelope existed (and
    # fork/revert paths) legitimately lack the key.
    if COMPLETION_GATES_META_KEY not in composer_meta:
        return None
    raw = composer_meta[COMPLETION_GATES_META_KEY]
    if not isinstance(raw, Mapping):
        raise ValueError(f"Tier 1: composer_meta.completion_gates is {type(raw).__name__}, expected a mapping")
    unknown = set(raw) - {_ADVISOR_SIGNOFF_GATE_KEY}
    if unknown:
        raise ValueError(f"Tier 1: composer_meta.completion_gates has unknown gate keys {sorted(unknown)!r}")
    # Sentinel probe: the writer persists {} on every clean compose turn, so
    # a missing gate key means "not withheld", not corruption.
    if _ADVISOR_SIGNOFF_GATE_KEY not in raw:
        return CompletionGateFacts(advisor_signoff=None)
    raw_signoff = raw[_ADVISOR_SIGNOFF_GATE_KEY]
    if not isinstance(raw_signoff, Mapping):
        raise ValueError(f"Tier 1: completion_gates.advisor_signoff is {type(raw_signoff).__name__}, expected a mapping")
    # From here every probe is get-then-assert: a missing or malformed field
    # falls through to a raise, so no absence is ever silently defaulted.
    status = raw_signoff.get("status")
    if status != _GATE_STATUS_BLOCKED:
        raise ValueError(f"Tier 1: completion_gates.advisor_signoff.status is {status!r}, expected {_GATE_STATUS_BLOCKED!r}")
    detail = raw_signoff.get("detail")
    if type(detail) is not str or not detail:
        raise ValueError("Tier 1: completion_gates.advisor_signoff.detail must be a non-empty string")
    for_graph = raw_signoff.get("for_graph")
    if type(for_graph) is not str or not for_graph:
        raise ValueError("Tier 1: completion_gates.advisor_signoff.for_graph must be a non-empty string")
    return CompletionGateFacts(advisor_signoff=AdvisorSignoffGateFact(detail=detail, for_graph=for_graph))


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
    verdict.
    """
    if facts is None or facts.advisor_signoff is None:
        return result
    fact = facts.advisor_signoff
    current = fact.for_graph == completion_gate_fingerprint(state)
    detail = fact.detail if current else ADVISOR_SIGNOFF_PENDING_DETAIL
    return result.model_copy(
        update={
            "checks": _reconcile_advisor_check(result.checks, detail=detail),
            "readiness": ValidationReadiness(
                authoring_valid=result.readiness.authoring_valid,
                execution_ready=result.readiness.execution_ready,
                completion_ready=False,
                blockers=_reconcile_advisor_blocker(result.readiness.blockers, detail=detail),
            ),
        }
    )
