"""No-tool finalization policy and user-facing augmentation messages."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Final, Literal

from opentelemetry import metrics

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.provider_rejection import ReasonDisposition, RequestRejectionReason, SafeRequestRejection
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools import is_discovery_tool
from elspeth.web.composer.turn_effects import CHECKPOINT_SCOPE_TEMPLATES, TurnEvidence
from elspeth.web.execution.schemas import (
    CHECK_STATE_EXISTS,
    ValidationCheck,
    ValidationError,
    ValidationReadiness,
    ValidationReadinessBlocker,
    ValidationResult,
)
from elspeth.web.interpretation_state import INTERPRETATION_REVIEW_PENDING_CODE

_COMPOSER_AUDIT_INTEGRITY_VIOLATION_COUNTER = metrics.get_meter(__name__).create_counter(
    "composer.audit_integrity_violation.total",
    description="Producer-side audit-integrity invariant violations (augmentation/replacement prefix contracts)",
)

_TRUSTED_NOTICE_SEPARATOR: Final = "\n\n---\n\n"
_TRUSTED_NOTICE_MARKER: Final = "[ELSPETH-SYSTEM] "
_CHECKPOINT_RECEIPT_LABEL: Final = "Checkpoint save status: "
_CHECKPOINT_RECEIPT_REGION_PREFIX: Final = "\n\n" + _CHECKPOINT_RECEIPT_LABEL
_CHECKPOINT_RECEIPT_END: Final = " Checkpoint evidence does not establish task completion."
# len(tuple) is bounded by Py_ssize_t. The closed decimal grammar also refuses
# signs, leading zeroes, Unicode digits and unbounded conversion work.
_CHECKPOINT_COUNT_PATTERN: Final = re.compile(r"\b(?:0|[1-9][0-9]{0,18})\b")
_CHECKPOINT_SCOPE_GRAMMAR: Final = (
    "(?:"
    + "|".join(re.escape(template).replace(re.escape("{count}"), r"(?:0|[1-9][0-9]{0,18})") for template in CHECKPOINT_SCOPE_TEMPLATES)
    + ")"
)
_CHECKPOINT_RECEIPT_PATTERN: Final = re.compile(
    rf"(?:(?P<base>.+){re.escape(_CHECKPOINT_RECEIPT_REGION_PREFIX)}|"
    rf"{re.escape(_TRUSTED_NOTICE_SEPARATOR + _TRUSTED_NOTICE_MARKER + _CHECKPOINT_RECEIPT_LABEL)})"
    rf"(?P<scope>{_CHECKPOINT_SCOPE_GRAMMAR}){re.escape(_CHECKPOINT_RECEIPT_END)}",
    re.DOTALL,
)


def _wrapped_diagnostic_wire_shape(header: str, footer: str) -> tuple[str, str]:
    """Single source of the trusted wrapped-diagnostic wire shape.

    Returns the exact ``(prefix, postfix)`` pair that brackets the untrusted
    diagnostic. Every producer template below and the recognizer
    (``_split_wrapped_diagnostic``) derive from this one function, so a
    producer can never emit a wrapped notice whose byte shape the recognizer
    re-derives differently — that drift would demote the entire trusted
    suffix to a single untrusted segment (fail-closed, but invisibly).
    """
    return (
        _TRUSTED_NOTICE_SEPARATOR + _TRUSTED_NOTICE_MARKER + header + "\n\nCause: ",
        "\n\n" + footer,
    )


def _wrapped_diagnostic_template(header: str, footer: str, *, diagnostic_slots: str = "{detail}") -> str:
    """Producer-side ``str.format`` template for one wrapped-diagnostic shape.

    ``diagnostic_slots`` is the format-slot text occupying the untrusted
    diagnostic region between the trusted prefix and postfix.
    """
    prefix, postfix = _wrapped_diagnostic_wire_shape(header, footer)
    return prefix + diagnostic_slots + postfix


def _bare_trusted_suffix(notice: str) -> str:
    """Single constructor for BARE (non-diagnostic) canonical finalize suffixes.

    The wrapped-diagnostic family derives producer templates and recognizer
    from one wire shape; this is the bare family's counterpart. Every bare
    canonical suffix must be assembled through this call — the completeness
    gate in ``test_no_tool_policy_segments`` discovers registrations by AST
    over these call sites, so a hand-concatenated suffix has no completeness
    proof and repeats elspeth-2ed41f0a4a R2 (an unregistered backend notice
    rendered as model-attributable prose).
    """
    return _TRUSTED_NOTICE_SEPARATOR + _TRUSTED_NOTICE_MARKER + notice


_GRAPH_RECOVERY_REFUSAL_NOTICE: Final = (
    "The requested graph recovery did not complete. Choose another saved draft or clarify how to continue."
)
_GRAPH_RECOVERY_REFUSAL_SUFFIX = _bare_trusted_suffix(_GRAPH_RECOVERY_REFUSAL_NOTICE)


def compose_graph_recovery_refusal_message() -> str:
    """Publish a terminal recovery refusal without prospective success prose.

    The caller retains provider text in the tool/call audit and persists this
    message with ``raw_content=''``. No provider-authored reply is attributed
    to this fixed, provenance-bearing system notice.
    """
    return _GRAPH_RECOVERY_REFUSAL_SUFFIX


_REVIEW_REPLY_UNAVAILABLE_NOTICE: Final = "The model's final reply is unavailable. Please retry your question."
_REVIEW_REPLY_UNAVAILABLE_SUFFIX = _bare_trusted_suffix(_REVIEW_REPLY_UNAVAILABLE_NOTICE)
_ADVISOR_REVIEW_BUDGET_NOTICE: Final = (
    "The reply was withheld because the completion review could not run within the existing request limits. "
    "Saved draft progress remains available; continue in a new message."
)
_ADVISOR_REVIEW_BUDGET_SUFFIX = _bare_trusted_suffix(_ADVISOR_REVIEW_BUDGET_NOTICE)


def compose_review_reply_unavailable_message(content: str) -> str:
    """Keep a missing provider reply distinct from model-authored prose."""
    return content + _REVIEW_REPLY_UNAVAILABLE_SUFFIX


_EMPTY_STATE_NOTICE_HEADER: Final = "The pipeline is still empty — the composer did not complete a valid build this turn."
_EMPTY_STATE_NOTICE_NEXT_STEP: Final = (
    "To continue: refine your request with more specifics, or reply telling the composer to retry with the plan it described above."
)
_EMPTY_STATE_NOTICE_BODY: Final = f"{_EMPTY_STATE_NOTICE_HEADER} {_EMPTY_STATE_NOTICE_NEXT_STEP}"
_EMPTY_STATE_FINALIZE_SUFFIX = _bare_trusted_suffix(_EMPTY_STATE_NOTICE_BODY)
_EMPTY_STATE_FINALIZE_SUFFIX_WITH_BLOCKER = _wrapped_diagnostic_template(
    _EMPTY_STATE_NOTICE_HEADER,
    _EMPTY_STATE_NOTICE_NEXT_STEP,
    diagnostic_slots="{blocker}",
)

_CLARIFICATION_NOTICE: Final = "No pipeline has been built yet. Answer the clarification above to continue."
_CLARIFICATION_FINALIZE_SUFFIX = _bare_trusted_suffix(_CLARIFICATION_NOTICE)

_PREFLIGHT_NOTICE_HEADER: Final = "Runtime preflight failed before this build could be marked complete."
_PREFLIGHT_NOTICE_FOOTER: Final = "The composer's analysis above is preserved verbatim; the validator's objection is recorded here."
_PREFLIGHT_INVALID_NONEMPTY_FINALIZE_SUFFIX_WITH_DETAIL = _wrapped_diagnostic_template(
    _PREFLIGHT_NOTICE_HEADER,
    _PREFLIGHT_NOTICE_FOOTER,
    diagnostic_slots="{detail}{suggestion_block}",
)
_PREFLIGHT_INVALID_NONEMPTY_FINALIZE_SUFFIX_BARE = _bare_trusted_suffix(_PREFLIGHT_NOTICE_HEADER + "\n\n" + _PREFLIGHT_NOTICE_FOOTER)

# R2-F14 (elspeth-5403f346c0): the advisor end gate used to borrow
# ``_PREFLIGHT_NOTICE_HEADER`` for a completion review that never rendered, telling the
# user "Runtime preflight failed" on a build whose preflight had just PASSED
# (green side rail, contradictory note). This notice is the honest alternative:
# it states what actually happened and what to do, and — unlike the preflight
# wrapper — carries no interpolated diagnostic, so the whole suffix is fixed
# operator-authored prose.
# elspeth-ff4f0068a4: every advisor-cohort terminal replaces the model's
# prose with fixed backend copy, so a turn in which the composer explained
# that part of the user's request could not be applied (live case: one served
# LLM profile, the user asked for two different LLMs) published nothing that
# said so — the user learned it a full turn later. The model-facing withheld
# disclosure already carries this fact into later provider context; this is
# the USER-facing counterpart, one fixed operator-authored sentence appended
# to each terminal that withholds prose. The transient repair status line
# ("ELSPETH is applying a pipeline correction.") is deliberately excluded:
# the terminal is where the user decides what the turn did.
ADVISOR_PROSE_WITHHELD_PUBLIC_DISCLOSURE: Final = (
    "ELSPETH withheld the composer's own summary of this exchange; verify the pipeline before assuming every requested change was applied."
)

# The END-gate blocked notices below are ``_PUBLISHED_``: a blocked turn
# publishes the composer's reply beside the notice (ruling 2026-09-22,
# elspeth-032ec69c41), so none of them may claim the reply was withheld. Only
# the advisor-repair replacer (``service._replace_advisor_repair_public_result``)
# still replaces the model's prose, and two of its branches reuse a blocked
# notice: sign-off pending and pending handoff. Those two alone also exist in a
# withheld form, DEFINED as the published notice + the withheld-prose
# disclosure so the pair cannot drift.
_ADVISOR_SIGNOFF_PENDING_PUBLISHED_NOTICE: Final = (
    "Completion advisory review did not clear after the available attempts. "
    "Composer completion is withheld. Review the pipeline; validation and the advisory review run again after your next pipeline change."
)
_ADVISOR_SIGNOFF_PENDING_NOTICE: Final = _ADVISOR_SIGNOFF_PENDING_PUBLISHED_NOTICE + " " + ADVISOR_PROSE_WITHHELD_PUBLIC_DISCLOSURE
_ADVISOR_SIGNOFF_PENDING_FINALIZE_SUFFIX = _bare_trusted_suffix(_ADVISOR_SIGNOFF_PENDING_NOTICE)
_ADVISOR_SIGNOFF_PENDING_PUBLISHED_FINALIZE_SUFFIX = _bare_trusted_suffix(_ADVISOR_SIGNOFF_PENDING_PUBLISHED_NOTICE)

# elspeth-2ae50afcd1 facet B (operator-adjudicated 2026-09-02): the FOURTH
# preflight shape the END advisor gate can hold — ``runtime_preflight is
# None``, i.e. no deterministic validation ran this turn (a question-only turn
# mutates nothing, so ``_turn_runtime_preflight`` never computes one). The
# preflight header would assert a failure that never occurred — the same
# dishonest-header class R2-F14 closed for the green shape, observed live on a
# build whose deterministic validation was green — and the sign-off-pending
# notice would stay silent about readiness where the repair cohort's sibling
# (``ADVISOR_REPAIR_UNVERIFIED_PUBLIC_MESSAGE``, elspeth-88592f5be7) already
# states it plainly. Fail-closed STRUCTURE is unchanged (every readiness axis
# stays withheld); only the wording stops claiming a preflight ran.
#
# Ruling 2026-09-22 (elspeth-032ec69c41): the END gate stands aside for an
# unchanged graph whose prior state row carries a GRAPH REJECTION, so that
# block is re-reviewed only after the next pipeline change — every rendered-
# flag notice says so, this ABSENT one included. The ABSENT shape arises only
# on an unchanged turn, but since 41aeaeac0 an advisor decision is persisted
# even when the graph did not move, so the next unchanged message meets the
# skip. (Unrendered verdicts and message rejections are re-reviewed on the
# next message; they carry their own notices.)
_ADVISOR_SIGNOFF_UNVERIFIED_PUBLISHED_NOTICE: Final = (
    "Completion advisory review did not clear after the available attempts. "
    "Composer completion is withheld. Pipeline readiness was not re-verified this turn; "
    "validation and the advisory review run again after your next pipeline change."
)
_ADVISOR_SIGNOFF_UNVERIFIED_PUBLISHED_FINALIZE_SUFFIX = _bare_trusted_suffix(_ADVISOR_SIGNOFF_UNVERIFIED_PUBLISHED_NOTICE)

# The two notices above say the review "did not clear", which is true only
# when the advisor RENDERED a verdict (a FLAG). The red shape already splits
# its footer by verdict class (elspeth-b61894d93d); the green and absent
# shapes did not, so an advisor outage on a validated build told the user the
# review did not clear and to "Review the pipeline" — a pipeline with nothing
# wrong — while the real remedy reached no published surface on green. These
# are the unrendered-verdict (unavailable / malformed) counterparts. Fixed
# operator-authored prose: no advisor output is interpolated (R2-F13), and the
# absent variants keep the facet B discipline of making no readiness claim.
_ADVISOR_SIGNOFF_UNAVAILABLE_CAUSE: Final = (
    "Completion advisory review could not be obtained: the advisor model was unavailable. Composer completion is withheld."
)
_ADVISOR_SIGNOFF_MALFORMED_CAUSE: Final = (
    "Completion advisory review could not be obtained: the advisor returned no usable verdict. Composer completion is withheld."
)
# Provider failures are retried on a later turn even when the graph is unchanged.
_ADVISOR_SIGNOFF_UNRENDERED_VERIFIED_NEXT_STEP: Final = (
    "Retry the request, or check the advisor model configuration; validation and the advisory review run again on your next message."
)
_ADVISOR_SIGNOFF_UNRENDERED_UNVERIFIED_NEXT_STEP: Final = (
    "Retry the request, or check the advisor model configuration; validation and the advisory review run again on your next message."
)
_ADVISOR_SIGNOFF_READINESS_NOT_REVERIFIED_CLAUSE: Final = "Pipeline readiness was not re-verified this turn."
# GREEN shape (the preflight ran and passed).
_ADVISOR_SIGNOFF_UNAVAILABLE_PENDING_PUBLISHED_NOTICE: Final = (
    _ADVISOR_SIGNOFF_UNAVAILABLE_CAUSE + " " + _ADVISOR_SIGNOFF_UNRENDERED_VERIFIED_NEXT_STEP
)
_ADVISOR_SIGNOFF_UNAVAILABLE_PENDING_PUBLISHED_FINALIZE_SUFFIX = _bare_trusted_suffix(_ADVISOR_SIGNOFF_UNAVAILABLE_PENDING_PUBLISHED_NOTICE)
_ADVISOR_SIGNOFF_MALFORMED_PENDING_PUBLISHED_NOTICE: Final = (
    _ADVISOR_SIGNOFF_MALFORMED_CAUSE + " " + _ADVISOR_SIGNOFF_UNRENDERED_VERIFIED_NEXT_STEP
)
_ADVISOR_SIGNOFF_MALFORMED_PENDING_PUBLISHED_FINALIZE_SUFFIX = _bare_trusted_suffix(_ADVISOR_SIGNOFF_MALFORMED_PENDING_PUBLISHED_NOTICE)
# ABSENT shape (no preflight ran this turn).
_ADVISOR_SIGNOFF_UNAVAILABLE_UNVERIFIED_PUBLISHED_NOTICE: Final = (
    _ADVISOR_SIGNOFF_UNAVAILABLE_CAUSE
    + " "
    + _ADVISOR_SIGNOFF_READINESS_NOT_REVERIFIED_CLAUSE
    + " "
    + _ADVISOR_SIGNOFF_UNRENDERED_UNVERIFIED_NEXT_STEP
)
_ADVISOR_SIGNOFF_UNAVAILABLE_UNVERIFIED_PUBLISHED_FINALIZE_SUFFIX = _bare_trusted_suffix(
    _ADVISOR_SIGNOFF_UNAVAILABLE_UNVERIFIED_PUBLISHED_NOTICE
)
_ADVISOR_SIGNOFF_MALFORMED_UNVERIFIED_PUBLISHED_NOTICE: Final = (
    _ADVISOR_SIGNOFF_MALFORMED_CAUSE
    + " "
    + _ADVISOR_SIGNOFF_READINESS_NOT_REVERIFIED_CLAUSE
    + " "
    + _ADVISOR_SIGNOFF_UNRENDERED_UNVERIFIED_NEXT_STEP
)
_ADVISOR_SIGNOFF_MALFORMED_UNVERIFIED_PUBLISHED_FINALIZE_SUFFIX = _bare_trusted_suffix(
    _ADVISOR_SIGNOFF_MALFORMED_UNVERIFIED_PUBLISHED_NOTICE
)

# elspeth-25f7b757e7 (A1, budget displacement): published when the END gate's
# deterministic pre-scan FLAGGED the USER'S OWN chat message — the one
# advisory-evidence surface no composer tool call can mutate. Granting the
# model a repair-continue there injects an instruction it cannot satisfy,
# re-fires the identical pre-scan on the next pass, and spends the whole
# advisory budget without the LLM advisory review ever running; the gate now
# terminal-blocks on the first pass instead, and this copy names the one
# action that clears the block. Fixed operator-authored prose: the flagged
# message text is never interpolated (the notice's information content
# derives entirely from control state).
#
# Fix round 1 (N1): the copy is SHAPE-AWARE, mirroring the gate's four
# preflight arms — one uniform notice asserted "No pipeline change is
# needed" over a red, absent, or pending-handoff preflight, the same
# false-pipeline-claim class R2-F14 / facet B / ac85b0ab0e each closed on
# one shape. The shared header states only what the gate knows on every
# shape; each variant adds only what its own preflight shape supports.
_ADVISOR_SIGNOFF_UNREPAIRABLE_HEADER: Final = (
    "Completion advisory review flagged this turn's chat message: it matched the advisor-instruction "
    "injection screen, and a chat message is not something the composer can change. Composer completion "
    "is withheld."
)
# GREEN — the only shape where the affirmative pipeline claim is true.
_ADVISOR_SIGNOFF_UNREPAIRABLE_PUBLISHED_NOTICE: Final = (
    _ADVISOR_SIGNOFF_UNREPAIRABLE_HEADER + " No pipeline change is needed — reword your message and resend."
)
_ADVISOR_SIGNOFF_UNREPAIRABLE_PUBLISHED_FINALIZE_SUFFIX = _bare_trusted_suffix(_ADVISOR_SIGNOFF_UNREPAIRABLE_PUBLISHED_NOTICE)
# ABSENT — no preflight ran this turn, so no pipeline claim at all (the
# facet B discipline: unknown readiness is stated as unknown).
_ADVISOR_SIGNOFF_UNREPAIRABLE_UNVERIFIED_PUBLISHED_NOTICE: Final = (
    _ADVISOR_SIGNOFF_UNREPAIRABLE_HEADER
    + " Pipeline readiness was not re-verified this turn. Reword your message and resend; validation and "
    "the advisory review run again on your next message."
)
_ADVISOR_SIGNOFF_UNREPAIRABLE_UNVERIFIED_PUBLISHED_FINALIZE_SUFFIX = _bare_trusted_suffix(
    _ADVISOR_SIGNOFF_UNREPAIRABLE_UNVERIFIED_PUBLISHED_NOTICE
)
# PENDING HANDOFF — a required interpretation review is still outstanding,
# so rewording is never the only remaining step (the ac85b0ab0e class).
_ADVISOR_SIGNOFF_UNREPAIRABLE_HANDOFF_PUBLISHED_NOTICE: Final = (
    _ADVISOR_SIGNOFF_UNREPAIRABLE_HEADER
    + " A required interpretation review is also pending, so rewording is not the only remaining step: "
    "reword your message and resend, then resolve the pending review cards."
)
_ADVISOR_SIGNOFF_UNREPAIRABLE_HANDOFF_PUBLISHED_FINALIZE_SUFFIX = _bare_trusted_suffix(
    _ADVISOR_SIGNOFF_UNREPAIRABLE_HANDOFF_PUBLISHED_NOTICE
)
# RED — the validator's leading objection rides the untrusted ``Cause:``
# region (same trust class and treatment as the preflight wrapper's
# interpolation): rewording a chat message cannot fix a broken pipeline, so
# hiding the objection would tell the user with a broken pipeline there is
# nothing to fix.
_ADVISOR_SIGNOFF_UNREPAIRABLE_RED_PUBLISHED_FOOTER: Final = (
    "Rewording alone will not make this pipeline runnable — fix the validation failure above as well."
)
_ADVISOR_SIGNOFF_UNREPAIRABLE_RED_PUBLISHED_SUFFIX_WITH_DETAIL = _wrapped_diagnostic_template(
    _ADVISOR_SIGNOFF_UNREPAIRABLE_HEADER,
    _ADVISOR_SIGNOFF_UNREPAIRABLE_RED_PUBLISHED_FOOTER,
    diagnostic_slots="{detail}{suggestion_block}",
)
_ADVISOR_SIGNOFF_UNREPAIRABLE_RED_PUBLISHED_SUFFIX_BARE = _bare_trusted_suffix(
    _ADVISOR_SIGNOFF_UNREPAIRABLE_HEADER + "\n\n" + _ADVISOR_SIGNOFF_UNREPAIRABLE_RED_PUBLISHED_FOOTER
)

# elspeth-b61894d93d: the advisor-blocked RED terminals for the OTHER three
# reasons (flagged_final_pass/flagged_no_repair, unavailable, malformed) used
# to compose their chat copy from the SYNTHESIZED advisor-signoff validation,
# so the ``Cause:`` interior carried the advisor wording and the validator's
# leading objection appeared on no published surface — the user with a
# genuinely broken pipeline was told only that the review did not clear.
# Same treatment as the unrepairable red shape: the truthful preflight header
# stays (it is the pre-existing R2-F14-scoped claim, and it is true), the
# validator's objection rides the untrusted ``Cause:`` interior, and the
# advisory situation rides a per-reason-class footer — the flagged footer
# keeps the did-not-clear framing, the unrendered footer keeps the
# could-not-be-obtained framing, because each is true only for its class.
_ADVISOR_SIGNOFF_FLAGGED_RED_PUBLISHED_FOOTER: Final = (
    "The completion advisory review also did not clear after the available attempts, so composer "
    "completion is withheld. Fix the validation failure above; validation and the advisory review run "
    "again after your next pipeline change."
)
_ADVISOR_SIGNOFF_FLAGGED_RED_PUBLISHED_SUFFIX_WITH_DETAIL = _wrapped_diagnostic_template(
    _PREFLIGHT_NOTICE_HEADER,
    _ADVISOR_SIGNOFF_FLAGGED_RED_PUBLISHED_FOOTER,
    diagnostic_slots="{detail}{suggestion_block}",
)
_ADVISOR_SIGNOFF_FLAGGED_RED_PUBLISHED_SUFFIX_BARE = _bare_trusted_suffix(
    _PREFLIGHT_NOTICE_HEADER + "\n\n" + _ADVISOR_SIGNOFF_FLAGGED_RED_PUBLISHED_FOOTER
)
_ADVISOR_SIGNOFF_UNRENDERED_RED_PUBLISHED_FOOTER: Final = (
    "The evidence-scoped completion advisory review could also not be obtained, so the Composer cannot "
    "mark this turn complete. Fix the validation failure above; validation and the advisory review run "
    "again on your next message."
)
_ADVISOR_SIGNOFF_UNRENDERED_RED_PUBLISHED_SUFFIX_WITH_DETAIL = _wrapped_diagnostic_template(
    _PREFLIGHT_NOTICE_HEADER,
    _ADVISOR_SIGNOFF_UNRENDERED_RED_PUBLISHED_FOOTER,
    diagnostic_slots="{detail}{suggestion_block}",
)
_ADVISOR_SIGNOFF_UNRENDERED_RED_PUBLISHED_SUFFIX_BARE = _bare_trusted_suffix(
    _PREFLIGHT_NOTICE_HEADER + "\n\n" + _ADVISOR_SIGNOFF_UNRENDERED_RED_PUBLISHED_FOOTER
)

# elspeth-66717f0c99: the third preflight shape the END advisor gate can hold —
# a resolvable pending-interpretation handoff. It is neither green (so
# ``_ADVISOR_SIGNOFF_PENDING_NOTICE`` is wrong: completion is NOT withheld
# there, the review card is what the turn is deferred to) nor genuinely red (so
# ``_PREFLIGHT_NOTICE_HEADER`` is wrong: authoring validated). This notice
# names BOTH outstanding facts. It deliberately does not say the review is the
# only remaining step, and it interpolates nothing, so the whole suffix is
# fixed operator-authored prose. When the masked re-validation
# (elspeth-5a372d3267) has established that the review is NOT the only
# remaining step, the qualified wrapped shape below names the outstanding
# validator objection alongside it (elspeth-ac85b0ab0e).
_ADVISOR_SIGNOFF_PENDING_HANDOFF_PUBLISHED_NOTICE: Final = (
    "A required interpretation review is pending, and the completion advisory review did not clear "
    "after the available attempts. Resolve the pending review cards; validation and the advisory "
    "review both run again on the next request."
)
_ADVISOR_SIGNOFF_PENDING_HANDOFF_NOTICE: Final = (
    _ADVISOR_SIGNOFF_PENDING_HANDOFF_PUBLISHED_NOTICE + " " + ADVISOR_PROSE_WITHHELD_PUBLIC_DISCLOSURE
)
_ADVISOR_SIGNOFF_PENDING_HANDOFF_FINALIZE_SUFFIX = _bare_trusted_suffix(_ADVISOR_SIGNOFF_PENDING_HANDOFF_NOTICE)
_ADVISOR_SIGNOFF_PENDING_HANDOFF_PUBLISHED_FINALIZE_SUFFIX = _bare_trusted_suffix(_ADVISOR_SIGNOFF_PENDING_HANDOFF_PUBLISHED_NOTICE)

# elspeth-ac85b0ab0e: the qualified variant of the pending-handoff notice.
# Battery round 7 (g03, run 700e19d5) terminated on the bare notice above
# while the persisted state carried an edge-contract violation with a repair
# suggestion — the strict ledger halts at ``review_interpretations`` before
# the graph/schema stages, so the bare notice implied the review cards were
# the only remaining step. When the authoring-masked re-validation
# (elspeth-5a372d3267) finds failures behind the handoff, this wrapped shape
# names them: the fixed handoff notice as trusted header, the validator's
# objection as an untrusted ``Cause:`` segment, and a fixed footer stating
# the review alone will not make the pipeline runnable. Built from the same
# ``_wrapped_diagnostic_wire_shape`` that ``_split_wrapped_diagnostic``
# consumes, so the recognizer accepts it by construction.
_ADVISOR_SIGNOFF_PENDING_HANDOFF_FINDINGS_FOOTER: Final = (
    "Resolving the review cards alone will not make this pipeline runnable — fix the validation failure above as well."
)
_ADVISOR_SIGNOFF_PENDING_HANDOFF_FINDINGS_SUFFIX_WITH_DETAIL = _wrapped_diagnostic_template(
    _ADVISOR_SIGNOFF_PENDING_HANDOFF_NOTICE,
    _ADVISOR_SIGNOFF_PENDING_HANDOFF_FINDINGS_FOOTER,
)
_ADVISOR_SIGNOFF_PENDING_HANDOFF_PUBLISHED_FINDINGS_SUFFIX_WITH_DETAIL = _wrapped_diagnostic_template(
    _ADVISOR_SIGNOFF_PENDING_HANDOFF_PUBLISHED_NOTICE,
    _ADVISOR_SIGNOFF_PENDING_HANDOFF_FINDINGS_FOOTER,
)

# elspeth-2ed41f0a4a R2: the NO-TOOL finalize tail's own pending-review
# announcement — the advisor-path notices above are its twins, reached when the
# END advisor gate ALSO failed to clear. This one announces pending review
# and conditions execution on remaining checks without claiming their outcome.
#
# It used to be hand-assembled in ``service.py`` and joined to the prose with a
# bare ``"\n\n"``: no ``_TRUSTED_NOTICE_SEPARATOR``, no ``_TRUSTED_NOTICE_MARKER``,
# and no entry in ``_canonical_trusted_suffix_segments``. Registration in the
# ``_AugmentationBranch`` literal covers the PREFIX invariant only, not the
# segment recognizer, so ``visible_message_segments`` fell through to its
# fail-closed arm and published this backend-authored disclosure as one
# ``AssistantTextSegment`` — model prose, in the operator's view, saying a
# review was staged. Both variants are canonical shapes now: the bare notice as
# fixed operator-authored prose, and the qualified variant as a wrapped
# diagnostic whose ``Cause:`` region carries the VALIDATOR-authored objection
# from the authoring-masked re-validation (same trust class, and the same
# treatment, as the preflight wrapper's interpolation).
_INTERPRETATION_REVIEW_HANDOFF_NOTICE: Final = (
    "Interpretation review cards are ready for this pipeline. Review the pending assumptions to continue. "
    "Approval does not establish execution readiness; remaining validation and required advisor checks must pass before execution."
)
_INTERPRETATION_REVIEW_HANDOFF_FINALIZE_SUFFIX = _bare_trusted_suffix(_INTERPRETATION_REVIEW_HANDOFF_NOTICE)
_INTERPRETATION_REVIEW_HANDOFF_FINDINGS_FOOTER: Final = (
    "Validation also found issues that must be fixed before this pipeline can run; "
    "resolving the review cards alone will not make this pipeline runnable."
)
_INTERPRETATION_REVIEW_HANDOFF_FINDINGS_SUFFIX_WITH_DETAIL = _wrapped_diagnostic_template(
    _INTERPRETATION_REVIEW_HANDOFF_NOTICE,
    _INTERPRETATION_REVIEW_HANDOFF_FINDINGS_FOOTER,
    # Keep the preflight wrapper's detail/suggestion pair when the staged-review
    # branch replaces that wrapper on its cross-turn red arm. Persisted runtime
    # errors omit ValidationError.suggestion. Reload backfills the decision
    # panel's separate Stage-1 suggestions; it does not restore this runtime
    # suggestion, so the handoff notice must retain it.
    diagnostic_slots="{detail}{suggestion_block}",
)

# The check-detail counterpart of that notice. ``_advisor_signoff_blocked_wording``
# says completion is withheld / cannot be marked complete — true on both
# surfaces it serves, FALSE on the preserved pending handoff, whose
# ``completion_ready`` stays True. A check detail is a diagnostic rather than a
# readiness claim, but it is a surfaced field, so the contradiction is dropped
# here too: same reason classes, same findings rule, no completion claim.
_ADVISOR_SIGNOFF_PENDING_HANDOFF_FLAGGED_DETAIL: Final = (
    "The evidence-scoped completion advisory review flagged this build and did not clear after the "
    "available attempts. The pending interpretation review remains resolvable; the advisory review "
    "runs again on the next request."
)
_ADVISOR_SIGNOFF_PENDING_HANDOFF_UNRENDERED_DETAIL: Final = (
    "The evidence-scoped completion advisory review could not be obtained. The pending interpretation "
    "review remains resolvable; the advisory review runs again on the next request."
)
# Fix round 1 (N1): the ``flagged_unrepairable`` reason used to fall through
# to the could-not-be-obtained arm above, producing a detail that claimed the
# review was never obtained with the FLAGGED finding appended to the same
# sentence — the R2-F14 self-contradiction class. The review WAS obtained: it
# deterministically flagged the user's own chat message.
_ADVISOR_SIGNOFF_PENDING_HANDOFF_UNREPAIRABLE_DETAIL: Final = (
    "The evidence-scoped completion advisory review flagged this turn's chat message and did not clear. "
    "The pending interpretation review remains resolvable; reword your message and resend."
)

# Primary-model prose produced after hidden advisor findings entered its
# context is not an operator transcript.  The model may quote, paraphrase, or
# rebut those findings even after the pipeline is repaired and the next
# checkpoint returns CLEAN.  These fixed backend-authored messages are the
# only prose published for that repair cohort; the state, tool calls, provider
# audit, and deterministic validation result remain unchanged.
ADVISOR_REPAIR_INTERMEDIATE_PUBLIC_MESSAGE: Final = "ELSPETH is applying a pipeline correction."
ADVISOR_REPAIR_SUCCESS_PUBLIC_MESSAGE: Final = "The pipeline is configured and ready. " + ADVISOR_PROSE_WITHHELD_PUBLIC_DISCLOSURE
ADVISOR_REPAIR_REVIEW_PUBLIC_MESSAGE: Final = (
    "The pipeline update is ready for the required review. Resolve the pending review before running it. "
    + ADVISOR_PROSE_WITHHELD_PUBLIC_DISCLOSURE
)
# elspeth-5a372d3267: published instead of the bare review message when the
# authoring-masked re-validation behind a pending-review handoff found
# failures in the stages the strict ledger never reached — "ready for the
# required review" would falsely imply the review is the only thing between
# the user and execution.
# The disclosure sits BEFORE the interpolated validator detail so the fixed
# operator-authored sentence is never visually attributed to the untrusted
# diagnostic tail.
ADVISOR_REPAIR_REVIEW_WITH_FINDINGS_PUBLIC_MESSAGE: Final = (
    "A required interpretation review is pending. "
    + ADVISOR_PROSE_WITHHELD_PUBLIC_DISCLOSURE
    + " Validation also found issues that must be fixed before this pipeline can run: {detail}"
)
# elspeth-88592f5be7: published when the advisor-repair turn ends with
# ``runtime_preflight=None`` — no deterministic validation ran this turn, so
# readiness is UNKNOWN, not ready. ``None`` is documented as fail-closed
# unknown by the END advisor gate; this surface must not read the same
# sentinel as an affirmative readiness claim.
ADVISOR_REPAIR_UNVERIFIED_PUBLIC_MESSAGE: Final = (
    "ELSPETH completed the advisor repair turn. Pipeline readiness was not determined this turn; "
    "run validation to confirm the pipeline state. " + ADVISOR_PROSE_WITHHELD_PUBLIC_DISCLOSURE
)

_AugmentationBranch = Literal[
    "clarification_augmentation",
    "no_mutation_empty_state_augmentation",
    "preflight_invalid_empty_state_augmentation",
    "preflight_invalid_non_empty_state_augmentation",
    "orphaned_interpretation_review_augmentation",
    "advisor_signoff_blocked_augmentation",
    "interpretation_review_handoff_augmentation",
    "proof_repair_exhausted_augmentation",
    "checkpoint_receipt_augmentation",
]


@dataclass(frozen=True, slots=True)
class AssistantTextSegment:
    """Untrusted assistant prose rendered with ordinary message semantics."""

    kind: ClassVar[Literal["text"]] = "text"
    content: str

    def __post_init__(self) -> None:
        if type(self.content) is not str:
            raise TypeError("AssistantTextSegment.content must be an exact string")


@dataclass(frozen=True, slots=True)
class TrustedSystemNoticeSegment:
    """Operator-authored suffix authenticated by the durable synthesis pairing."""

    kind: ClassVar[Literal["trusted_system_notice"]] = "trusted_system_notice"
    content: str

    def __post_init__(self) -> None:
        if type(self.content) is not str or not self.content:
            raise TypeError("TrustedSystemNoticeSegment.content must be a non-empty exact string")


type VisibleMessageSegment = AssistantTextSegment | TrustedSystemNoticeSegment


def _split_wrapped_diagnostic(
    suffix: str,
    *,
    header: str,
    footer: str,
) -> tuple[VisibleMessageSegment, ...] | None:
    """Split one exact fixed-wrapper shape without trusting its diagnostic."""
    prefix, postfix = _wrapped_diagnostic_wire_shape(header, footer)
    if not suffix.startswith(prefix) or not suffix.endswith(postfix):
        return None
    diagnostic = suffix[len(prefix) : -len(postfix)]
    if not diagnostic:
        return None
    return (
        TrustedSystemNoticeSegment(header),
        AssistantTextSegment(f"Cause: {diagnostic}"),
        TrustedSystemNoticeSegment(footer),
    )


# Historical stored messages retain their original provenance. These literal
# bytes are reader vocabulary only; no live producer makes this NLP judgment.
_LEGACY_GROUNDING_HEADER: Final = (
    "The composer's prose above contradicts the actual "
    "pipeline state. The state below is authoritative; the prose may be "
    "stale or refer to an earlier turn."
)
_LEGACY_GROUNDING_INSTRUCTION: Final = "Re-check the actual pipeline state before making further claims about pipeline configuration."


def _split_legacy_grounding_correction(suffix: str) -> tuple[VisibleMessageSegment, ...] | None:
    """Decode a historical wrapper without rejudging prose or trusting bullets."""
    prefix = _TRUSTED_NOTICE_SEPARATOR + _TRUSTED_NOTICE_MARKER + _LEGACY_GROUNDING_HEADER + "\n\n"
    postfix = "\n\n" + _LEGACY_GROUNDING_INSTRUCTION
    if not suffix.startswith(prefix) or not suffix.endswith(postfix):
        return None
    violation_bullets = suffix[len(prefix) : -len(postfix)]
    if not violation_bullets.startswith("- "):
        return None
    return (
        TrustedSystemNoticeSegment(_LEGACY_GROUNDING_HEADER),
        AssistantTextSegment(violation_bullets),
        TrustedSystemNoticeSegment(_LEGACY_GROUNDING_INSTRUCTION),
    )


# Recognizer registration for the ``_PUBLISHED_`` END-gate blocked notices:
# (bare suffix, trusted notice) and (header, footer) of each wrapped shape.
# The pending-handoff findings shape also exists withheld, differing only by
# the disclosure sentence at the end of its HEADER. ``_split_wrapped_diagnostic``
# requires the header to be followed directly by ``"\n\nCause: "``, so neither
# arm can claim the other's suffix (pinned by
# ``test_the_recognizer_attributes_every_wrapped_suffix_to_its_own_shape``).
_ADVISOR_PUBLISHED_BARE_SUFFIXES: Final[tuple[tuple[str, str], ...]] = (
    (_ADVISOR_SIGNOFF_PENDING_PUBLISHED_FINALIZE_SUFFIX, _ADVISOR_SIGNOFF_PENDING_PUBLISHED_NOTICE),
    (_ADVISOR_SIGNOFF_UNVERIFIED_PUBLISHED_FINALIZE_SUFFIX, _ADVISOR_SIGNOFF_UNVERIFIED_PUBLISHED_NOTICE),
    (_ADVISOR_SIGNOFF_UNAVAILABLE_PENDING_PUBLISHED_FINALIZE_SUFFIX, _ADVISOR_SIGNOFF_UNAVAILABLE_PENDING_PUBLISHED_NOTICE),
    (_ADVISOR_SIGNOFF_MALFORMED_PENDING_PUBLISHED_FINALIZE_SUFFIX, _ADVISOR_SIGNOFF_MALFORMED_PENDING_PUBLISHED_NOTICE),
    (_ADVISOR_SIGNOFF_UNAVAILABLE_UNVERIFIED_PUBLISHED_FINALIZE_SUFFIX, _ADVISOR_SIGNOFF_UNAVAILABLE_UNVERIFIED_PUBLISHED_NOTICE),
    (_ADVISOR_SIGNOFF_MALFORMED_UNVERIFIED_PUBLISHED_FINALIZE_SUFFIX, _ADVISOR_SIGNOFF_MALFORMED_UNVERIFIED_PUBLISHED_NOTICE),
    (_ADVISOR_SIGNOFF_UNREPAIRABLE_PUBLISHED_FINALIZE_SUFFIX, _ADVISOR_SIGNOFF_UNREPAIRABLE_PUBLISHED_NOTICE),
    (_ADVISOR_SIGNOFF_UNREPAIRABLE_UNVERIFIED_PUBLISHED_FINALIZE_SUFFIX, _ADVISOR_SIGNOFF_UNREPAIRABLE_UNVERIFIED_PUBLISHED_NOTICE),
    (_ADVISOR_SIGNOFF_UNREPAIRABLE_HANDOFF_PUBLISHED_FINALIZE_SUFFIX, _ADVISOR_SIGNOFF_UNREPAIRABLE_HANDOFF_PUBLISHED_NOTICE),
    (
        _ADVISOR_SIGNOFF_UNREPAIRABLE_RED_PUBLISHED_SUFFIX_BARE,
        f"{_ADVISOR_SIGNOFF_UNREPAIRABLE_HEADER}\n\n{_ADVISOR_SIGNOFF_UNREPAIRABLE_RED_PUBLISHED_FOOTER}",
    ),
    (_ADVISOR_SIGNOFF_FLAGGED_RED_PUBLISHED_SUFFIX_BARE, f"{_PREFLIGHT_NOTICE_HEADER}\n\n{_ADVISOR_SIGNOFF_FLAGGED_RED_PUBLISHED_FOOTER}"),
    (
        _ADVISOR_SIGNOFF_UNRENDERED_RED_PUBLISHED_SUFFIX_BARE,
        f"{_PREFLIGHT_NOTICE_HEADER}\n\n{_ADVISOR_SIGNOFF_UNRENDERED_RED_PUBLISHED_FOOTER}",
    ),
    (_ADVISOR_SIGNOFF_PENDING_HANDOFF_PUBLISHED_FINALIZE_SUFFIX, _ADVISOR_SIGNOFF_PENDING_HANDOFF_PUBLISHED_NOTICE),
)
_ADVISOR_PUBLISHED_WRAPPED_SHAPES: Final[tuple[tuple[str, str], ...]] = (
    (_ADVISOR_SIGNOFF_UNREPAIRABLE_HEADER, _ADVISOR_SIGNOFF_UNREPAIRABLE_RED_PUBLISHED_FOOTER),
    (_PREFLIGHT_NOTICE_HEADER, _ADVISOR_SIGNOFF_FLAGGED_RED_PUBLISHED_FOOTER),
    (_PREFLIGHT_NOTICE_HEADER, _ADVISOR_SIGNOFF_UNRENDERED_RED_PUBLISHED_FOOTER),
    (_ADVISOR_SIGNOFF_PENDING_HANDOFF_PUBLISHED_NOTICE, _ADVISOR_SIGNOFF_PENDING_HANDOFF_FINDINGS_FOOTER),
)


def _canonical_base_suffix_segments(suffix: str, *, allow_request_rejection: bool = True) -> tuple[VisibleMessageSegment, ...] | None:
    """Recognize only pre-receipt shapes, including one final unavailable notice."""
    rejection_prefix, separator, rejection_notice = suffix.rpartition(_TRUSTED_NOTICE_SEPARATOR + _TRUSTED_NOTICE_MARKER)
    if separator and allow_request_rejection:
        # Reconstruct from the closed safe reason vocabulary. An arbitrary
        # provider string or a changed reference cannot acquire system origin.
        reasons: tuple[tuple[RequestRejectionReason | None, ReasonDisposition], ...] = (
            ("invalid_request", "retained"),
            ("context_length_exceeded", "retained"),
            ("unsupported_parameter", "retained"),
            (None, "absent"),
            (None, "unrecognized"),
            (None, "unavailable"),
        )
        for reason_code, disposition in reasons:
            rejection = SafeRequestRejection(
                reason_code=reason_code,
                reason_disposition=disposition,
                reason_source="sdk_body" if reason_code is not None else "unavailable",
            )
            for base in (rejection.public_detail, rejection.handoff_detail):
                tail = rejection_notice.removeprefix(base)
                reference = re.fullmatch(r" Diagnostic reference: provider-failure:[0-9a-f]{64}\.", tail)
                if rejection_notice == base or (rejection_notice.startswith(base) and reference is not None):
                    preceding_segments = (
                        _canonical_base_suffix_segments(rejection_prefix, allow_request_rejection=False) if rejection_prefix else ()
                    )
                    if preceding_segments is not None:
                        return (*preceding_segments, TrustedSystemNoticeSegment(rejection_notice))
    if suffix == _GRAPH_RECOVERY_REFUSAL_SUFFIX:
        return (TrustedSystemNoticeSegment(_GRAPH_RECOVERY_REFUSAL_NOTICE),)
    if suffix == _REVIEW_REPLY_UNAVAILABLE_SUFFIX:
        return (TrustedSystemNoticeSegment(_REVIEW_REPLY_UNAVAILABLE_NOTICE),)
    if suffix == _ADVISOR_REVIEW_BUDGET_SUFFIX:
        return (TrustedSystemNoticeSegment(_ADVISOR_REVIEW_BUDGET_NOTICE),)
    if suffix.endswith(_REVIEW_REPLY_UNAVAILABLE_SUFFIX):
        preceding = suffix[: -len(_REVIEW_REPLY_UNAVAILABLE_SUFFIX)]
        if _REVIEW_REPLY_UNAVAILABLE_SUFFIX in preceding:
            return None
        segments = _canonical_base_suffix_segments(preceding)
        if segments is not None:
            return (*segments, TrustedSystemNoticeSegment(_REVIEW_REPLY_UNAVAILABLE_NOTICE))
        return None
    if suffix == _EMPTY_STATE_FINALIZE_SUFFIX:
        return (TrustedSystemNoticeSegment(_EMPTY_STATE_NOTICE_BODY),)
    if suffix == _CLARIFICATION_FINALIZE_SUFFIX:
        return (TrustedSystemNoticeSegment(_CLARIFICATION_NOTICE),)
    empty_with_blocker = _split_wrapped_diagnostic(
        suffix,
        header=_EMPTY_STATE_NOTICE_HEADER,
        footer=_EMPTY_STATE_NOTICE_NEXT_STEP,
    )
    if empty_with_blocker is not None:
        return empty_with_blocker
    if suffix == _ADVISOR_SIGNOFF_PENDING_FINALIZE_SUFFIX:
        return (TrustedSystemNoticeSegment(_ADVISOR_SIGNOFF_PENDING_NOTICE),)
    if suffix == _ADVISOR_SIGNOFF_PENDING_HANDOFF_FINALIZE_SUFFIX:
        return (TrustedSystemNoticeSegment(_ADVISOR_SIGNOFF_PENDING_HANDOFF_NOTICE),)
    handoff_with_findings = _split_wrapped_diagnostic(
        suffix,
        header=_ADVISOR_SIGNOFF_PENDING_HANDOFF_NOTICE,
        footer=_ADVISOR_SIGNOFF_PENDING_HANDOFF_FINDINGS_FOOTER,
    )
    if handoff_with_findings is not None:
        return handoff_with_findings
    for published_suffix, published_notice in _ADVISOR_PUBLISHED_BARE_SUFFIXES:
        if suffix == published_suffix:
            return (TrustedSystemNoticeSegment(published_notice),)
    for published_header, published_footer in _ADVISOR_PUBLISHED_WRAPPED_SHAPES:
        published_with_detail = _split_wrapped_diagnostic(suffix, header=published_header, footer=published_footer)
        if published_with_detail is not None:
            return published_with_detail
    if suffix == _INTERPRETATION_REVIEW_HANDOFF_FINALIZE_SUFFIX:
        return (TrustedSystemNoticeSegment(_INTERPRETATION_REVIEW_HANDOFF_NOTICE),)
    review_handoff_with_findings = _split_wrapped_diagnostic(
        suffix,
        header=_INTERPRETATION_REVIEW_HANDOFF_NOTICE,
        footer=_INTERPRETATION_REVIEW_HANDOFF_FINDINGS_FOOTER,
    )
    if review_handoff_with_findings is not None:
        return review_handoff_with_findings
    if suffix == _PREFLIGHT_INVALID_NONEMPTY_FINALIZE_SUFFIX_BARE:
        return (TrustedSystemNoticeSegment(f"{_PREFLIGHT_NOTICE_HEADER}\n\n{_PREFLIGHT_NOTICE_FOOTER}"),)
    preflight_with_diagnostic = _split_wrapped_diagnostic(
        suffix,
        header=_PREFLIGHT_NOTICE_HEADER,
        footer=_PREFLIGHT_NOTICE_FOOTER,
    )
    if preflight_with_diagnostic is not None:
        return preflight_with_diagnostic
    return _split_legacy_grounding_correction(suffix)


def _canonical_trusted_suffix_segments(suffix: str) -> tuple[VisibleMessageSegment, ...] | None:
    """Consume a base, receipt, or complete base followed by one closed receipt."""
    receipt = _CHECKPOINT_RECEIPT_PATTERN.fullmatch(suffix)
    if receipt is None:
        return _canonical_base_suffix_segments(suffix)
    scope = receipt.group("scope")
    count = _CHECKPOINT_COUNT_PATTERN.search(scope)
    if count is not None and int(count.group()) > sys.maxsize:
        return None
    notice = TrustedSystemNoticeSegment(_CHECKPOINT_RECEIPT_LABEL + scope + _CHECKPOINT_RECEIPT_END)
    base = receipt.group("base")
    if base is None:
        return (notice,)
    segments = _canonical_base_suffix_segments(base)
    return (*segments, notice) if segments is not None else None


def compose_checkpoint_receipt_message(content: str, *, raw_content: str, evidence: TurnEvidence) -> str:
    """Append facts at the durable raw boundary after all base reconstruction.

    Unsupported or already decorated suffixes are caller contract violations;
    never promote an interior marker or recover hidden prose to repair them.
    """
    if type(evidence) is not TurnEvidence:
        raise TypeError("Checkpoint receipt requires owned turn evidence")
    if not content.startswith(raw_content):
        raise AuditIntegrityError("Checkpoint receipt requires the exact raw prefix")
    scope = evidence.render_saved_scope()
    notice = _CHECKPOINT_RECEIPT_LABEL + scope + _CHECKPOINT_RECEIPT_END
    suffix = content[len(raw_content) :]
    if not suffix:
        return content + _bare_trusted_suffix(notice)
    canonical_suffix = (
        _TRUSTED_NOTICE_SEPARATOR + suffix if raw_content == "" and not suffix.startswith(_TRUSTED_NOTICE_SEPARATOR) else suffix
    )
    if _canonical_base_suffix_segments(canonical_suffix) is None:
        raise AuditIntegrityError("Checkpoint receipt requires a complete base-only suffix")
    return content + _CHECKPOINT_RECEIPT_REGION_PREFIX + scope + _CHECKPOINT_RECEIPT_END


def visible_message_segments(*, content: str, raw_content: str | None) -> tuple[VisibleMessageSegment, ...]:
    """Project durable assistant text into provenance-bearing visible segments.

    The ``raw_content`` prefix pairing is necessary but not sufficient: empty
    or stale prefixes can match attacker-controlled content.  A suffix mints
    trusted chrome only when it exactly matches one of the closed canonical
    composer shapes above. Dynamic blocker, validator, and historical grounding
    diagnostics remain ordinary text between fixed trusted segments.

    Equality, legacy replacement, malformed, and unknown synthesis shapes fail
    closed to one ordinary text segment. Empty ``raw_content`` is accepted only
    when the complete content matches one of those same canonical wrappers.
    """
    if type(content) is not str:
        raise TypeError("content must be an exact string")
    if raw_content is not None and type(raw_content) is not str:
        raise TypeError("raw_content must be an exact string or None")
    if raw_content == "":
        canonical_content = content if content.startswith(_TRUSTED_NOTICE_SEPARATOR) else _TRUSTED_NOTICE_SEPARATOR + content
        suffix_segments = _canonical_trusted_suffix_segments(canonical_content)
        if suffix_segments is not None:
            return suffix_segments
        return (AssistantTextSegment(content),)
    if raw_content is None or not content.startswith(raw_content):
        return (AssistantTextSegment(content),)

    suffix_segments = _canonical_trusted_suffix_segments(content[len(raw_content) :])
    if suffix_segments is None:
        return (AssistantTextSegment(content),)
    return (AssistantTextSegment(raw_content), *suffix_segments)


def state_is_structurally_empty(state: CompositionState) -> bool:
    """Return True when no composition tools have produced visible state."""
    return not state.sources and not state.nodes and not state.edges and not state.outputs


def enforce_augmentation_prefix_invariant(
    *,
    branch: _AugmentationBranch,
    content: str,
    augmented: str,
) -> None:
    """Mechanically enforce the producer-side augmentation prefix contract."""
    if not augmented.startswith(content):
        _COMPOSER_AUDIT_INTEGRITY_VIOLATION_COUNTER.add(1, {"invariant": "augmentation_prefix", "branch": branch})
        raise AuditIntegrityError(
            f"Tier 1: composer augmentation contract violated on branch={branch!r}. "
            "Producer constructed an augmented message that does not have the "
            "model's pre-synthesis prose as a strict prefix. The consumer-side "
            "discriminator at routes._composer_history_content uses "
            "content.startswith(raw_content) to detect synthesis and strip "
            "the operator-facing suffix from LLM history; a producer that "
            "breaks the prefix property misroutes synthesized operator-facing "
            "text into the model's prior-turn history. Fix the augmented-"
            "message constructor so the prose appears verbatim at the start "
            "of message."
        )


def compose_clarification_message(content: str) -> str:
    """Disclose the unchanged empty state without asserting a build failure."""
    return content + _CLARIFICATION_FINALIZE_SUFFIX


def compose_empty_state_message(content: str, *, blocker: str | None = None) -> str:
    """Build the user-facing message for the empty-state finalize path."""
    suffix = _EMPTY_STATE_FINALIZE_SUFFIX_WITH_BLOCKER.format(blocker=blocker) if blocker else _EMPTY_STATE_FINALIZE_SUFFIX
    if not content:
        return suffix.lstrip("\n").lstrip("-").lstrip()
    return content + suffix


def first_validation_objection(runtime_result: ValidationResult) -> str | None:
    """Name the leading error (or failed check) from a validation result."""
    if runtime_result.errors:
        return runtime_result.errors[0].message
    failed_checks = [check for check in runtime_result.checks if not check.passed]
    if failed_checks:
        return failed_checks[0].detail
    return None


def _red_diagnostic_suffix(runtime_result: ValidationResult, *, with_detail_template: str, bare_suffix: str) -> str:
    """One extraction of a red result's leading objection into a wrapped shape.

    Shared by every composer that renders a red validation result: the
    leading error's message and suggestion (or the first failed check's
    detail) fill the untrusted diagnostic slots; a result with nothing
    extractable falls back to the fixed bare shape.
    """
    detail: str | None = None
    suggestion: str | None = None
    if runtime_result.errors:
        first_error = runtime_result.errors[0]
        detail = first_error.message
        suggestion = first_error.suggestion
    else:
        failed_checks = [check for check in runtime_result.checks if not check.passed]
        if failed_checks:
            detail = failed_checks[0].detail
    if not detail:
        return bare_suffix
    suggestion_block = f"\n\nSuggested fix: {suggestion}" if suggestion else ""
    return with_detail_template.format(detail=detail, suggestion_block=suggestion_block)


def compose_preflight_failure_message(content: str, *, runtime_result: ValidationResult) -> str:
    """Build the user-facing message for the non-empty-state preflight-invalid path."""
    suffix = _red_diagnostic_suffix(
        runtime_result,
        with_detail_template=_PREFLIGHT_INVALID_NONEMPTY_FINALIZE_SUFFIX_WITH_DETAIL,
        bare_suffix=_PREFLIGHT_INVALID_NONEMPTY_FINALIZE_SUFFIX_BARE,
    )
    if not content:
        return suffix.lstrip("\n").lstrip("-").lstrip()
    return content + suffix


def _append_advisor_suffix(content: str, suffix: str) -> str:
    if not content:
        return suffix.lstrip("\n").lstrip("-").lstrip()
    return content + suffix


# The END-gate blocked composers below publish the model's reply beside the
# notice (ruling 2026-09-22, elspeth-032ec69c41), so they emit only the
# ``_PUBLISHED_`` notices. Two of them — ``compose_advisor_signoff_pending_message``
# and ``compose_advisor_pending_handoff_message`` — also serve the advisor-repair
# replacer, which DOES withhold, so they alone take ``prose_withheld``
# (required, no default): True selects the notice carrying the withheld-prose
# disclosure and is the only honest choice when ``content`` is the empty string
# the caller substituted for prose it withheld; False selects the published
# notice. A default would let a new caller publish the disclosure over prose it
# did not withhold, or omit it over prose it did.
def compose_advisor_signoff_flagged_red_message(content: str, *, runtime_result: ValidationResult) -> str:
    """RED preflight + a FLAGGED advisory verdict (elspeth-b61894d93d).

    ``runtime_result`` is the turn's ACTUAL red preflight — never the
    synthesized advisor-signoff validation, whose errors carry the advisor
    wording rather than the validator's objection. The footer keeps the
    did-not-clear framing, which is true only for this reason class.
    """
    suffix = _red_diagnostic_suffix(
        runtime_result,
        with_detail_template=_ADVISOR_SIGNOFF_FLAGGED_RED_PUBLISHED_SUFFIX_WITH_DETAIL,
        bare_suffix=_ADVISOR_SIGNOFF_FLAGGED_RED_PUBLISHED_SUFFIX_BARE,
    )
    return _append_advisor_suffix(content, suffix)


def compose_advisor_signoff_unrendered_red_message(content: str, *, runtime_result: ValidationResult) -> str:
    """RED preflight + an UNRENDERED advisory verdict (elspeth-b61894d93d).

    Same shape as the flagged variant, but the footer keeps the
    could-not-be-obtained framing — true only when the advisor rendered no
    verdict (unavailable/malformed), never for a FLAG.
    """
    suffix = _red_diagnostic_suffix(
        runtime_result,
        with_detail_template=_ADVISOR_SIGNOFF_UNRENDERED_RED_PUBLISHED_SUFFIX_WITH_DETAIL,
        bare_suffix=_ADVISOR_SIGNOFF_UNRENDERED_RED_PUBLISHED_SUFFIX_BARE,
    )
    return _append_advisor_suffix(content, suffix)


def compose_advisor_signoff_pending_message(content: str, *, prose_withheld: bool) -> str:
    """Build the user-facing message for a validated-but-unsigned build.

    Deliberately NOT ``compose_preflight_failure_message``: the runtime
    preflight passed, so a "Runtime preflight failed" header would be a false
    statement about the user's pipeline (R2-F14, elspeth-5403f346c0). The
    suffix is entirely fixed prose — no validator detail is interpolated,
    because there is no validator objection to report.
    """
    return _append_advisor_suffix(
        content,
        _ADVISOR_SIGNOFF_PENDING_FINALIZE_SUFFIX if prose_withheld else _ADVISOR_SIGNOFF_PENDING_PUBLISHED_FINALIZE_SUFFIX,
    )


def compose_advisor_signoff_unverified_message(content: str) -> str:
    """Build the user-facing message for an absent-preflight advisor block.

    elspeth-2ae50afcd1 facet B. Deliberately NOT
    ``compose_preflight_failure_message``: no preflight ran this turn, so a
    "Runtime preflight failed" header would assert a failure that never
    occurred (the same dishonest-header class R2-F14 / elspeth-5403f346c0
    closed for the green shape). And deliberately not the sign-off-pending
    notice: that shape rides a preflight that ran and PASSED, while this one
    must say readiness was not re-verified. The suffix is entirely fixed
    prose — there is no validator objection to interpolate, because no
    validator ran.
    """
    return _append_advisor_suffix(content, _ADVISOR_SIGNOFF_UNVERIFIED_PUBLISHED_FINALIZE_SUFFIX)


def compose_advisor_signoff_unrendered_pending_message(
    content: str, *, failure_class: Literal["unavailable", "request_rejected", "malformed", "budget_exhausted"]
) -> str:
    """GREEN preflight + an UNRENDERED advisory verdict (unavailable/malformed).

    Sibling of :func:`compose_advisor_signoff_pending_message`, which keeps the
    did-not-clear framing that is true only for a rendered FLAG. The copy names
    the verdict class and its remedy (retry, or check the advisor model
    configuration). Fixed prose; nothing from the advisor is interpolated.
    """
    if failure_class == "budget_exhausted":
        return _append_advisor_suffix(content, _ADVISOR_REVIEW_BUDGET_SUFFIX)
    if failure_class == "request_rejected":
        return content
    if failure_class == "unavailable":
        suffix = _ADVISOR_SIGNOFF_UNAVAILABLE_PENDING_PUBLISHED_FINALIZE_SUFFIX
    else:
        suffix = _ADVISOR_SIGNOFF_MALFORMED_PENDING_PUBLISHED_FINALIZE_SUFFIX
    return _append_advisor_suffix(content, suffix)


def compose_advisor_signoff_unrendered_unverified_message(
    content: str, *, failure_class: Literal["unavailable", "request_rejected", "malformed", "budget_exhausted"]
) -> str:
    """ABSENT preflight + an UNRENDERED advisory verdict (unavailable/malformed).

    Sibling of :func:`compose_advisor_signoff_unverified_message`: the same
    facet B readiness disclosure (no preflight ran, so no pipeline claim), with
    the could-not-be-obtained framing and remedy for the verdict class.
    """
    if failure_class == "budget_exhausted":
        return _append_advisor_suffix(content, _ADVISOR_REVIEW_BUDGET_SUFFIX)
    if failure_class == "request_rejected":
        return content
    if failure_class == "unavailable":
        suffix = _ADVISOR_SIGNOFF_UNAVAILABLE_UNVERIFIED_PUBLISHED_FINALIZE_SUFFIX
    else:
        suffix = _ADVISOR_SIGNOFF_MALFORMED_UNVERIFIED_PUBLISHED_FINALIZE_SUFFIX
    return _append_advisor_suffix(content, suffix)


def compose_request_rejected_message(content: str, *, rejection: SafeRequestRejection, handoff: bool) -> str:
    """Append only the admitted descriptor's canonical system notice."""
    return _append_advisor_suffix(content, _bare_trusted_suffix(rejection.handoff_detail if handoff else rejection.public_detail))


