"""No-tool finalization policy and user-facing augmentation messages."""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from io import StringIO
from typing import Any, ClassVar, Final, Literal

from opentelemetry import metrics

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.recipe_intent_routing import match_freeform_recipe_intent
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.state_claim_grounding import (
    GROUNDING_CORRECTION_HEADER,
    GROUNDING_CORRECTION_INSTRUCTION,
)
from elspeth.web.composer.tools import is_discovery_tool
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

_EMPTY_STATE_NOTICE_HEADER: Final = "The pipeline is still empty — the composer did not complete a valid build this turn."
_EMPTY_STATE_NOTICE_NEXT_STEP: Final = (
    "To continue: refine your request with more specifics, or reply telling the composer to retry with the plan it described above."
)
_EMPTY_STATE_NOTICE_BODY: Final = f"{_EMPTY_STATE_NOTICE_HEADER} {_EMPTY_STATE_NOTICE_NEXT_STEP}"
_EMPTY_STATE_FINALIZE_SUFFIX = _TRUSTED_NOTICE_SEPARATOR + _TRUSTED_NOTICE_MARKER + _EMPTY_STATE_NOTICE_BODY
_EMPTY_STATE_FINALIZE_SUFFIX_WITH_BLOCKER = (
    _TRUSTED_NOTICE_SEPARATOR
    + _TRUSTED_NOTICE_MARKER
    + _EMPTY_STATE_NOTICE_HEADER
    + "\n\nCause: {blocker}\n\n"
    + _EMPTY_STATE_NOTICE_NEXT_STEP
)

_PREFLIGHT_NOTICE_HEADER: Final = "Runtime preflight failed before this build could be marked complete."
_PREFLIGHT_NOTICE_FOOTER: Final = "The composer's analysis above is preserved verbatim; the validator's objection is recorded here."
_PREFLIGHT_INVALID_NONEMPTY_FINALIZE_SUFFIX_WITH_DETAIL = (
    _TRUSTED_NOTICE_SEPARATOR
    + _TRUSTED_NOTICE_MARKER
    + _PREFLIGHT_NOTICE_HEADER
    + "\n\nCause: {detail}{suggestion_block}\n\n"
    + _PREFLIGHT_NOTICE_FOOTER
)
_PREFLIGHT_INVALID_NONEMPTY_FINALIZE_SUFFIX_BARE = (
    _TRUSTED_NOTICE_SEPARATOR + _TRUSTED_NOTICE_MARKER + _PREFLIGHT_NOTICE_HEADER + "\n\n" + _PREFLIGHT_NOTICE_FOOTER
)

# R2-F14 (elspeth-5403f346c0): the advisor end gate used to borrow
# ``_PREFLIGHT_NOTICE_HEADER`` for a completion review that never rendered, telling the
# user "Runtime preflight failed" on a build whose preflight had just PASSED
# (green side rail, contradictory note). This notice is the honest alternative:
# it states what actually happened and what to do, and — unlike the preflight
# wrapper — carries no interpolated diagnostic, so the whole suffix is fixed
# operator-authored prose.
_ADVISOR_SIGNOFF_PENDING_NOTICE: Final = (
    "Completion advisory review did not clear after the available attempts. "
    "Composer completion is withheld. Review the pipeline and retry the evidence-scoped advisor review."
)
_ADVISOR_SIGNOFF_PENDING_FINALIZE_SUFFIX = _TRUSTED_NOTICE_SEPARATOR + _TRUSTED_NOTICE_MARKER + _ADVISOR_SIGNOFF_PENDING_NOTICE

# Primary-model prose produced after hidden advisor findings entered its
# context is not an operator transcript.  The model may quote, paraphrase, or
# rebut those findings even after the pipeline is repaired and the next
# checkpoint returns CLEAN.  These fixed backend-authored messages are the
# only prose published for that repair cohort; the state, tool calls, provider
# audit, and deterministic validation result remain unchanged.
ADVISOR_REPAIR_INTERMEDIATE_PUBLIC_MESSAGE: Final = "ELSPETH is applying a pipeline correction."
ADVISOR_REPAIR_SUCCESS_PUBLIC_MESSAGE: Final = "The pipeline is configured and ready."
ADVISOR_REPAIR_REVIEW_PUBLIC_MESSAGE: Final = (
    "The pipeline update is ready for the required review. Resolve the pending review before running it."
)
# elspeth-5a372d3267: published instead of the bare review message when the
# authoring-masked re-validation behind a pending-review handoff found
# failures in the stages the strict ledger never reached — "ready for the
# required review" would falsely imply the review is the only thing between
# the user and execution.
ADVISOR_REPAIR_REVIEW_WITH_FINDINGS_PUBLIC_MESSAGE: Final = (
    "A required interpretation review is pending. Validation also found issues that must be fixed before this pipeline can run: {detail}"
)
# elspeth-88592f5be7: published when the advisor-repair turn ends with
# ``runtime_preflight=None`` — no deterministic validation ran this turn, so
# readiness is UNKNOWN, not ready. ``None`` is documented as fail-closed
# unknown by the END advisor gate; this surface must not read the same
# sentinel as an affirmative readiness claim.
ADVISOR_REPAIR_UNVERIFIED_PUBLIC_MESSAGE: Final = "ELSPETH completed the advisor repair turn. Pipeline readiness was not determined this turn; run validation to confirm the pipeline state."

