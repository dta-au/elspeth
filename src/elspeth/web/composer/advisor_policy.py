"""Advisor verdict presentation and completion-readiness policy.

This module builds closed, user-safe validation outcomes from admitted advisor
decisions. Provider dispatch and durable checkpoint publication belong to the
advisor checkpoint owner.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, Final, Literal

from elspeth.web.composer import no_tool_policy as _no_tool_policy
from elspeth.web.composer.state import CompositionState
from elspeth.web.execution.schemas import (
    ADVISOR_SIGNOFF_BLOCKED_CODE,
    CHECK_ADVISOR_SIGNOFF,
    ValidationCheck,
    ValidationCheckName,
    ValidationError,
    ValidationReadiness,
    ValidationReadinessBlocker,
    ValidationResult,
)

_ADVISOR_SIGNOFF_UNVERIFIED_PUBLISHED_NOTICE = _no_tool_policy._ADVISOR_SIGNOFF_UNVERIFIED_PUBLISHED_NOTICE
_ADVISOR_SIGNOFF_UNREPAIRABLE_HEADER = _no_tool_policy._ADVISOR_SIGNOFF_UNREPAIRABLE_HEADER
_ADVISOR_SIGNOFF_PENDING_PUBLISHED_NOTICE = _no_tool_policy._ADVISOR_SIGNOFF_PENDING_PUBLISHED_NOTICE
_advisor_signoff_pending_handoff_wording = _no_tool_policy.advisor_signoff_pending_handoff_wording


def outstanding_findings_detail(outstanding_findings: ValidationResult | None) -> str | None:
    """Leading objection from a red masked validation, or None for a pure handoff."""
    if outstanding_findings is None:
        return None
    objection = _no_tool_policy.first_validation_objection(outstanding_findings)
    return objection if objection else "run validation for details."


def advisor_preflight_shape(runtime_result: ValidationResult | None) -> Literal["absent", "green", "pending_handoff", "red"]:
    """Closed preflight-shape vocabulary for terminal-publication telemetry."""
    if runtime_result is None:
        return "absent"
    if runtime_result.is_valid:
        return "green"
    if _no_tool_policy.is_pending_interpretation_handoff(runtime_result):
        return "pending_handoff"
    return "red"


# END authoritative advisor gate. The synthetic ValidationResult builder is
# module-level because it is pure data with no service-instance dependency.
_ADVISOR_SIGNOFF_BLOCKED_CODE: Final[str] = ADVISOR_SIGNOFF_BLOCKED_CODE
# Mirrors the orphan gate's check-name convention so the synthetic fail-closed
# result names a stable check the UI/audit can key on.
_ADVISOR_SIGNOFF_BLOCKED_CHECK_NAME: Final[ValidationCheckName] = CHECK_ADVISOR_SIGNOFF
ADVISOR_UNAVAILABLE_USER_DETAIL: Final[str] = "advisor model was unavailable after retry"
# Fixed user-facing detail for a MALFORMED advisor failure (parse/shape error, or
# any unclassified exception). Like the unavailable detail it carries NO provider
# SDK text, exception class name, message, URL, or credential — the raw exception
# is classified only into ``AdvisorCheckpointVerdict.failure_class`` (P5.3/D13).
ADVISOR_MALFORMED_USER_DETAIL: Final[str] = "advisor response was malformed"


# elspeth-032ec69c41 (ruling 2026-09-22): one fixed backend sentence per closed
# advisor category. The advisor picks the category from a closed vocabulary; the
# SENTENCE is ours, so no provider text reaches the header even when the
# category is attacker-influenced. An unrecognised category was already
# normalised to "other" by the parser.
_ADVISOR_CATEGORY_HEADERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "request_not_met": "The reviewer found the request not fully met.",
        "error_handling": "The reviewer flagged how failures are handled.",
        "prompt_defect": "The reviewer flagged a prompt.",
        "schema_mismatch": "The reviewer flagged a field or schema mismatch.",
        "other": "The reviewer flagged this pipeline.",
    }
)


def validated_advisor_step_ids(state: CompositionState, raw: Sequence[str]) -> tuple[str, ...]:
    """Keep only ids the state actually has, in the advisor's order, de-duplicated.

    The advisor's ``STEPS:`` line is provider text: this is what makes the
    header safe to render. ``state.sources`` is a mapping keyed by source name,
    so its keys are the ids; nodes carry ``id`` and outputs carry ``name``.
    """
    known = set(state.sources) | {node.id for node in state.nodes} | {output.name for output in state.outputs}
    kept: list[str] = []
    for candidate in raw:
        if candidate in known and candidate not in kept:
            kept.append(candidate)
    return tuple(kept)


def _advisor_flagged_header(category: str, step_ids: Sequence[str]) -> str:
    """The backend-authored header sentence(s) for a rendered FLAG.

    The parser already normalises ``category`` into
    :data:`advisor_output.ADVISOR_FINDING_CATEGORIES`, but this function is reachable with a
    plain ``str`` from the wording helper's default, so the fall back to
    "other" is written out rather than hidden in a ``dict.get`` default: an
    unrecognised category is a caller bug we want visible in the code, not a
    silently absorbed lookup.
    """
    if category not in _ADVISOR_CATEGORY_HEADERS:
        category = "other"
    header = _ADVISOR_CATEGORY_HEADERS[category]
    if step_ids:
        return f"{header} Steps named by the reviewer: {', '.join(step_ids)}."
    return header


def advisor_signoff_blocked_validation(
    *,
    reason: str,
    findings: str,
    findings_backend_authored: bool = False,
    category: str,
    step_ids: Sequence[str],
    note: str | None,
) -> ValidationResult:
    """Build the fully-red shape for a RED runtime preflight.

    Returned (not raised) by the END authoritative advisor gate
    (:meth:`AdvisorCheckpointOwner._advisor_blocked_result`) when the advisor
    A green build always takes :func:`advisor_signoff_pending_validation`,
    regardless of advisor reason: a FLAG is not evidence execution is unsafe.
    An ABSENT preflight takes :func:`advisor_signoff_unverified_validation`
    (elspeth-2ae50afcd1 facet B) — the same fully-blocking structure with
    wording that does not claim a preflight ran.

    Mirrors :func:`_orphaned_interpretation_review_validation`'s shape: every
    readiness axis is blocking (``authoring_valid`` / ``execution_ready`` /
    ``completion_ready`` all ``False``) so the UI cannot advance regardless of
    which flag it gates on. FLAGGED reasons use one fixed sign-off notice;
    unavailable and malformed reasons retain their fixed backend wording.
    Raw advisor-MODEL findings never enter this wire shape; the one exception
    is the backend-authored deterministic pre-scan finding, which names the
    triggering key/field so the operator can act (elspeth-cd9af8e61d,
    ``findings_backend_authored``).
    """
    detail, suggestion = advisor_signoff_blocked_wording(
        reason=reason,
        findings=findings,
        findings_backend_authored=findings_backend_authored,
        category=category,
        step_ids=step_ids,
    )
    return _advisor_signoff_fully_blocking_validation(detail=detail, suggestion=suggestion, note=note)


def advisor_signoff_unverified_validation(
    *,
    reason: str,
    findings: str,
    findings_backend_authored: bool = False,
    category: str,
    step_ids: Sequence[str],
    note: str | None,
) -> ValidationResult:
    """Build the fully-blocking shape for an ABSENT runtime preflight.

    elspeth-2ae50afcd1 facet B (operator-adjudicated 2026-09-02). ``None``
    means the preflight was NOT COMPUTED this turn — the elspeth-88592f5be7
    tri-state's "unknown, fail closed" arm. Unknown readiness withholds every
    axis exactly like :func:`advisor_signoff_blocked_validation` (nothing may
    advance), but the surfaced wording states the advisory review did not
    clear and readiness was not re-verified, instead of reporting a preflight
    failure the turn never produced. Same ``advisor_signoff_blocked`` code and
    check shape, so no closed vocabulary widens.
    """
    detail, suggestion = advisor_signoff_blocked_wording(
        reason=reason,
        findings=findings,
        findings_backend_authored=findings_backend_authored,
        notice=_ADVISOR_SIGNOFF_UNVERIFIED_PUBLISHED_NOTICE,
        category=category,
        step_ids=step_ids,
    )
    return _advisor_signoff_fully_blocking_validation(detail=detail, suggestion=suggestion, note=note)


def _advisor_signoff_fully_blocking_validation(*, detail: str, suggestion: str, note: str | None) -> ValidationResult:
    """Shared fully-blocking wire shape for the red and absent advisor blocks."""
    return ValidationResult(
        is_valid=False,
        checks=[
            ValidationCheck(
                name=_ADVISOR_SIGNOFF_BLOCKED_CHECK_NAME,
                passed=False,
                detail=detail,
                affected_nodes=(),
                outcome_code=None,
            )
        ],
        errors=[
            ValidationError(
                component_id="pipeline",
                component_type="pipeline",
                message=detail,
                suggestion=suggestion,
                error_code=_ADVISOR_SIGNOFF_BLOCKED_CODE,
            )
        ],
        readiness=ValidationReadiness(
            authoring_valid=False,
            execution_ready=False,
            completion_ready=False,
            blockers=[
                ValidationReadinessBlocker(
                    code=_ADVISOR_SIGNOFF_BLOCKED_CODE,
                    suggestion=suggestion,
                    note=note,
                    component_id="pipeline",
                    component_type="pipeline",
                    detail=detail,
                )
            ],
        ),
    )


# ---------------------------------------------------------------------------
# Advisor findings re-injection fence (C2 follow-up to Task 6).
#
# ``verdict.findings_text`` is the advisor MODEL's own free text on a FLAGGED
# verdict (or the backend deterministic pre-scan string — see
# ``advisor_prompt_template_injection_finding`` — which is also short and
# backend-controlled). A prompt-injection payload smuggled into an
# operator-authored pipeline option value (Tier-3 at the read site) can
# survive into the advisor's own response and get parroted back here. This
# R2-F13 (elspeth-e8872dfbbe): the BEGIN/END sentinels are meaningful ONLY on
# the LLM re-injection path (:func:`fence_advisor_findings`, consumed by a
# downstream LLM re-reading the transcript) — never on the human-facing wire
# payload (:func:`advisor_signoff_blocked_validation`), which now uses plain
# framing instead so no fence token ever reaches a user surface. On the LLM
# path, advisor output that parrots the exact sentinel line (e.g. the advisor
# model echoing "END_UNTRUSTED_ADVISOR_FINDINGS" back, whether by adversarial
# intent or by innocently quoting the earlier prompt) would otherwise close
# the fence early — a fence ESCAPE, not just a leak — so
# :func:`fence_advisor_findings` neutralizes any embedded occurrence of
# either sentinel inside the payload before wrapping it in the wrapper's own,
# guaranteed-unique BEGIN/END pair.
# ---------------------------------------------------------------------------
_ADVISOR_FINDINGS_MAX_CHARS: Final[int] = 4_000
_ADVISOR_FINDINGS_UNTRUSTED_BEGIN: Final[str] = "BEGIN_UNTRUSTED_ADVISOR_FINDINGS"
_ADVISOR_FINDINGS_UNTRUSTED_END: Final[str] = "END_UNTRUSTED_ADVISOR_FINDINGS"


# R2-F12 (elspeth-bff8fe6864): the user-facing output-contract sentence
# shared by BOTH advisor-injection sites (the END gate's FLAGGED repair
# message and the EARLY advisory transition message) — a single source of
# truth so the two injections cannot drift apart, and so one test constant
# can assert both sites carry the identical clause.
# Ruling 2026-09-22 (elspeth-032ec69c41): the reply is published on a
# blocked turn, so the clause asks for a reply that stands on its own
# rather than one that hides the review; quoting the fenced text stays
# forbidden — it derives from pipeline data and can carry injected text.
# The clause does NOT promise the reply reaches the user: a no-tool reply
# after which the next advisor pass returns CLEAN falls through to finalize
# and case 5 (``_replace_advisor_repair_public_result``) replaces it.
ADVISOR_OUTPUT_CONTRACT_CLAUSE: Final[str] = (
    "Fix the findings via tool calls. The end user has not read these "
    "findings: write your final reply to stand on its own and do not quote "
    "the fenced text."
)

# elspeth-71617f1d21: the END-gate repair-continue message must state that
# MUTATIONS are expected. In session 2e0c8ea3 both advisor repair turns were
# spent entirely on get_pipeline_state lookups — the injection carried only
# the output-contract clause, and nothing said a read-only turn is a wasted
# pass. END-gate only: the EARLY advisory injection deliberately keeps its
# "continue if it does not apply" framing, where demanding a mutation would
# be wrong.
# Ruling 2026-09-22 (elspeth-032ec69c41): the "say what blocks you" exit
# now names an outcome that exists — a no-tool reply ends the turn, and on a
# blocked turn it is published — instead of routing into a deleted reply
# (session 6990d39f). It stops short of promising the user sees it, for the
# case-5 reason on ``ADVISOR_OUTPUT_CONTRACT_CLAUSE``. Trailing space is
# load-bearing: the two clauses concatenate.
ADVISOR_MUTATION_EXPECTATION_CLAUSE: Final[str] = (
    "If the user's active request authorizes changes, resolve graph findings through pipeline MUTATIONS via tool calls "
    "(e.g. patch_node_options, upsert_node, patch_source_options, "
    "patch_output_options). Re-reading state (get_pipeline_state) or other "
    "lookup-only calls is not a fix, though they can supply evidence. If the user asks for explanation or says not to make changes, "
    "answer that request without changing the pipeline; the advisor block still prevents completion. If a "
    "finding needs a decision only the user can make, or no tool call can "
    "address it, make no change: tell the user what blocks you and what "
    "their options are. That reply ends the turn. "
)

# elspeth-2306940c70: durable provider-visible disclosure persisted on every
# terminal END-gate block. Fixed backend copy only — no advisor findings ride
# this string, so replaying it into later turns cannot re-introduce the
# repair-cohort contamination the gate keeps out of user-visible surfaces.
# Since the 2026-09-22 ruling (elspeth-032ec69c41) the blocked turn's own
# prose is published and replays beside this row; the row is the backend's
# assertion that completion was withheld, whatever that prose claims.
ADVISOR_SIGNOFF_WITHHELD_DISCLOSURE: Final[str] = (
    "[composer-system] The completion advisory review did not clear, so "
    "ELSPETH withheld composer completion for the preceding request. Do not "
    "assume that request was applied: the pipeline state supplied in the "
    "current context is the authoritative record. Verify against it before "
    "describing any earlier instruction as applied or in effect."
)


def _truncate_advisor_findings(findings_text: str) -> str:
    """Cap free-text advisor findings to ``_ADVISOR_FINDINGS_MAX_CHARS``.

    Used only by the internal LLM re-injection fence; human surfaces never
    contain provider findings.
    """
    return findings_text if len(findings_text) <= _ADVISOR_FINDINGS_MAX_CHARS else findings_text[: _ADVISOR_FINDINGS_MAX_CHARS - 1] + "…"


def fence_advisor_findings(findings_text: str) -> str:
    """Bound and fence free-text advisor findings before LLM re-injection.

    Truncation caps the blast radius of a runaway/adversarial advisor
    response; the BEGIN/END markers mirror the
    ``BEGIN/END_UNTRUSTED_PIPELINE_SUMMARY`` convention already used for the
    schema excerpt sent TO the advisor, so a downstream LLM reader (the
    composer re-reading this next turn, or a future assistant-turn replay)
    has the same signal that the enclosed text is untrusted commentary, not
    a new operator instruction. Callers pass only the FLAGGED/free-text case;
    the fixed unavailable/malformed constants are deliberately NOT routed
    through this helper (their wording must stay literal, see callers).

    Before wrapping, any occurrence of the sentinel strings THEMSELVES inside
    the (already-truncated) payload is neutralized by splicing an escape
    backslash into the middle of the token — otherwise advisor output that
    parrots ``END_UNTRUSTED_ADVISOR_FINDINGS`` would prematurely close the
    fence, letting the remainder of the payload be read as trusted
    instructions by the downstream LLM (a fence escape, R2-F13/
    elspeth-e8872dfbbe). Splicing (rather than merely prefixing) breaks the
    token's contiguity so the exact sentinel substring no longer occurs
    anywhere in the escaped payload, guaranteeing the wrapped output contains
    exactly one occurrence of each sentinel: the wrapper's own.
    """
    text = _truncate_advisor_findings(findings_text)
    text = text.replace(
        _ADVISOR_FINDINGS_UNTRUSTED_BEGIN,
        _ADVISOR_FINDINGS_UNTRUSTED_BEGIN[0] + "\\" + _ADVISOR_FINDINGS_UNTRUSTED_BEGIN[1:],
    )
    text = text.replace(
        _ADVISOR_FINDINGS_UNTRUSTED_END,
        _ADVISOR_FINDINGS_UNTRUSTED_END[0] + "\\" + _ADVISOR_FINDINGS_UNTRUSTED_END[1:],
    )
    return f"{_ADVISOR_FINDINGS_UNTRUSTED_BEGIN}\n{text}\n{_ADVISOR_FINDINGS_UNTRUSTED_END}"


# The one-line re-prompt appended to the (Tier-1, backend-produced) checkpoint
# ``problem_summary`` when a transport-successful reply could not be parsed as
# a verdict. It travels the SAME contracted advisor-arguments channel as the
# first attempt — there is no second, unaudited prompt path.
_ADVISOR_VERDICT_FORMAT_REPROMPT: Final[str] = (
    "The previous reply did not satisfy the checkpoint schema. Return only the required JSON object, "
    "following the output contract in the system instructions."
)
_ADVISOR_VERDICT_CONTRACT_REPROMPT: Final[str] = (
    "The previous reply satisfied the checkpoint schema but violated the output contract. "
    "For CLEAN, steps must be empty and note must be null; FLAGGED requires non-empty, non-whitespace findings. "
    "Return only the required JSON object, following the output contract in the system instructions."
)


def advisor_arguments_with_format_reprompt(arguments: Mapping[str, Any], *, schema_valid: bool) -> dict[str, Any]:
    """Return checkpoint arguments with a fixed re-prompt matching the rejection.

    The retry must not lose the original problem summary (the rubric, the
    degeneracy directive, the pipeline excerpt) — it only adds an explicit
    restatement of the schema or semantic rules the previous reply violated.
    Neither path echoes the rejected provider text.
    """
    reprompt = _ADVISOR_VERDICT_CONTRACT_REPROMPT if schema_valid else _ADVISOR_VERDICT_FORMAT_REPROMPT
    retry = dict(arguments)
    retry["problem_summary"] = f"{arguments['problem_summary']} {reprompt}"
    return retry


def advisor_signoff_blocked_wording(
    *,
    reason: str,
    findings: str,
    findings_backend_authored: bool = False,
    notice: str = _ADVISOR_SIGNOFF_PENDING_PUBLISHED_NOTICE,
    category: str = "other",
    step_ids: Sequence[str] = (),
) -> tuple[str, str]:
    """Return the (detail, suggestion) pair for one blocked-sign-off reason.

    Shared by the fully-blocking result (:func:`advisor_signoff_blocked_validation`),
    the validated-but-unsigned result (:func:`advisor_signoff_pending_validation`),
    and the absent-preflight result (:func:`advisor_signoff_unverified_validation`)
    so the surfaces cannot drift. ``notice`` swaps the fixed notice the FLAGGED
    arms embed — the unverified shape states readiness was not re-verified
    (elspeth-2ae50afcd1 facet B) — while the could-not-be-obtained arms are
    notice-independent and identical across all three consumers. All three
    serve only ``_advisor_blocked_result``, which publishes the composer's
    reply, so ``notice`` is always a ``_PUBLISHED_`` notice: the withheld form
    would put "ELSPETH withheld the composer's own summary" on the durable
    blocker beside that published summary.

    R2-F14: ``reason`` is now the RESOLVED failure class, not a fixed literal.
    The old text interpolated ``(unavailable)`` unconditionally and then
    appended a ``findings`` constant that could say "advisor response was
    malformed" — a note that contradicted itself in the same sentence. The
    reason parenthetical is dropped from the could-not-be-obtained branches
    entirely: ``findings`` already names the class in plain language.

    elspeth-cd9af8e61d (c): the FLAGGED branches used to discard ``findings``
    entirely, so a deterministic pre-scan force-FLAG — byte-identical on
    every pass, no advisor call at all — blocked completion without ever
    telling the operator which key/field triggered. When
    ``findings_backend_authored`` is True (the deterministic pre-scan
    string: fixed shape, names the triggering surface, carries no provider
    text) the finding is appended so the operator can act. Advisor-MODEL
    findings remain withheld on these branches (R2-F13, narrowed by the
    2026-09-22 ruling: raw provider findings never enter ``detail``,
    ``suggestion``, the check text or the composer's published prose — they
    reach the user only through the blocker's ``note`` field, which the
    caller sets. Scoped deliberately: a flagged model's subsequent TOOL CALLS
    can still write derived text into pipeline state the user inspects, and
    that state channel is uncontained by design, elspeth-25f7b757e7 A4).

    ``category`` and ``step_ids`` (elspeth-032ec69c41) add the backend-authored
    header to a RENDERED flag: one fixed sentence per closed category, plus the
    ids the caller already validated against the state. Both are backend copy —
    the advisor chooses which sentence, never its words — so they sit in
    ``detail`` while the advisor's own prose stays in ``note``. A
    backend-authored pre-scan finding keeps its existing wording and gets no
    header: it is not a reviewer's judgement about a step.
    """
    if reason == "flagged_unrepairable":
        # elspeth-25f7b757e7 (A1): the trigger is the user's own chat message,
        # so a pipeline-edit suggestion would be wrong — the one clearing
        # action is rewording. ``findings`` on this reason is always the
        # backend-authored pre-scan string (the user-message arm is this
        # reason's only producer), so surfacing it follows the same
        # elspeth-cd9af8e61d carve-out as the flagged arms below.
        # Fix round 1 (N1): the suggestion claims only what the gate knows.
        # This wording pair serves the RED and ABSENT builders, where an
        # affirmative "no pipeline change is needed" is false or unknowable;
        # that claim lives solely in the GREEN chat notice.
        # Self-review 2026-09-23: ``detail`` names the chat message, not the
        # shared ``notice``. That notice says "Review the pipeline" and states a
        # retry rule for graph rejections — the wrong remedy, and false here: a
        # message rejection is re-reviewed on the next message (the END gate's
        # unchanged-graph skip covers graph rejections only), which is what the
        # suggestion below tells the user to do.
        return (
            f"{_ADVISOR_SIGNOFF_UNREPAIRABLE_HEADER} {findings}"
            if findings_backend_authored and findings
            else _ADVISOR_SIGNOFF_UNREPAIRABLE_HEADER,
            "Reword your chat message to avoid text that reads as instructions to the reviewer, then resend.",
        )
    if reason in {"flagged_final_pass", "flagged_no_repair"}:
        if findings_backend_authored and findings:
            return (
                f"{notice} {findings}",
                "Remove the flagged text from the named field; the advisory review runs again after your next pipeline change.",
            )
        # Header first, then its step sentence, then the standing notice: the
        # plan said "prefix the header, append the steps", but a step list
        # placed after "run again after your next pipeline change" reads as
        # part of the next-steps advice rather than as what the reviewer
        # named. Keeping the two header sentences adjacent is the same copy,
        # ordered as a person reads it.
        return (
            f"{_advisor_flagged_header(category, step_ids)} {notice}",
            "Review the pipeline; validation and the advisory review run again after your next pipeline change.",
        )
    # Provider failures are turn-scoped; another message may obtain a verdict
    # without editing the graph, and its decision replaces the durable block.
    if reason == "unavailable":
        return (
            f"The evidence-scoped completion advisory review could not be obtained; the Composer cannot mark this turn complete. {findings}",
            "The advisor model was unavailable after retry; check the advisor model configuration. "
            "Validation and the advisory review run again on your next message.",
        )
    return (
        f"The evidence-scoped completion advisory review could not be obtained; the Composer cannot mark this turn complete. {findings}",
        "The advisor returned no usable verdict after a format retry; check the advisor model configuration. "
        "Validation and the advisory review run again on your next message.",
    )


def advisor_signoff_pending_validation(
    base: ValidationResult,
    *,
    reason: str,
    findings: str,
    findings_backend_authored: bool = False,
    category: str,
    step_ids: Sequence[str],
    note: str | None,
) -> ValidationResult:
    """Gate COMPLETION only, on a pipeline whose validation genuinely passed.

    R2-F14 (elspeth-5403f346c0). ``advisor_signoff_blocked_validation`` zeroes
    every readiness axis, which is right when the pipeline is actually broken
    and wrong when it is not: an advisor that never rendered a verdict says
    nothing about whether the build validates. Reporting a green build as
    authoring-invalid AND execution-unready (under a "Runtime preflight
    failed" header) is a false statement about the user's pipeline.

    So when ``validate_pipeline`` is green and only the sign-off is missing,
    the validated result is carried through unchanged — ``is_valid``,
    ``errors``, ``authoring_valid`` and ``execution_ready`` all stay as
    validation found them — and ONLY ``completion_ready`` is withheld, with an
    ``advisor_signoff_blocked`` blocker and a failed ``advisor_signoff`` check
    naming why. The turn is still not "complete"; it is simply no longer
    mislabelled as a validation failure.

    Applies to every advisor reason. This release's authority decision is
    completion-only: an advisor FLAG does not make execution unsafe.
    """
    detail, suggestion = advisor_signoff_blocked_wording(
        reason=reason,
        findings=findings,
        findings_backend_authored=findings_backend_authored,
        category=category,
        step_ids=step_ids,
    )
    return base.model_copy(
        update={
            "checks": [
                *base.checks,
                ValidationCheck(
                    name=_ADVISOR_SIGNOFF_BLOCKED_CHECK_NAME,
                    passed=False,
                    detail=detail,
                    affected_nodes=(),
                    outcome_code=None,
                ),
            ],
            "readiness": ValidationReadiness(
                authoring_valid=base.readiness.authoring_valid,
                execution_ready=base.readiness.execution_ready,
                completion_ready=False,
                blockers=[
                    *base.readiness.blockers,
                    ValidationReadinessBlocker(
                        code=_ADVISOR_SIGNOFF_BLOCKED_CODE,
                        suggestion=suggestion,
                        note=note,
                        component_id="pipeline",
                        component_type="pipeline",
                        detail=detail,
                    ),
                ],
            ),
        }
    )


def advisor_signoff_pending_handoff_validation(
    base: ValidationResult,
    *,
    reason: str,
    findings: str,
    findings_backend_authored: bool = False,
) -> ValidationResult:
    """Record the advisor verdict ADDITIVELY on a resolvable pending handoff.

    elspeth-66717f0c99. The pending-interpretation-handoff shape is the third
    thing the END gate's preflight can be, and it is the one the other two
    builders get wrong: ``advisor_signoff_blocked_validation`` replaces it
    with an all-red result whose only blocker is the advisor's, destroying the
    ``interpretation_review_pending`` blocker that tells every consumer a
    RESOLVABLE review card is waiting — and telling the operator the build
    failed validation, which is false, because authoring validated.

    So the base is carried through and only the failed ``advisor_signoff``
    check is appended. Readiness is untouched, deliberately including
    ``completion_ready=True``: it is the load-bearing axis of BOTH
    ``is_pending_interpretation_handoff`` and ``ComposerResult``'s own pending
    carve-out, so withholding it would reproduce the defect one level down —
    every discriminator consumer would revert to seeing plain red. The turn is
    not thereby announced complete: it is deferred to a user-action boundary,
    ``execution_ready`` stays False so execute()'s gate still blocks, and a
    fresh advisor pass runs on the next compose request.

    No advisor BLOCKER is appended for the same reason. The verdict's audit
    evidence rides the appended check plus the withheld-prose disclosure the
    gate has already persisted.

    The check detail comes from ``_advisor_signoff_pending_handoff_wording``
    rather than the shared ``advisor_signoff_blocked_wording``, whose text
    asserts completion is withheld — a claim this result's own readiness
    contradicts.
    """
    detail = _advisor_signoff_pending_handoff_wording(
        reason=reason,
        findings=findings,
        findings_backend_authored=findings_backend_authored,
    )
    return base.model_copy(
        update={
            "checks": [
                *base.checks,
                ValidationCheck(
                    name=_ADVISOR_SIGNOFF_BLOCKED_CHECK_NAME,
                    passed=False,
                    detail=detail,
                    affected_nodes=(),
                    outcome_code=None,
                ),
            ],
        }
    )