def compose_advisor_signoff_unrepairable_message(content: str) -> str:
    """Build the user-facing message for a user-message pre-scan block.

    elspeth-25f7b757e7 (A1). The deterministic pre-scan FLAGGED the user's own
    chat message, so the actionable step is REWORDING — not a pipeline edit,
    and not another advisory attempt against the same unchangeable bytes. The
    suffix is entirely fixed prose; the flagged message itself is never
    interpolated.
    """
    return _append_advisor_suffix(content, _ADVISOR_SIGNOFF_UNREPAIRABLE_PUBLISHED_FINALIZE_SUFFIX)


def compose_advisor_signoff_unrepairable_unverified_message(content: str) -> str:
    """ABSENT-preflight variant of the unrepairable block (fix round 1, N1).

    No validation ran this turn, so the copy makes no pipeline claim at all —
    the facet B discipline applied to the unrepairable reason.
    """
    return _append_advisor_suffix(content, _ADVISOR_SIGNOFF_UNREPAIRABLE_UNVERIFIED_PUBLISHED_FINALIZE_SUFFIX)


def compose_advisor_signoff_unrepairable_handoff_message(content: str) -> str:
    """PENDING-HANDOFF variant of the unrepairable block (fix round 1, N1).

    Names the still-pending interpretation review: rewording is not the only
    remaining step (the ac85b0ab0e class).
    """
    return _append_advisor_suffix(content, _ADVISOR_SIGNOFF_UNREPAIRABLE_HANDOFF_PUBLISHED_FINALIZE_SUFFIX)


