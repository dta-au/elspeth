"""Strict admission of advisor checkpoints and bounded user-facing notes."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Annotated, Final, Literal, TypedDict, get_args

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator, model_validator

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.advisor_checkpoint_telemetry import AdvisorCheckpointPhase
from elspeth.web.validation import _PII_WARNING_PATTERNS

AdvisorFindingCategory = Literal["request_not_met", "error_handling", "prompt_defect", "schema_mismatch", "other"]

ADVISOR_FINDING_CATEGORIES: Final[frozenset[str]] = frozenset(get_args(AdvisorFindingCategory))
ADVISOR_NOTE_MAX_CHARS: Final[int] = 600


def _require_unicode_scalars(text: str) -> None:
    """Reject escaped unpaired surrogates before audit UTF-8 canonicalization."""
    if any(0xD800 <= ord(character) <= 0xDFFF for character in text):
        raise ValueError("Advisor text must contain only Unicode scalar values")


class AdvisorCheckpointResponse(BaseModel):
    """The five required fields accepted from checkpoint providers."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    verdict: Literal["CLEAN", "FLAGGED"]
    category: AdvisorFindingCategory
    steps: list[str]
    findings: str
    note: str | None

    @field_validator("findings", "note")
    @classmethod
    def require_unicode_text(cls, value: str | None) -> str | None:
        """Admit only Unicode scalar text in findings and optional notes."""
        if value is not None:
            _require_unicode_scalars(value)
        return value

    @field_validator("steps")
    @classmethod
    def require_unicode_steps(cls, value: list[str]) -> list[str]:
        """Admit only Unicode scalar text in every offered step identifier."""
        for step in value:
            _require_unicode_scalars(step)
        return value


class AdvisorEndResponse(AdvisorCheckpointResponse):
    """END adds prose permission without changing graph verdict semantics."""

    publication_verdict: Literal["ADMIT", "WITHHOLD"]


