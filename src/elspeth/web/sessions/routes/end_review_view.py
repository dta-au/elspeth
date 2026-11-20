"""END advisor-checkpoint pass rows in the LLM audit view of a session's messages.

GET /api/sessions/{id}/messages?include_llm_audit=true returns each stored
``advisor_checkpoint_pass_audit`` row whose phase is ``end`` as a
``role="audit"`` row carrying one envelope::

    {"_kind": "advisor_checkpoint_pass_audit",
     "pass": {"phase", "pass_index", "verdict", "source",
              "provider_attempts", "publication_verdict"}}

The stored row carries more fields than these six; only these six are read.
A stored row that does not parse to that shape is left out of the response,
and the count of rows left out is returned in the
``X-Elspeth-End-Review-Rows-Omitted`` response header, which this view always
sets. EARLY-phase pass rows are not part of the view.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final, final

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.advisor_audit import ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
from elspeth.web.sessions.protocol import ChatMessageRecord
from elspeth.web.sessions.schemas import AdvisorEndReviewPassResponse

END_REVIEW_ROWS_OMITTED_HEADER: Final = "X-Elspeth-End-Review-Rows-Omitted"
END_REVIEW_FIELDS: Final[tuple[str, ...]] = (
    "phase",
    "pass_index",
    "verdict",
    "source",
    "provider_attempts",
    "publication_verdict",
)


@final
@dataclass(frozen=True, slots=True)
class LlmAuditView:
    """The rows the LLM audit view returns, and how many END rows it left out."""

    messages: tuple[ChatMessageRecord, ...]
    end_review_rows_omitted: int


def _is_advisor_checkpoint_pass_row(message: ChatMessageRecord) -> bool:
    if message.role != "audit" or message.tool_calls is None:
        return False
    return any("_kind" in envelope and envelope["_kind"] == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND for envelope in message.tool_calls)


# The closed value set of each enum field. AdvisorEndReviewPassResponse declares
# the same sets; a stored value outside them leaves the row out of the view.
END_REVIEW_VERDICTS: Final[frozenset[str]] = frozenset({"clean", "flagged", "unavailable", "request_rejected", "malformed"})
END_REVIEW_SOURCES: Final[frozenset[str]] = frozenset({"prescan", "model"})
END_REVIEW_PUBLICATION_VERDICTS: Final[frozenset[str]] = frozenset({"ADMIT", "WITHHOLD"})
END_REVIEW_PROVIDER_ATTEMPTS: Final[frozenset[int]] = frozenset({0, 1, 2})


def _stored_pass(message: ChatMessageRecord) -> tuple[object, ...] | None:
    """The six view fields of a single-envelope row, in ``END_REVIEW_FIELDS`` order, else None."""
    if message.tool_calls is None or len(message.tool_calls) != 1:
        return None
    envelope = deep_thaw(message.tool_calls[0])
    if type(envelope) is not dict or "pass" not in envelope:
        return None
    stored = envelope["pass"]
    if type(stored) is not dict or any(name not in stored for name in END_REVIEW_FIELDS):
        return None
    return tuple(stored[name] for name in END_REVIEW_FIELDS)


def _is_early_pass(message: ChatMessageRecord) -> bool:
    stored = _stored_pass(message)
    return stored is not None and stored[0] == "early"


def parse_end_review_pass(message: ChatMessageRecord) -> AdvisorEndReviewPassResponse | None:
    """Read the six view fields of one stored END pass row, or None when it does not parse.

    A row does not parse when it has more than one envelope, lacks a field,
    is not an END row, or holds a value outside the closed sets above.
    """
    stored = _stored_pass(message)
    if stored is None:
        return None
    phase, pass_index, verdict, source, attempts, publication = stored
    if (
        phase != "end"
        or type(pass_index) is not int
        or pass_index < 0
        or type(verdict) is not str
        or verdict not in END_REVIEW_VERDICTS
        or type(source) is not str
        or source not in END_REVIEW_SOURCES
        or type(attempts) is not int
        or attempts not in END_REVIEW_PROVIDER_ATTEMPTS
        or (publication is not None and (type(publication) is not str or publication not in END_REVIEW_PUBLICATION_VERDICTS))
    ):
        return None
    return AdvisorEndReviewPassResponse.model_validate(dict(zip(END_REVIEW_FIELDS, stored, strict=True)))


def _projected_row(message: ChatMessageRecord, review: AdvisorEndReviewPassResponse) -> ChatMessageRecord:
    envelope = {"_kind": ADVISOR_CHECKPOINT_PASS_AUDIT_KIND, "pass": review.model_dump(mode="json")}
    return replace(message, content=json.dumps(envelope, sort_keys=True), raw_content=None, tool_calls=(envelope,))


def with_end_review_rows(messages: Sequence[ChatMessageRecord], selected: Sequence[ChatMessageRecord]) -> LlmAuditView:
    """Merge END pass rows, cut down to the view fields, into ``selected``.

    ``messages`` is the session's transcript in order and ``selected`` the
    subset the LLM audit view already returns. Pass rows are handled before
    the ``selected`` membership test, so no stored pass envelope reaches the
    response except through :func:`parse_end_review_pass`.
    """
    selected_ids = {message.id for message in selected}
    rows: list[ChatMessageRecord] = []
    omitted = 0
    for message in messages:
        if _is_advisor_checkpoint_pass_row(message):
            if _is_early_pass(message):
                continue
            review = parse_end_review_pass(message)
            if review is None:
                omitted += 1
            else:
                rows.append(_projected_row(message, review))
        elif message.id in selected_ids:
            rows.append(message)
    return LlmAuditView(messages=tuple(rows), end_review_rows_omitted=omitted)