def compose_advisor_signoff_unrepairable_red_message(content: str, *, runtime_result: ValidationResult) -> str:
    """RED-preflight variant of the unrepairable block (fix round 1, N1).

    ``runtime_result`` is the turn's ACTUAL red preflight (never the
    synthesized advisor-signoff validation): its leading objection rides the
    untrusted ``Cause:`` region so the user with a broken pipeline is not
    told there is nothing to fix.
    """
    suffix = _red_diagnostic_suffix(
        runtime_result,
        with_detail_template=_ADVISOR_SIGNOFF_UNREPAIRABLE_RED_PUBLISHED_SUFFIX_WITH_DETAIL,
        bare_suffix=_ADVISOR_SIGNOFF_UNREPAIRABLE_RED_PUBLISHED_SUFFIX_BARE,
    )
    return _append_advisor_suffix(content, suffix)


def compose_advisor_pending_handoff_message(content: str, *, prose_withheld: bool, outstanding_findings_detail: str | None = None) -> str:
    """Build the user-facing message for a pending handoff the advisor did not clear.

    Neither sibling wording fits: the preflight header would claim validation
    failed on a build whose authoring passed, and the sign-off-pending notice
    would claim completion is withheld on a result that keeps
    ``completion_ready`` (elspeth-66717f0c99). The bare shape is fixed prose —
    the advisor's own wording rides the appended failed ``advisor_signoff``
    check instead of this surface.

    ``outstanding_findings_detail`` (elspeth-ac85b0ab0e) carries the leading
    objection from the authoring-masked re-validation when it found failures
    behind the pending review. The bare notice would then imply the review
    cards are the only remaining step — false, and exactly how battery round
    7's g03 sessions terminated over a persisted ``is_valid=False`` state.
    The detail is VALIDATOR-authored text (same trust class as the preflight
    wrapper's interpolation), rendered as an untrusted ``Cause:`` segment
    between the trusted notice and a trusted footer.
    """
    if outstanding_findings_detail is None:
        suffix = (
            _ADVISOR_SIGNOFF_PENDING_HANDOFF_FINALIZE_SUFFIX
            if prose_withheld
            else _ADVISOR_SIGNOFF_PENDING_HANDOFF_PUBLISHED_FINALIZE_SUFFIX
        )
    else:
        template = (
            _ADVISOR_SIGNOFF_PENDING_HANDOFF_FINDINGS_SUFFIX_WITH_DETAIL
            if prose_withheld
            else _ADVISOR_SIGNOFF_PENDING_HANDOFF_PUBLISHED_FINDINGS_SUFFIX_WITH_DETAIL
        )
        suffix = template.format(detail=outstanding_findings_detail)
    return _append_advisor_suffix(content, suffix)