class AdvisorAbandonmentResponse(BaseModel):
    """A request-scoped stop decision, independent of graph and prose clearance."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    decision: Literal["STOP_CONFIRMED", "CONTINUE", "CONTINUE_WITH_LIMITATION"]
    reason: str = Field(min_length=1, max_length=2000)
    next_action: str | None = Field(max_length=2000)
    question: str | None = Field(max_length=2000)

    @field_validator("reason", "next_action", "question")
    @classmethod
    def require_nonempty_unicode(cls, value: str | None) -> str | None:
        if value is not None:
            _require_unicode_scalars(value)
            if not value.strip():
                raise ValueError("Advisor decision text must not be blank")
        return value

    @model_validator(mode="after")
    def require_actionable_decision(self) -> AdvisorAbandonmentResponse:
        if self.decision in ("CONTINUE", "CONTINUE_WITH_LIMITATION") and (self.next_action is None or self.question is not None):
            raise ValueError("A continuation requires a supported action, not a question for the user")
        if self.decision == "STOP_CONFIRMED" and self.next_action is not None:
            raise ValueError("A confirmed stop must not require another authoring action")
        return self


def parse_advisor_abandonment_response(text: str) -> AdvisorAbandonmentResponse | None:
    parsed, error = parse_json_strict(text)
    if error is not None:
        return None
    try:
        return AdvisorAbandonmentResponse.model_validate(parsed)
    except ValidationError:
        return None


def advisor_abandonment_response_format() -> dict[str, object]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "advisor_abandonment_response",
            "strict": True,
            "schema": AdvisorAbandonmentResponse.model_json_schema(),
        },
    }


@dataclass(frozen=True, slots=True)
class AdvisorResponseAdmission:
    """Schema conformance and the semantically accepted response, if any."""

    schema_valid: bool
    response: AdvisorCheckpointResponse | None


def parse_advisor_checkpoint_response(text: str, *, phase: AdvisorCheckpointPhase) -> AdvisorResponseAdmission:
    """Admit Tier-3 JSON without retaining provider-controlled error details."""
    parsed, error = parse_json_strict(text)
    if error is not None:
        return AdvisorResponseAdmission(schema_valid=False, response=None)
    response: AdvisorCheckpointResponse
    try:
        if phase == "end":
            response = AdvisorEndResponse.model_validate(parsed)
        elif phase == "early":
            response = AdvisorCheckpointResponse.model_validate(parsed)
        else:
            raise ValueError("Unknown owned advisor checkpoint phase")
    except ValidationError:
        return AdvisorResponseAdmission(schema_valid=False, response=None)

    # The old prose rule let any FLAGGED marker dominate ambiguous CLEAN text.
    # A closed enum, duplicate-key rejection and these consistency checks
    # preserve that fail-closed direction without retaining a prose fallback.
    if response.verdict == "CLEAN" and (response.note is not None or response.steps):
        return AdvisorResponseAdmission(schema_valid=True, response=None)
    if response.verdict == "FLAGGED" and not response.findings.strip():
        return AdvisorResponseAdmission(schema_valid=True, response=None)
    if isinstance(response, AdvisorEndResponse) and response.publication_verdict == "WITHHOLD" and not response.findings.strip():
        return AdvisorResponseAdmission(schema_valid=True, response=None)
    return AdvisorResponseAdmission(schema_valid=True, response=response)


class AdvisorJSONSchemaFormat(TypedDict):
    """Owned provider schema specification."""

    name: Literal["advisor_checkpoint_response", "advisor_end_response"]
    strict: Literal[True]
    schema: dict[str, object]


class AdvisorResponseFormat(TypedDict):
    """Owned LiteLLM structured-response envelope."""

    type: Literal["json_schema"]
    json_schema: AdvisorJSONSchemaFormat


def advisor_response_format(*, phase: AdvisorCheckpointPhase) -> AdvisorResponseFormat:
    """Build the same strict provider schema for checkpoints and boot probes."""
    if phase not in ("early", "end"):
        raise ValueError("Unknown owned advisor checkpoint phase")
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "advisor_end_response" if phase == "end" else "advisor_checkpoint_response",
            "strict": True,
            "schema": (AdvisorEndResponse if phase == "end" else AdvisorCheckpointResponse).model_json_schema(),
        },
    }


@dataclass(frozen=True, slots=True)
class SanitizedAdvisorNote:
    """Bounded note and actual substitutions made before its final cap."""

    note: str | None
    url_redactions: int
    email_redactions: int


_NOTE_ANSI_CSI_RE: Final[re.Pattern[str]] = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_NOTE_STRIPPED_CATEGORIES: Final[frozenset[str]] = frozenset({"Cc", "Cf", "Zl", "Zp"})
_NOTE_KEPT_CONTROLS: Final[frozenset[str]] = frozenset("\t\n")
# Scheme and ``www.`` forms only. A schemeless ``name.name/path`` is also an
# ELSPETH expression (``row.total/row.count``), and the note renders as plain
# text, so redacting bare hosts would erase expressions for no link protection.
_NOTE_URL_RE: Final[re.Pattern[str]] = re.compile(r"(?<![a-z0-9])(?:[a-z][a-z0-9+.\-]*://|www\.)[^\s<>\"']+", re.IGNORECASE)
# Reuse the existing egress email matcher without broadening validation.py's
# other egress surfaces or counting sentinel text already present in a note.
_NOTE_EMAIL_RE: Final[re.Pattern[str]] = dict(_PII_WARNING_PATTERNS)["email"]
_NOTE_BLANK_RUN_RE: Final[re.Pattern[str]] = re.compile(r"\n{3,}")


def _markdown_destination_end(match: re.Match[str]) -> int:
    """Locate the outer close while keeping nested or escaped URL parentheses."""
    depth = 0
    escaped = False
    for offset, character in enumerate(match.group()):
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "(":
            depth += 1
        elif character == ")":
            if depth == 0:
                return match.start() + offset
            depth -= 1
    return match.end()


def _redact_note_urls(text: str) -> tuple[str, int]:
    """Redact URL destinations, retaining a markdown link's own closing mark."""
    parts: list[str] = []
    cursor = 0
    count = 0
    while (match := _NOTE_URL_RE.search(text, cursor)) is not None:
        opening_end = match.start()
        while opening_end > 0 and text[opening_end - 1].isspace():
            opening_end -= 1
        end = _markdown_destination_end(match) if text.endswith("](", 0, opening_end) else match.end()
        parts.extend((text[cursor : match.start()], "[link removed]"))
        cursor = end
        count += 1
    parts.append(text[cursor:])
    return "".join(parts), count


def sanitize_advisor_note(note: str | None) -> SanitizedAdvisorNote:
    """Remove note-only links/addresses after invisible text is normalized."""
    if note is None:
        return SanitizedAdvisorNote(note=None, url_redactions=0, email_redactions=0)

    # Normalize first: filtering Zl/Zp before splitlines would join words.
    body = "\n".join(note.splitlines())
    # Strip ANSI before controls so ESC removal cannot leave visible [31m text.
    body = _NOTE_ANSI_CSI_RE.sub("", body)
    body = "".join(
        character
        for character in body
        if character in _NOTE_KEPT_CONTROLS or unicodedata.category(character) not in _NOTE_STRIPPED_CATEGORIES
    )
    # Filtering removable format characters can reassemble a fence sentinel.
    body = body.replace("BEGIN_UNTRUSTED_ADVISOR_FINDINGS", "").replace("END_UNTRUSTED_ADVISOR_FINDINGS", "")
    # URLs win overlaps (e.g. an email in a URL path); counts describe the
    # actual substitutions, including matches beyond the eventual note cap.
    body, url_redactions = _redact_note_urls(body)
    body, email_redactions = _NOTE_EMAIL_RE.subn("<redacted-sensitive:email>", body)
    body = _NOTE_BLANK_RUN_RE.sub("\n\n", body).strip()
    if len(body) > ADVISOR_NOTE_MAX_CHARS:
        body = body[: ADVISOR_NOTE_MAX_CHARS - 1].rstrip() + "…"
    return SanitizedAdvisorNote(note=body or None, url_redactions=url_redactions, email_redactions=email_redactions)


class ContinuationEvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    receipt_id: str = Field(min_length=1)
    source_kind: Literal["graph", "read", "provider", "validation"] = "read"
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_attempt_id: str | None
    coverage_binding: str


class ContinuationObligationProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    obligation_id: str = Field(min_length=1, max_length=128)
    parent_id: str | None
    description: str = Field(min_length=1)
    original_message_id: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    status: Literal["open", "blocked", "satisfied"]


class ContinuationActionBase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    obligation_ids: tuple[str, ...] = Field(min_length=1)
    evidence_ids: tuple[str, ...]
    input_hash: str
    graph_hash: str
    policy_hash: str
    tool_manifest_hash: str

    @model_validator(mode="after")
    def require_unique_obligation_ids(self) -> ContinuationActionBase:
        if len(set(self.obligation_ids)) != len(self.obligation_ids):
            raise ValueError("Advisor repeated an action obligation reference")
        return self


class PrimaryContinuationAction(ContinuationActionBase):
    kind: Literal["primary_turn"]
    instruction: str = Field(min_length=1)


class ReadContinuationAction(ContinuationActionBase):
    kind: Literal["read_tool"]
    tool_name: str = Field(min_length=1)
    arguments_json: str


class ReviewContinuationAction(ContinuationActionBase):
    kind: Literal["review"]
    candidate_ref: str = Field(pattern=r"^candidate:[0-9a-f]{64}$")
    evidence_binding: str


PrescribedContinuationAction = Annotated[
    PrimaryContinuationAction | ReadContinuationAction | ReviewContinuationAction,
    Field(discriminator="kind"),
]


class _ContinuationAdvisorAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.continuation-decision.v1"]
    decision: Literal["CONTINUE", "CONTINUE_WITH_LIMITATION", "STOP_CONFIRMED"]
    scope: Literal["subset", "whole_request"]
    obligation_ids: tuple[str, ...] = Field(min_length=1)
    obligations: tuple[ContinuationObligationProposal, ...]
    evidence: tuple[ContinuationEvidenceRef, ...]
    reason: str = Field(min_length=1)
    remaining_actionable_ids: tuple[str, ...]
    prescribed_action: PrescribedContinuationAction | None

    @model_validator(mode="after")
    def require_owned_decision_shape(self) -> _ContinuationAdvisorAnswer:
        _require_unicode_scalars(self.reason)
        if self.decision == "STOP_CONFIRMED":
            if self.prescribed_action is not None:
                raise ValueError("stop decision cannot prescribe an unattempted action")
            if self.scope == "whole_request" and self.remaining_actionable_ids:
                raise ValueError("whole-request stop cannot leave actionable work")
        elif self.prescribed_action is None:
            raise ValueError("continuation must prescribe a concrete action")
        return self


class ContinuationAdvisorDecision(_ContinuationAdvisorAnswer):
    decision_id: str = Field(min_length=1)


def parse_continuation_advisor_decision(
    text: str,
    *,
    actual_advisor_call_id: str,
) -> ContinuationAdvisorDecision | None:
    if not actual_advisor_call_id:
        raise AuditIntegrityError("continuation decision lacks its actual Advisor call")
    parsed, error = parse_json_strict(text)
    if error is not None or type(parsed) is not dict:
        return None
    try:
        answer = _ContinuationAdvisorAnswer.model_validate_json(text)
        return ContinuationAdvisorDecision(
            **answer.model_dump(mode="python"),
            decision_id=actual_advisor_call_id,
        )
    except ValidationError:
        return None


class ContinuationAdvisorJSONSchemaFormat(TypedDict):
    name: Literal["composer_continuation_decision"]
    strict: Literal[True]
    schema: dict[str, JsonValue]


class ContinuationAdvisorResponseFormat(TypedDict):
    type: Literal["json_schema"]
    json_schema: ContinuationAdvisorJSONSchemaFormat


def continuation_advisor_response_format() -> ContinuationAdvisorResponseFormat:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "composer_continuation_decision",
            "strict": True,
            "schema": _ContinuationAdvisorAnswer.model_json_schema(),
        },
    }
