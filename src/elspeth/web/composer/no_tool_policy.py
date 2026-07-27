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

_MAX_INTENT_CLASSIFICATION_CHARS: Final = 4_096
_MUTATION_ACTION_PATTERN: Final = (
    r"(?:set\s+(?:this|it)\s+up|set\s+up|setup|build|create|make|wire|add|update|modify|change|run|execute|process|route|split|save)"
)
_CONTROLLED_OBJECT_GRAMMAR: Final = (
    r"(?:(?:a|an|the|this)\s+)?"
    r"(?:"
    r"[a-z0-9_.-]+\s+to\s+[a-z0-9_.-]+\s+pipeline|"
    r"(?:(?:requested|csv|jsonl?|parquet)\s+)?(?:pipeline|composition|workflow|automation)|"
    r"source|input(?:\s+rows?)?|output|node|edge|transform|filter|sink|rows?"
    r")"
)
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
_FIRST_PERSON_REQUEST_GRAMMAR: Final = r"(?:(?:i|we)\s+(?:want|need|would\s+like)\s+(?:you\s+)?to\s+|let(?:'|\u2019)s\s+)"
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
    """Closed request-intent decision used at Composer lifecycle gates."""

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


def classify_pipeline_mutation_intent(message: str) -> PipelineMutationIntentDecision:
    """Classify whether the user explicitly authorizes pipeline mutation.

    Authority requires the complete bounded request to match a closed positive
    production. Quoted material cannot be elided to manufacture authority;
    only a complete registered recipe envelope may contain its prescribed
    quotes. Unmatched governing prefixes, trailing clauses, bare questions,
    unrelated objects, and oversized input fail closed to clarification on
    the conversational path.
    """
    if len(message) > _MAX_INTENT_CLASSIFICATION_CHARS:
        return PipelineMutationIntentDecision.AMBIGUOUS

    unquoted_text, quotes_balanced, quoted_material_seen = _strip_quoted_text(message)
    if not quotes_balanced:
        return PipelineMutationIntentDecision.AMBIGUOUS
    unquoted = " ".join(unquoted_text.split())
    if not unquoted:
        return PipelineMutationIntentDecision.CONVERSATIONAL
    if _ANY_MUTATION_ACTION_PATTERN.search(unquoted) is None:
        return PipelineMutationIntentDecision.CONVERSATIONAL
    if _matches_complete_registered_recipe_request(message):
        return PipelineMutationIntentDecision.EXPLICIT_MUTATION
    if quoted_material_seen:
        return PipelineMutationIntentDecision.AMBIGUOUS
    if any(pattern.fullmatch(unquoted) is not None for pattern in _COMPLETE_EXPLICIT_REQUEST_PATTERNS):
        return PipelineMutationIntentDecision.EXPLICIT_MUTATION
    return PipelineMutationIntentDecision.AMBIGUOUS


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