def compose_interpretation_review_handoff_message(
    content: str,
    *,
    outstanding_findings_detail: str | None = None,
    suggestion_block: str = "",
) -> str:
    """Build the user-facing message announcing a staged interpretation review.

    The no-tool finalize tail's counterpart to
    :func:`compose_advisor_pending_handoff_message`, and deliberately quieter:
    the notice announces review cards and conditions execution on remaining
    checks without claiming an advisory outcome.

    ``outstanding_findings_detail`` carries the leading objection from the
    authoring-masked re-validation (``_pending_handoff_outstanding_findings``)
    when it found failures behind the pending review. The bare notice would
    then imply the review cards are the only remaining step. The detail is
    VALIDATOR-authored text, so the qualified shape renders it as an untrusted
    ``Cause:`` segment between the trusted notice and a trusted footer rather
    than folding it into operator-authored prose.

    Callers pass the detail through ``_outstanding_findings_detail``, which
    guards the empty-string objection that would format as ``Cause: \\n\\n`` —
    a shape ``_split_wrapped_diagnostic`` rejects, silently demoting the whole
    suffix to one untrusted segment.

    ``suggestion_block`` is the preformatted ``"\\n\\nSuggested fix: ..."`` tail,
    empty when there is no suggestion. It is meaningful only alongside a
    detail, and like the detail it is validator-authored, so it stays inside
    the untrusted region.
    """
    if outstanding_findings_detail is None:
        suffix = _INTERPRETATION_REVIEW_HANDOFF_FINALIZE_SUFFIX
    else:
        suffix = _INTERPRETATION_REVIEW_HANDOFF_FINDINGS_SUFFIX_WITH_DETAIL.format(
            detail=outstanding_findings_detail,
            suggestion_block=suggestion_block,
        )
    if not content:
        return suffix.lstrip("\n").lstrip("-").lstrip()
    return content + suffix


