"""Durable audit rows for composer advisor checkpoint events.

Two advisor events explain a composer turn's outcome after the fact: which
verdict each completion-advisory checkpoint pass returned, and which terminal
branch published the fixed backend copy the user finally saw. Both were the
evidence elspeth-fa18d54eef was diagnosed from, and both used to exist only
as structlog events — a channel the logging policy reserves for last resort.
Under that policy (audit first, synchronously, must-fire; telemetry second,
best-effort; logs only when neither can) each event is now written as a
``role="audit"`` ``chat_messages`` row through the sessions service BEFORE
its telemetry mirror fires, in the same shape as the per-LLM-call and
planner-attempt sidecars: an ``audit`` row whose ``tool_calls`` column holds
one ``_kind``-discriminated envelope and whose ``content`` is that envelope's
JSON. ``role="audit"`` keeps the rows out of the conversation and out of
prompt history (``_is_composer_audit_tool_message``); the distinct ``_kind``
keeps them out of the LLM-audit opt-in view.

A compose that runs without a session (``session_id is None`` — the eval
harnesses and direct unit invocations) has no session audit store to write
to; those emit telemetry only, exactly as the withheld-turn disclosure row
already behaves. A session WITHOUT the turn's operation context is a defect,
not a sessionless compose: every session write is fenced (P4-D6 family A2b),
so the write refuses rather than running unfenced.

Every field of both records is a closed vocabulary, a small integer, a hash,
or a boolean. No advisor findings text and no model prose enter the rows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, TypedDict, final
from uuid import UUID

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.advisor_checkpoint_telemetry import (
    AdvisorCheckpointPhase,
    AdvisorCheckpointTelemetryVerdict,
    AdvisorCheckpointVerdictSource,
    AdvisorPreflightShape,
    AdvisorTerminalPublicationBranch,
    record_advisor_checkpoint_pass,
    record_advisor_terminal_publication,
)

if TYPE_CHECKING:
    from elspeth.contracts.session_operation import SessionOperationContext
    from elspeth.web.sessions.protocol import SessionServiceProtocol

ADVISOR_CHECKPOINT_PASS_AUDIT_KIND: Final[Literal["advisor_checkpoint_pass_audit"]] = "advisor_checkpoint_pass_audit"
ADVISOR_TERMINAL_PUBLICATION_AUDIT_KIND: Final[Literal["advisor_terminal_publication_audit"]] = "advisor_terminal_publication_audit"

_CHECKPOINT_PHASES: Final[frozenset[str]] = frozenset({"early", "end"})
_CHECKPOINT_VERDICTS: Final[frozenset[str]] = frozenset({"clean", "flagged", "unavailable", "malformed"})
_CHECKPOINT_SOURCES: Final[frozenset[str]] = frozenset({"prescan", "model"})
_PUBLICATION_BRANCHES: Final[frozenset[str]] = frozenset(
    {
        "terminal_block",
        "repair_unverified",
        "repair_success",
        "repair_handoff_signoff_failed",
        "repair_review_with_findings",
        "repair_review",
        "repair_preflight_failure",
        "repair_signoff_pending",
    }
)
_PREFLIGHT_SHAPES: Final[frozenset[str]] = frozenset({"absent", "green", "pending_handoff", "red"})
# The END gate's blocked-terminal reasons (``_advisor_blocked_result``). Only
# the ``terminal_block`` branch carries one; every repair branch passes None.
AdvisorTerminalBlockReason = Literal["unavailable", "malformed", "flagged_final_pass", "flagged_no_repair", "flagged_unrepairable"]
_TERMINAL_BLOCK_REASONS: Final[frozenset[str]] = frozenset(
    {"unavailable", "malformed", "flagged_final_pass", "flagged_no_repair", "flagged_unrepairable"}
)


class AdvisorCheckpointPassData(TypedDict):
    """Serialised :class:`AdvisorCheckpointPassRecord` — the audit row's and the event's payload."""

    phase: AdvisorCheckpointPhase
    pass_index: int
    verdict: AdvisorCheckpointTelemetryVerdict
    source: AdvisorCheckpointVerdictSource
    findings_hash: str


class AdvisorTerminalPublicationData(TypedDict):
    """Serialised :class:`AdvisorTerminalPublication` — the audit row's and the event's payload."""

    branch: AdvisorTerminalPublicationBranch
    reason: AdvisorTerminalBlockReason | None
    preflight_shape: AdvisorPreflightShape
    findings_backend_authored: bool


# Functional syntax: ``pass`` is a keyword, so it cannot be a class-body attribute name.
AdvisorCheckpointPassAuditEnvelope = TypedDict(
    "AdvisorCheckpointPassAuditEnvelope",
    {"_kind": Literal["advisor_checkpoint_pass_audit"], "pass": AdvisorCheckpointPassData},
)


class AdvisorTerminalPublicationAuditEnvelope(TypedDict):
    _kind: Literal["advisor_terminal_publication_audit"]
    publication: AdvisorTerminalPublicationData


AdvisorAuditEnvelope = AdvisorCheckpointPassAuditEnvelope | AdvisorTerminalPublicationAuditEnvelope


@final
@dataclass(frozen=True, slots=True)
class AdvisorCheckpointPassRecord:
    """One completed advisor checkpoint pass, as the audit row and event carry it.

    ``findings_hash`` is the canonical hash of the verdict's findings text —
    the text itself never leaves the service. Build through
    :meth:`from_findings` so the hash payload shape stays the one the journal
    has carried since the event was introduced.
    """

    phase: AdvisorCheckpointPhase
    pass_index: int
    verdict: AdvisorCheckpointTelemetryVerdict
    source: AdvisorCheckpointVerdictSource
    findings_hash: str

    def __post_init__(self) -> None:
        if self.phase not in _CHECKPOINT_PHASES:
            raise AuditIntegrityError("AdvisorCheckpointPassRecord.phase is outside the closed vocabulary")
        if type(self.pass_index) is not int or self.pass_index < 0:
            raise AuditIntegrityError("AdvisorCheckpointPassRecord.pass_index must be a non-negative exact int")
        if self.verdict not in _CHECKPOINT_VERDICTS:
            raise AuditIntegrityError("AdvisorCheckpointPassRecord.verdict is outside the closed vocabulary")
        if self.source not in _CHECKPOINT_SOURCES:
            raise AuditIntegrityError("AdvisorCheckpointPassRecord.source is outside the closed vocabulary")
        if type(self.findings_hash) is not str or not self.findings_hash:
            raise AuditIntegrityError("AdvisorCheckpointPassRecord.findings_hash must be a non-empty exact string")

    @classmethod
    def from_findings(
        cls,
        *,
        phase: AdvisorCheckpointPhase,
        pass_index: int,
        verdict: AdvisorCheckpointTelemetryVerdict,
        source: AdvisorCheckpointVerdictSource,
        findings_text: str,
    ) -> AdvisorCheckpointPassRecord:
        # ``stable_hash`` is ELSPETH-owned: a canonicalization refusal is a
        # programmer error about our own payload and propagates from here,
        # before any row is written or any event is emitted.
        return cls(
            phase=phase,
            pass_index=pass_index,
            verdict=verdict,
            source=source,
            findings_hash=stable_hash({"advisor_findings": findings_text}),
        )

    def to_dict(self) -> AdvisorCheckpointPassData:
        return {
            "phase": self.phase,
            "pass_index": self.pass_index,
            "verdict": self.verdict,
            "source": self.source,
            "findings_hash": self.findings_hash,
        }


@final
@dataclass(frozen=True, slots=True)
class AdvisorTerminalPublication:
    """Which advisor-cohort branch published a turn's fixed backend copy.

    Minted by the publication site itself (``_advisor_blocked_result`` for
    ``terminal_block``; ``_replace_advisor_repair_public_result`` for every
    repair branch) and carried on ``ComposerResult.advisor_terminal_publication``
    so the site that holds the session write context can persist it.

    ``findings_backend_authored`` (elspeth-25f7b757e7 A2) is True iff the
    published wording embeds the backend-authored deterministic pre-scan
    finding. Only the blocked terminal can carry True — it is the one
    publication whose wording rides the verdict; every repair-cohort branch
    publishes fixed copy with no finding at all and passes False.
    """

    branch: AdvisorTerminalPublicationBranch
    reason: AdvisorTerminalBlockReason | None
    preflight_shape: AdvisorPreflightShape
    findings_backend_authored: bool

    def __post_init__(self) -> None:
        if self.branch not in _PUBLICATION_BRANCHES:
            raise AuditIntegrityError("AdvisorTerminalPublication.branch is outside the closed vocabulary")
        if self.reason is not None and self.reason not in _TERMINAL_BLOCK_REASONS:
            raise AuditIntegrityError("AdvisorTerminalPublication.reason is outside the closed vocabulary")
        if (self.branch == "terminal_block") != (self.reason is not None):
            raise AuditIntegrityError("AdvisorTerminalPublication.reason is carried by the terminal_block branch and no other")
        if self.preflight_shape not in _PREFLIGHT_SHAPES:
            raise AuditIntegrityError("AdvisorTerminalPublication.preflight_shape is outside the closed vocabulary")
        if type(self.findings_backend_authored) is not bool:
            raise AuditIntegrityError("AdvisorTerminalPublication.findings_backend_authored must be an exact bool")
        if self.findings_backend_authored and self.branch != "terminal_block":
            raise AuditIntegrityError("AdvisorTerminalPublication: only the terminal_block branch can carry a backend-authored finding")

    def to_dict(self) -> AdvisorTerminalPublicationData:
        return {
            "branch": self.branch,
            "reason": self.reason,
            "preflight_shape": self.preflight_shape,
            "findings_backend_authored": self.findings_backend_authored,
        }


def advisor_checkpoint_pass_audit_envelope(record: AdvisorCheckpointPassRecord) -> AdvisorCheckpointPassAuditEnvelope:
    """Wrap one checkpoint pass in the canonical ``tool_calls`` audit envelope."""
    return {"_kind": ADVISOR_CHECKPOINT_PASS_AUDIT_KIND, "pass": record.to_dict()}


def advisor_terminal_publication_audit_envelope(publication: AdvisorTerminalPublication) -> AdvisorTerminalPublicationAuditEnvelope:
    """Wrap one terminal publication in the canonical ``tool_calls`` audit envelope."""
    return {"_kind": ADVISOR_TERMINAL_PUBLICATION_AUDIT_KIND, "publication": publication.to_dict()}


async def _persist_advisor_audit_row(
    *,
    sessions: SessionServiceProtocol | None,
    session_id: str,
    session_operation_context: SessionOperationContext | None,
    envelope: AdvisorAuditEnvelope,
) -> None:
    kind = envelope["_kind"]
    if session_operation_context is None:
        # Fenced session write (P4-D6 family A2b): the row carries the compose
        # operation this turn runs under — same refusal as the withheld-turn
        # disclosure row.
        raise TypeError(f"{kind} row requires the turn's session_operation_context")
    if sessions is None:
        raise RuntimeError("sessions_service not wired")
    await sessions.add_message(
        UUID(session_id),
        "audit",
        json.dumps(envelope, sort_keys=True),
        tool_calls=[envelope],
        writer_principal="compose_loop",
        session_operation_context=session_operation_context,
    )


async def persist_advisor_checkpoint_pass(
    *,
    sessions: SessionServiceProtocol | None,
    session_id: str | None,
    session_operation_context: SessionOperationContext | None,
    record: AdvisorCheckpointPassRecord,
) -> None:
    """Audit row first, then the ``composer.advisor_checkpoint_pass`` event.

    An audit write failure propagates and the event never fires: a journal
    line claiming a pass the legal record does not hold is the inversion the
    logging policy forbids.
    """
    if session_id is not None:
        await _persist_advisor_audit_row(
            sessions=sessions,
            session_id=session_id,
            session_operation_context=session_operation_context,
            envelope=advisor_checkpoint_pass_audit_envelope(record),
        )
    record_advisor_checkpoint_pass(
        session_id=session_id,
        phase=record.phase,
        pass_index=record.pass_index,
        verdict=record.verdict,
        source=record.source,
        findings_hash=record.findings_hash,
    )


async def persist_advisor_terminal_publication(
    *,
    sessions: SessionServiceProtocol | None,
    session_id: str | None,
    session_operation_context: SessionOperationContext | None,
    publication: AdvisorTerminalPublication,
) -> None:
    """Audit row first, then the ``composer.advisor_terminal_publication`` event.

    Same ordering contract as :func:`persist_advisor_checkpoint_pass`.
    """
    if session_id is not None:
        await _persist_advisor_audit_row(
            sessions=sessions,
            session_id=session_id,
            session_operation_context=session_operation_context,
            envelope=advisor_terminal_publication_audit_envelope(publication),
        )
    record_advisor_terminal_publication(
        session_id=session_id,
        branch=publication.branch,
        reason=publication.reason,
        preflight_shape=publication.preflight_shape,
        findings_backend_authored=publication.findings_backend_authored,
    )