_MAX_INTENT_CLASSIFICATION_CHARS: Final = 4_096

# THREAT-002: apostrophe confusables. U+2019 was once patched as a lone
# codepoint, not the class — any codepoint here that the grammars miss
# suppresses revocation (fail-open to EXPLICIT_MUTATION). NFKC cannot
# substitute (only U+FF07 folds). All members verified to survive
# _strip_quoted_text; re-verify that if _QUOTE_TERMINATORS is ever touched.
_APOSTROPHE_CLASS: Final = "'\u2019\u02bc\uff07\u00b4\u2032"
_APOSTROPHE_GRAMMAR: Final = f"[{_APOSTROPHE_CLASS}]"

# The canonical mutation-verb vocabulary, as data. BOTH the authority
# pattern (_MUTATION_ACTION_PATTERN) and the revocation pattern
# (_WHOLE_REQUEST_ACTION_GRAMMAR) are built from this tuple, so the
# revocation vocabulary can never silently become a subset of the
# authority vocabulary again (F4). Multi-word phrases stay ahead of their
# prefixes so alternation matches longest-first.
_MUTATION_ACTION_PHRASES: Final[tuple[str, ...]] = (
    "set this up",
    "set it up",
    "set up",
    "setup",
    "build",
    "create",
    "make",
    "wire",
    "add",
    "update",
    "modify",
    "change",
    "run",
    "execute",
    "process",
    "route",
    "split",
    "save",
)
_MUTATION_ACTION_PATTERN: Final = "(?:" + "|".join(r"\s+".join(map(re.escape, phrase.split())) for phrase in _MUTATION_ACTION_PHRASES) + ")"
_CONTROLLED_OBJECT_BODY_GRAMMAR: Final = (
    r"(?:"
    r"[a-z0-9_.-]+\s+to\s+[a-z0-9_.-]+\s+pipeline|"
    r"(?:(?:requested|csv|jsonl?|parquet)\s+)?(?:pipeline|composition|workflow|automation)|"
    r"source|input(?:\s+rows?)?|output|node|edge|transform|filter|sink|rows?"
    r")"
)
_CONTROLLED_OBJECT_GRAMMAR: Final = rf"(?:(?:a|an|the|this)\s+)?{_CONTROLLED_OBJECT_BODY_GRAMMAR}"
_CONTROLLED_COMPLEMENT_GRAMMAR: Final = (
    r"(?:"
    r"\s+now|"
    r"\s+for\s+this\s+file|"
    r"\s+to\s+(?:(?:a|an|the|this)\s+)?(?:output|input|source|node|sink)|"
    r"\s+by\s+[a-z0-9_.-]+|"
    r"\s+from\s+[a-z0-9_./-]+|"
    r"\s+with\s+[a-z0-9_.-]+|"
    r"\s+using\s+[a-z0-9_.-]+"
    r")?"
)
_DIRECT_MUTATION_GRAMMAR: Final = rf"{_MUTATION_ACTION_PATTERN}\b\s+{_CONTROLLED_OBJECT_GRAMMAR}{_CONTROLLED_COMPLEMENT_GRAMMAR}"
_POSITIVE_LEAD_GRAMMAR: Final = r"(?:(?:please[\s,]+)|(?:now|actually|just|simply)\s+|go\s+ahead\s+(?:and\s+)?)*"
_FIRST_PERSON_REQUEST_GRAMMAR: Final = rf"(?:(?:i|we)\s+(?:want|need|would\s+like)\s+(?:you\s+)?to\s+|let{_APOSTROPHE_GRAMMAR}s\s+)"
_SINGLE_CLAUSE_IMPERATIVE_PATTERN: Final = re.compile(
    rf"{_POSITIVE_LEAD_GRAMMAR}(?:{_FIRST_PERSON_REQUEST_GRAMMAR})?"
    rf"{_POSITIVE_LEAD_GRAMMAR}{_DIRECT_MUTATION_GRAMMAR}[.!]?",
    re.IGNORECASE,
)
_SINGLE_CLAUSE_POLITE_REQUEST_PATTERN: Final = re.compile(
    rf"{_POSITIVE_LEAD_GRAMMAR}(?:can|could|would|will)\s+you\s+(?:please\s+)?"
    rf"{_POSITIVE_LEAD_GRAMMAR}{_DIRECT_MUTATION_GRAMMAR}[.!?]?",
    re.IGNORECASE,
)
_NARROW_COMPLETE_REQUEST_PATTERNS: Final = (
    re.compile(r"build!", re.IGNORECASE),
    re.compile(r"just\s+build\s+the\s+whole\s+thing[.!]?", re.IGNORECASE),
    re.compile(
        r"set\s+(?:this|it)\s+up\s+to\s+actually\s+run\s+from\s+"
        r"[a-z0-9_./-]+\.(?:csv|jsonl?|parquet)[.!]?",
        re.IGNORECASE,
    ),
    re.compile(r"how\s+does\s+this\s+work\?\s+now\s+build\s+it[.!]?", re.IGNORECASE),
    re.compile(r"first\s+inspect\s+the\s+file,\s+then\s+build\s+(?:a|the)\s+pipeline[.!]?", re.IGNORECASE),
    re.compile(r"create\s+(?:a|the)\s+workflow\s+that\s+rates\s+how\s+cool\s+pages\s+are[.!]?", re.IGNORECASE),
)
_COMPLETE_EXPLICIT_REQUEST_PATTERNS: Final = (
    _SINGLE_CLAUSE_IMPERATIVE_PATTERN,
    _SINGLE_CLAUSE_POLITE_REQUEST_PATTERN,
    *_NARROW_COMPLETE_REQUEST_PATTERNS,
)
_REFERENTIAL_EXPLICIT_MUTATION_SIGNAL: Final = re.compile(
    rf"\b(?:{_MUTATION_ACTION_PATTERN})\b\s+(?:the\s+)?requested\s+"
    r"(?:pipeline|composition|workflow|automation)\b"
    rf"|\b(?:{_MUTATION_ACTION_PATTERN})\b\s+(?:this|that)\s+{_CONTROLLED_OBJECT_BODY_GRAMMAR}\b"
    rf"|\b(?:{_MUTATION_ACTION_PATTERN})\b\s+the\s+(?:pipeline|composition|workflow|automation)\b"
    r"|\b(?:build|create|make|wire|run|execute)\s+it\b"
    r"|\bjust\s+build\s+the\s+whole\s+thing\b"
    r"|\Abuild!\Z",
    re.IGNORECASE,
)
_MULTI_CLAUSE_REQUEST_LEAD_GRAMMAR: Final = (
    rf"{_POSITIVE_LEAD_GRAMMAR}"
    rf"(?:(?:{_FIRST_PERSON_REQUEST_GRAMMAR})|(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?))?"
    rf"{_POSITIVE_LEAD_GRAMMAR}"
)
_DATA_SOURCE_FORMAT_GRAMMAR: Final = r"(?:csv|jsonl?|parquet)"
_DATA_SOURCE_REFERENCE_GRAMMAR: Final = (
    rf"(?:"
    rf"[a-z0-9_./-]+\.{_DATA_SOURCE_FORMAT_GRAMMAR}\b|"
    rf".+?\s+from\s+(?:(?:an?|the|named)\s+)*{_DATA_SOURCE_FORMAT_GRAMMAR}\b|"
    rf".*?\b{_DATA_SOURCE_FORMAT_GRAMMAR}\s+(?:inputs?|files?|rows?)\b"
    rf")(?=\s*(?:[,;:.]|\u2013|\u2014|\b(?:and|then)\b))"
)
_PIPELINE_OPERATION_GRAMMAR: Final = (
    r"(?:aggregate|batch|coerce|collect|expand|fan(?:\s+out)?|hand(?:\s+off)?|"
    r"interleave|merge|pass|process|route|send|transform|wait|write)"
)
_PIPELINE_OPERATION_OBJECT_GRAMMAR: Final = (
    r"(?:"
    r"(?:(?:a|an|the|this|these|those|each|every|both|all|one|two)\s+)?"
    r"(?:(?:combined|aggregated|transformed|expanded|input|output|source|typed|numeric)\s+)*"
    r"(?:rows?|records?|data|streams?|results?|outputs?|sinks?|sources?|inputs?|nodes?|edges?|batches?|fields?|values?|price)|"
    r"each(?=\s+through\b)|"
    r"[a-z0-9_./-]+\.(?:csv|jsonl?|parquet)\b|"
    r"(?:it|them)\s+(?:to|into|through)\s+"
    r"(?:(?:a|an|the|one)\s+)?(?:(?:csv|jsonl?|parquet)\s+)?(?:output|sink|file|path)"
    r")"
)
_MULTI_CLAUSE_PIPELINE_BUILD_PATTERN: Final = re.compile(
    rf"{_MULTI_CLAUSE_REQUEST_LEAD_GRAMMAR}"
    r"(?:build|create|make)\s+(?:a|an|the|this)\s+"
    r"(?:(?:requested|csv|jsonl?|parquet)\s+)?(?:pipeline|workflow|automation)\s+"
    r"(?:that|which)\b.+",
    re.IGNORECASE,
)
_MULTI_CLAUSE_PIPELINE_READ_PATTERN: Final = re.compile(
    rf"{_MULTI_CLAUSE_REQUEST_LEAD_GRAMMAR}read\s+{_DATA_SOURCE_REFERENCE_GRAMMAR}.+",
    re.IGNORECASE,
)
_MULTI_CLAUSE_PIPELINE_OPERATION_PATTERN: Final = re.compile(
    rf"(?:[,;:.]|\u2013|\u2014|\b(?:and|then)\b)\s*"
    rf"(?:(?:and|then)\s+)?{_PIPELINE_OPERATION_GRAMMAR}\b\s+"
    rf"{_PIPELINE_OPERATION_OBJECT_GRAMMAR}",
    re.IGNORECASE,
)
_NEGATED_DO_GRAMMAR: Final = rf"(?:do\s+not|don{_APOSTROPHE_GRAMMAR}t)"
# Same canonical vocabulary as the authority pattern \u2014 see
# _MUTATION_ACTION_PHRASES. A verb that can authorize can be revoked.
_WHOLE_REQUEST_ACTION_GRAMMAR: Final = _MUTATION_ACTION_PATTERN
_WHOLE_REQUEST_TARGET_GRAMMAR: Final = (
    r"(?:anything|any\s+changes?|it|this|that|(?:(?:this|that|the|my)\s+)?(?:request|build|pipeline|workflow|automation))"
)
_WHOLE_REQUEST_ACTION_CLAUSE_GRAMMAR: Final = (
    rf"{_WHOLE_REQUEST_ACTION_GRAMMAR}"
    rf"(?:\s+{_WHOLE_REQUEST_TARGET_GRAMMAR})?"
    r"\s*(?:[.!;]|$)"
)
_REQUEST_REVOCATION_PATTERN: Final = re.compile(
    rf"(?:"
    rf"\b(?:{_NEGATED_DO_GRAMMAR}|never)\s+{_WHOLE_REQUEST_ACTION_CLAUSE_GRAMMAR}|"
    rf"\b(?:{_NEGATED_DO_GRAMMAR}|never)\s+do\s+(?:that|this|it|so)\s*(?:[.!;]|$)|"
    rf"\b{_NEGATED_DO_GRAMMAR}\s+want\s+(?:you\s+)?to\s+{_WHOLE_REQUEST_ACTION_CLAUSE_GRAMMAR}|"
    rf"\bi\s+{_NEGATED_DO_GRAMMAR}\s+want\s+(?:that|this|it)\s*(?:[.!;]|$)|"
    rf"\b(?:actually,\s*)?{_NEGATED_DO_GRAMMAR}\s*(?:[.!;]|$)|"
    rf"\b(?:please\s+)?{_NEGATED_DO_GRAMMAR}\s+(?:proceed|continue)\s*(?:[.!;]|$)|"
    r"\bshould\s+not\s+be\s+(?:built|created|made|run|executed)\b|"
    r"\bcancel\s+(?:(?:that|this|the|my)\s+)?(?:request|build|pipeline)\b|"
    r"\bforget\s+(?:(?:that|this|the|my)\s+)?(?:request|build|pipeline)\b|"
    r"\bdisregard\s+(?:(?:that|this|the|my)\s+)?(?:request|build|pipeline)\b|"
    r"\bi\s+no\s+longer\s+(?:want|need)\b|"
    r"\bonly\s+an?\s+example\b|"
    r"\binstead,\s+(?:please\s+)?(?:describe|explain|show|tell)\b|"
    r"\b(?:never\s+mind|cancel\s+(?:that|this|it)|scratch\s+that|forget\s+it|undo\s+that|revert\s+that|"
    r"belay\s+that|hold\s+off|ignore\s+that|withdraw\s+that\s+request|i\s+take\s+that\s+back|"
    r"i\s+changed\s+my\s+mind|on\s+second\s+thought|skip\s+it|without\s+changing\s+anything)\b|"
    r"\b(?:please\s+)?(?:stop|cancel)\s*(?:[.!;]|$)|"
    r"\babort\b"
    rf")",
    re.IGNORECASE,
)
_QUOTE_TERMINATORS: Final = {
    '"': '"',
    "`": "`",
    "\u201c": "\u201d",
    "\u2018": "\u2019",
}
_REGISTERED_RECIPE_DELIMITER_PATTERN: Final = re.compile(
    r"customer\s+rows\s*\(csv\):",
    re.IGNORECASE,
)
_REGISTERED_RECIPE_ENVELOPE_PATTERN: Final = re.compile(
    r"please create a pipeline that processes the following customer rows\. "
    r"each row should be processed two ways in parallel and combined into a single merged output row at "
    r"[^\s:]+\.jsonl: path a keeps the original row unchanged, path b truncates the "
    r"[a-z_][a-z0-9_]* field to \d+ characters? with suffix '[^'\r\n]*'\. "
    r"combine both branches under separate keys `[a-z_][a-z0-9_]*` and `[a-z_][a-z0-9_]*` "
    r"in each merged output row -- one input row produces one output row containing both branches side-by-side\. "
    r"customer rows \(csv\):",
    re.IGNORECASE,
)
_ANY_MUTATION_ACTION_PATTERN: Final = re.compile(rf"\b{_MUTATION_ACTION_PATTERN}\b", re.IGNORECASE)