def advisor_signoff_pending_handoff_wording(*, reason: str, findings: str, findings_backend_authored: bool = False) -> str:
    """Return the appended check's detail for a PRESERVED pending handoff.

    Deliberately not ``_advisor_signoff_blocked_wording``: that pair is shared
    by the two surfaces that genuinely withhold completion, and this one does
    not (elspeth-66717f0c99). The reason classes and the findings rule are
    identical to it — a flagged reason carries ``findings`` only when it is the
    backend-authored pre-scan string that names the triggering key/field
    (elspeth-cd9af8e61d), so raw advisor-MODEL findings stay withheld (R2-F13),
    and the could-not-be-obtained reasons always carry it because there it is
    the fixed backend classification naming the class in plain language.

    Returns the detail alone, not a (detail, suggestion) pair: the preserved
    shape appends no blocker and no error, so there is no surface to carry a
    suggestion.
    """
    if reason == "flagged_unrepairable":
        # Fix round 1 (N1): dedicated arm — falling through to the outage
        # wording below claimed the review was never obtained while appending
        # its FLAGGED finding to the same sentence. ``findings`` on this
        # reason is always the backend-authored pre-scan string.
        if findings_backend_authored and findings:
            return f"{_ADVISOR_SIGNOFF_PENDING_HANDOFF_UNREPAIRABLE_DETAIL} {findings}"
        return _ADVISOR_SIGNOFF_PENDING_HANDOFF_UNREPAIRABLE_DETAIL
    if reason in {"flagged_final_pass", "flagged_no_repair"}:
        if findings_backend_authored and findings:
            return f"{_ADVISOR_SIGNOFF_PENDING_HANDOFF_FLAGGED_DETAIL} {findings}"
        return _ADVISOR_SIGNOFF_PENDING_HANDOFF_FLAGGED_DETAIL
    return f"{_ADVISOR_SIGNOFF_PENDING_HANDOFF_UNRENDERED_DETAIL} {findings}"


