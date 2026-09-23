"""Strict admission of advisor checkpoints and bounded user-facing notes."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final, Literal, TypedDict, get_args

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from elspeth.plugins.infrastructure.clients.json_utils import parse_json_strict
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


@dataclass(frozen=True, slots=True)
class AdvisorResponseAdmission:
    """Schema conformance and the semantically accepted response, if any."""

    schema_valid: bool
    response: AdvisorCheckpointResponse | None


def parse_advisor_checkpoint_response(text: str) -> AdvisorResponseAdmission:
    """Admit Tier-3 JSON without retaining provider-controlled error details."""
    parsed, error = parse_json_strict(text)
    if error is not None:
        return AdvisorResponseAdmission(schema_valid=False, response=None)
    try:
        response = AdvisorCheckpointResponse.model_validate(parsed)
    except ValidationError:
        return AdvisorResponseAdmission(schema_valid=False, response=None)

    # The old prose rule let any FLAGGED marker dominate ambiguous CLEAN text.
    # A closed enum, duplicate-key rejection and these consistency checks
    # preserve that fail-closed direction without retaining a prose fallback.
    if response.verdict == "CLEAN" and (response.note is not None or response.steps):
        return AdvisorResponseAdmission(schema_valid=True, response=None)
    if response.verdict == "FLAGGED" and not response.findings.strip():
        return AdvisorResponseAdmission(schema_valid=True, response=None)
    return AdvisorResponseAdmission(schema_valid=True, response=response)


class AdvisorJSONSchemaFormat(TypedDict):
    """Owned provider schema specification."""

    name: Literal["advisor_checkpoint_response"]
    strict: Literal[True]
    schema: dict[str, object]


class AdvisorResponseFormat(TypedDict):
    """Owned LiteLLM structured-response envelope."""

    type: Literal["json_schema"]
    json_schema: AdvisorJSONSchemaFormat


def advisor_response_format() -> AdvisorResponseFormat:
    """Build the same strict provider schema for checkpoints and boot probes."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "advisor_checkpoint_response",
            "strict": True,
            "schema": AdvisorCheckpointResponse.model_json_schema(),
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