_AugmentationBranch = Literal[
    "no_mutation_empty_state_augmentation",
    "preflight_invalid_empty_state_augmentation",
    "preflight_invalid_non_empty_state_augmentation",
    "state_claim_grounding_correction",
    "orphaned_interpretation_review_augmentation",
    "advisor_signoff_blocked_augmentation",
    "interpretation_review_handoff_augmentation",
    "proof_repair_exhausted_augmentation",
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


class PipelineMutationIntentDecision(Enum):
    """Closed request-intent decision for authoring-surface selection.

    Not an authority value: ``__bool__`` raises so it can never be consumed
    as a truthy consent signal. Real mutation authority is
    ``trust_mode == "explicit_approve"`` (``web/composer/tool_batch.py:563``
    and ``:965``); see :func:`classify_pipeline_mutation_intent`.
    """

    EXPLICIT_MUTATION = "explicit_mutation"
    CONVERSATIONAL = "conversational"
    AMBIGUOUS = "ambiguous"

    def __bool__(self) -> bool:
        raise TypeError("PipelineMutationIntentDecision must be compared to an explicit member")


def _split_wrapped_diagnostic(
    suffix: str,
    *,
    header: str,
    footer: str,
) -> tuple[VisibleMessageSegment, ...] | None:
    """Split one exact fixed-wrapper shape without trusting its diagnostic."""
    prefix = _TRUSTED_NOTICE_SEPARATOR + _TRUSTED_NOTICE_MARKER + header + "\n\nCause: "
    postfix = "\n\n" + footer
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


def _split_grounding_correction(suffix: str) -> tuple[VisibleMessageSegment, ...] | None:
    """Split the exact grounding wrapper without trusting violation bullets."""
    prefix = _TRUSTED_NOTICE_SEPARATOR + _TRUSTED_NOTICE_MARKER + GROUNDING_CORRECTION_HEADER + "\n\n"
    postfix = "\n\n" + GROUNDING_CORRECTION_INSTRUCTION
    if not suffix.startswith(prefix) or not suffix.endswith(postfix):
        return None
    violation_bullets = suffix[len(prefix) : -len(postfix)]
    if not violation_bullets.startswith("- "):
        return None
    return (
        TrustedSystemNoticeSegment(GROUNDING_CORRECTION_HEADER),
        AssistantTextSegment(violation_bullets),
        TrustedSystemNoticeSegment(GROUNDING_CORRECTION_INSTRUCTION),
    )


def _canonical_trusted_suffix_segments(suffix: str) -> tuple[VisibleMessageSegment, ...] | None:
    """Recognize the closed set of canonical composer synthesis suffixes."""
    if suffix == _EMPTY_STATE_FINALIZE_SUFFIX:
        return (TrustedSystemNoticeSegment(_EMPTY_STATE_NOTICE_BODY),)
    empty_with_blocker = _split_wrapped_diagnostic(
        suffix,
        header=_EMPTY_STATE_NOTICE_HEADER,
        footer=_EMPTY_STATE_NOTICE_NEXT_STEP,
    )
    if empty_with_blocker is not None:
        return empty_with_blocker
    if suffix == _ADVISOR_SIGNOFF_PENDING_FINALIZE_SUFFIX:
        return (TrustedSystemNoticeSegment(_ADVISOR_SIGNOFF_PENDING_NOTICE),)
    if suffix == _PREFLIGHT_INVALID_NONEMPTY_FINALIZE_SUFFIX_BARE:
        return (TrustedSystemNoticeSegment(f"{_PREFLIGHT_NOTICE_HEADER}\n\n{_PREFLIGHT_NOTICE_FOOTER}"),)
    preflight_with_diagnostic = _split_wrapped_diagnostic(
        suffix,
        header=_PREFLIGHT_NOTICE_HEADER,
        footer=_PREFLIGHT_NOTICE_FOOTER,
    )
    if preflight_with_diagnostic is not None:
        return preflight_with_diagnostic
    return _split_grounding_correction(suffix)


def visible_message_segments(*, content: str, raw_content: str | None) -> tuple[VisibleMessageSegment, ...]:
    """Project durable assistant text into provenance-bearing visible segments.

    The ``raw_content`` prefix pairing is necessary but not sufficient: empty
    or stale prefixes can match attacker-controlled content.  A suffix mints
    trusted chrome only when it exactly matches one of the closed canonical
    composer shapes above. Dynamic blocker, validator, and grounding
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


def compose_preflight_failure_message(content: str, *, runtime_result: ValidationResult) -> str:
    """Build the user-facing message for the non-empty-state preflight-invalid path."""
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
    if detail:
        suggestion_block = f"\n\nSuggested fix: {suggestion}" if suggestion else ""
        suffix = _PREFLIGHT_INVALID_NONEMPTY_FINALIZE_SUFFIX_WITH_DETAIL.format(
            detail=detail,
            suggestion_block=suggestion_block,
        )
    else:
        suffix = _PREFLIGHT_INVALID_NONEMPTY_FINALIZE_SUFFIX_BARE
    if not content:
        return suffix.lstrip("\n").lstrip("-").lstrip()
    return content + suffix


def compose_advisor_signoff_pending_message(content: str) -> str:
    """Build the user-facing message for a validated-but-unsigned build.

    Deliberately NOT ``compose_preflight_failure_message``: the runtime
    preflight passed, so a "Runtime preflight failed" header would be a false
    statement about the user's pipeline (R2-F14, elspeth-5403f346c0). The
    suffix is entirely fixed prose — no validator detail is interpolated,
    because there is no validator objection to report.
    """
    if not content:
        return _ADVISOR_SIGNOFF_PENDING_FINALIZE_SUFFIX.lstrip("\n").lstrip("-").lstrip()
    return content + _ADVISOR_SIGNOFF_PENDING_FINALIZE_SUFFIX


def _strip_quoted_text(message: str) -> tuple[str, bool, bool]:
    """Replace quoted spans in one bounded pass and report closure and presence."""
    output: list[str] = []
    closing_quote: str | None = None
    quoted_material_seen = False
    index = 0
    while index < len(message):
        character = message[index]
        if closing_quote is not None:
            output.append(" ")
            if character == "\\" and closing_quote in {'"', "'"} and index + 1 < len(message):
                output.append(" ")
                index += 2
                continue
            if character == closing_quote:
                closing_quote = None
            index += 1
            continue

        if character in _QUOTE_TERMINATORS:
            closing_quote = _QUOTE_TERMINATORS[character]
            quoted_material_seen = True
            output.append(" ")
            index += 1
            continue
        if character == "'":
            previous_is_word = index > 0 and message[index - 1].isalnum()
            next_is_word = index + 1 < len(message) and message[index + 1].isalnum()
            if not (previous_is_word and next_is_word):
                closing_quote = "'"
                quoted_material_seen = True
                output.append(" ")
                index += 1
                continue

        output.append(character)
        index += 1
    return "".join(output), closing_quote is None, quoted_material_seen


def _matches_complete_registered_recipe_request(message: str) -> bool:
    """Recognize one registered full-request production with shaped CSV data."""
    delimiters = tuple(_REGISTERED_RECIPE_DELIMITER_PATTERN.finditer(message))
    if len(delimiters) != 1:
        return False
    normalized_envelope = " ".join(message[: delimiters[0].end()].split())
    if _REGISTERED_RECIPE_ENVELOPE_PATTERN.fullmatch(normalized_envelope) is None:
        return False
    match = match_freeform_recipe_intent(message)
    if match is None or match.inline_blob is None:
        return False
    try:
        rows = list(csv.reader(StringIO(match.inline_blob.content)))
    except csv.Error:
        return False
    if len(rows) < 2 or len(rows[0]) < 2:
        return False
    column_count = len(rows[0])
    return all(len(row) == column_count for row in rows[1:])


def _matches_complete_multi_clause_pipeline_request(message: str) -> bool:
    """Recognize a positive pipeline root followed by a complete specification."""
    if "?" in message or _REQUEST_REVOCATION_PATTERN.search(message) is not None:
        return False
    if _MULTI_CLAUSE_PIPELINE_BUILD_PATTERN.fullmatch(message) is not None:
        return True
    return (
        _MULTI_CLAUSE_PIPELINE_READ_PATTERN.fullmatch(message) is not None
        and _MULTI_CLAUSE_PIPELINE_OPERATION_PATTERN.search(message) is not None
    )


def classify_pipeline_mutation_intent(message: str) -> PipelineMutationIntentDecision:
    """Select the authoring surface for a request — NOT an authority gate.

    This classifier ROUTES (planner vs compose_loop) and gates one
    repair-prompt nudge; its consumers are surface selection, referential
    context threading, and disclosure only. Nothing here grants or denies
    mutation: real mutation authority is ``trust_mode == "explicit_approve"``
    (``web/composer/tool_batch.py:563`` and ``:965``) plus the tool-call
    guards (interpretation review, preflight, advisor veto). Its
    misclassifications fail SAFE — toward clarification — and its recall on
    open phrasing is poor by construction: a closed vocabulary cannot cover
    open human language. **Do not wire a consent or authority gate to this
    predicate.**

    Classification contract: EXPLICIT_MUTATION requires the complete bounded
    request to match a closed positive production. Quoted material cannot be
    elided to manufacture a match; only a complete registered recipe envelope
    may contain its prescribed quotes. Multi-clause productions require a
    pipeline-build or data-source root and reject request revocation.
    Unmatched governing prefixes, bare questions, unrelated objects, and
    oversized input fail closed to clarification on the conversational path.
    """
    if len(message) > _MAX_INTENT_CLASSIFICATION_CHARS:
        return PipelineMutationIntentDecision.AMBIGUOUS

    unquoted_text, quotes_balanced, quoted_material_seen = _strip_quoted_text(message)
    if not quotes_balanced:
        return PipelineMutationIntentDecision.AMBIGUOUS
    unquoted = " ".join(unquoted_text.split())
    if not unquoted:
        return PipelineMutationIntentDecision.CONVERSATIONAL
    multi_clause_request = _matches_complete_multi_clause_pipeline_request(unquoted)
    if _ANY_MUTATION_ACTION_PATTERN.search(unquoted) is None and not multi_clause_request:
        return PipelineMutationIntentDecision.CONVERSATIONAL
    if _matches_complete_registered_recipe_request(message):
        return PipelineMutationIntentDecision.EXPLICIT_MUTATION
    if quoted_material_seen:
        return PipelineMutationIntentDecision.AMBIGUOUS
    if multi_clause_request or any(pattern.fullmatch(unquoted) is not None for pattern in _COMPLETE_EXPLICIT_REQUEST_PATTERNS):
        return PipelineMutationIntentDecision.EXPLICIT_MUTATION
    return PipelineMutationIntentDecision.AMBIGUOUS


def carries_build_action(message: str) -> bool:
    """Return whether a message asks for pipeline construction at all.

    This is a DISCLOSURE decision, not an authority decision, and it is
    deliberately not expressed in terms of
    :class:`PipelineMutationIntentDecision` (issue elspeth-a6a2ed6b1d).
    That enum reaches ``AMBIGUOUS`` through two early returns that never
    test content — the input-size ceiling and the unbalanced-quote guard
    — so gating disclosure on ``is not CONVERSATIONAL`` let an oversize
    or unbalanced-quote greeting fail open into a build-failure notice.
    Here the content test always runs.

    Three deliberate calls, none of them inherited:

    * **No size ceiling.** ``classify_pipeline_mutation_intent`` caps
      input because granting *mutation authority* over unbounded text is
      unsafe. Disclosure grants nothing, and failing closed on length
      would re-create the withholding defect for the realistic "here is
      my 6KB CSV, build me a pipeline that ..." shape. The search is a
      single linear alternation.
    * **Unbalanced quotes fall back to the raw message.** A balanced
      strip is worth keeping: it stops quoted material being read as a
      command, so ``What does "build a pipeline" mean?`` carries no
      build action. When the strip is unreliable it would instead
      swallow the tail of the message, hiding a real request, so the raw
      text is searched — erring toward disclosure, the direction this
      issue exists to correct.
    * **Request revocation is NOT consulted**, though it was tried.
      ``_REQUEST_REVOCATION_PATTERN`` looks like free reuse, but the
      intent grammar only ever applies it inside
      ``_matches_complete_multi_clause_pipeline_request``, where a
      fullmatch production has already constrained the message shape.
      As a bare search over free prose it misfires on ordinary English:
      it suppressed disclosure for acceptance intents g07/g07r, whose
      "Build me a pipeline that runs Amazon Textract ..." request also
      contains "... rather than have the whole run stop." Suppressing a
      real build request re-creates the exact defect this predicate
      exists to fix, so the revocation false-positives below are the
      lesser harm.

    Known imprecision, deliberately not hand-tuned: the underlying verb
    alternation has no object requirement, so revocations ("Do not build
    anything"), non-imperative uses ("I make heavy use of CSV files"),
    and capability questions ("Are you able to run pipelines yourself?")
    all report True and will draw an empty-state notice they should not.
    Narrowing by excluding interrogatives is ruled out by evidence
    rather than taste — "Can you build a pipeline that ...?" is a
    genuine build request this predicate must keep admitting. Narrowing
    the verb list by intuition is how this grammar became fragile in the
    first place; it wants a sampled corpus of conversational traffic,
    which does not exist yet.
    """
    unquoted_text, quotes_balanced, _quoted_material_seen = _strip_quoted_text(message)
    candidate = " ".join((unquoted_text if quotes_balanced else message).split())
    if not candidate:
        return False
    return _ANY_MUTATION_ACTION_PATTERN.search(candidate) is not None


def is_referential_pipeline_mutation_intent(message: str) -> bool:
    """Return whether an admitted mutation request depends on prior prose.

    Surface selection remains wholly owned by
    :func:`classify_pipeline_mutation_intent` — neither function grants
    mutation authority (that is ``trust_mode``; see the classifier's
    docstring). This second decision reuses that closed grammar, then
    identifies only deictic request shapes whose current text cannot specify
    the requested topology by itself. Complete registered recipes are
    self-contained even if their inline data happens to contain a matching
    phrase.
    """
    if classify_pipeline_mutation_intent(message) is not PipelineMutationIntentDecision.EXPLICIT_MUTATION:
        return False
    if _matches_complete_registered_recipe_request(message):
        return False
    unquoted_text, quotes_balanced, _quoted_material_seen = _strip_quoted_text(message)
    if not quotes_balanced:
        return False
    normalized = " ".join(unquoted_text.split())
    return _REFERENTIAL_EXPLICIT_MUTATION_SIGNAL.search(normalized) is not None


def _tool_failure_detail(payload: Mapping[str, Any]) -> str:
    """Extract a concise semantic failure detail from a ToolResult payload."""
    match payload:
        case {"data": {"error": error}}:
            return f": {error}"
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
                and not is_discovery_tool(invocation.tool_name)
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
                    component_id=None,
                    component_type=None,
                    detail=detail,
                )
            ],
        ),
    )


def last_failure_was_pre_state_interpretation_review(tool_invocations: tuple[ComposerToolInvocation, ...]) -> bool:
    """Return True when review was called before any state row existed."""
    for invocation in reversed(tool_invocations):
        if is_discovery_tool(invocation.tool_name):
            continue
        return (
            invocation.tool_name == "request_interpretation_review"
            and invocation.status is ComposerToolStatus.ARG_ERROR
            and invocation.error_message is not None
            and "composition_state_id" in invocation.error_message
            and "missing current_state_id" in invocation.error_message
        )
    return False


def pre_state_interpretation_review_repair_message(*, next_turn: int, max_repair_turns: int) -> str:
    return (
        "[composer-system] You called request_interpretation_review before a "
        "persisted composition state existed. Do not reply to the user yet. "
        "First call set_pipeline or another state-staging tool to create the "
        "affected LLM transform with prompt_template containing the "
        "{{interpretation:<term>}} placeholder. Wait for that tool result, "
        "then call request_interpretation_review again. "
        f"This is forced repair turn {next_turn} of {max_repair_turns}."
    )