def _tool_failure_detail(payload: Mapping[str, Any]) -> str:
    """Extract a concise semantic failure detail from a ToolResult payload."""
    match payload:
        case {"validation": {"errors": [first_error, *_]}}:
            match first_error:
                case {"message": message}:
                    return f": {message}"
    if "error" in payload:
        return f": {payload['error']}"
    return "."


def last_mutation_was_pending_proposal(tool_invocations: tuple[ComposerToolInvocation, ...]) -> bool:
    """Return True iff the most recent non-discovery dispatch was an approval proposal."""
    for invocation in reversed(tool_invocations):
        if is_discovery_tool(invocation.tool_name):
            continue
        if invocation.status is not ComposerToolStatus.SUCCESS:
            return False
        if invocation.result_canonical is None:
            return False
        payload = json.loads(invocation.result_canonical)
        if type(payload) is not dict:
            return False
        data = payload["data"] if "data" in payload else None
        if type(data) is not dict:
            return False
        return ("status" in data) and data["status"] == "APPROVAL_REQUIRED"
    return False


def blocking_result_from_tool_invocations(tool_invocations: tuple[ComposerToolInvocation, ...]) -> str:
    """Name the most recent failed build/edit tool result, if one exists."""
    for invocation in reversed(tool_invocations):
        # Discovery (read-only) calls cannot be why a build left the state
        # empty, in ANY outcome arm: a trailing discovery ARG_ERROR,
        # PLUGIN_CRASH or ``success=false`` used to become the user-facing
        # Cause and mask the failed build/edit call before it.
        if is_discovery_tool(invocation.tool_name):
            continue
        if invocation.status is ComposerToolStatus.ARG_ERROR:
            return f"{invocation.tool_name} failed before mutation ({invocation.error_class}: {invocation.error_message})."
        if invocation.status is ComposerToolStatus.PLUGIN_CRASH:
            return (
                f"{invocation.tool_name} crashed before a safe mutation completed ({invocation.error_class}: {invocation.error_message})."
            )
        if invocation.status is ComposerToolStatus.SUCCESS and invocation.result_canonical is not None:
            payload = json.loads(invocation.result_canonical)
            if type(payload) is dict and "success" in payload and payload["success"] is False:
                return f"{invocation.tool_name} returned success=false{_tool_failure_detail(payload)}"
            if (
                type(payload) is dict
                and "success" in payload
                and payload["success"] is True
                and invocation.version_after == invocation.version_before
            ):
                return f"{invocation.tool_name} succeeded without mutating CompositionState (version stayed {invocation.version_before})."
    return "the model ended the turn without calling any build/edit tool."


def is_pending_interpretation_handoff(result: ValidationResult) -> bool:
    readiness = result.readiness
    return (
        readiness.authoring_valid
        and readiness.completion_ready
        and not readiness.execution_ready
        and any(blocker.code == INTERPRETATION_REVIEW_PENDING_CODE for blocker in readiness.blockers)
    )


def no_mutation_empty_state_validation(blocker: str) -> ValidationResult:
    """Build the synthetic final-gate result for empty-state no-mutation replies."""
    detail = f"No composition-state mutation completed successfully; state_exists=false. Blocking result: {blocker}"
    suggestion = (
        "Call set_pipeline with source.blob_id or source.inline_blob, call "
        "set_source_from_blob/set_source plus set_output, or ask for the specific "
        "missing file/configuration."
    )
    return ValidationResult(
        is_valid=False,
        checks=[ValidationCheck(name=CHECK_STATE_EXISTS, passed=False, detail=detail, affected_nodes=(), outcome_code=None)],
        errors=[
            ValidationError(
                component_id=None,
                component_type=None,
                message=detail,
                suggestion=suggestion,
                error_code=None,
            )
        ],
        readiness=ValidationReadiness(
            authoring_valid=False,
            execution_ready=False,
            completion_ready=False,
            blockers=[
                ValidationReadinessBlocker(
                    code="state_exists",
                    suggestion=None,
                    note=None,
                    component_id=None,
                    component_type=None,
                    detail=detail,
                )
            ],
        ),
    )
