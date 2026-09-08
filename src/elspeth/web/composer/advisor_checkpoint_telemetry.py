"""Bounded operational telemetry for composer advisor checkpoints.

Every event here is the real-time MIRROR of an audit row that
``web/composer/advisor_audit.py`` has already committed through the sessions
service (or that a sessionless compose has no store to hold). Under the
logging policy the audit row is the record; these emits are telemetry, so
an exporter failure must neither displace the composer outcome the row
already fixes nor pass silently: it is acknowledged on the last-resort
channel (``composer.advisor_telemetry_failed``) the way the tutorial and
preferences telemetry acknowledge theirs, and registered Tier-1 integrity
failures still escape.
"""

from __future__ import annotations

from typing import Literal

import structlog
from opentelemetry import metrics

from elspeth.contracts import errors as contract_errors

AdvisorCheckpointPhase = Literal["early", "end"]
AdvisorCheckpointTelemetryVerdict = Literal["clean", "flagged", "unavailable", "malformed"]
# elspeth-25f7b757e7 (A2): which mechanism rendered the verdict. "prescan" is
# the deterministic backend pre-scan (no provider call was made); "model"
# covers everything downstream of a provider call attempt — parsed CLEAN or
# FLAGGED, and the unavailable/malformed failure classes. Without this
# dimension a pre-scan force-FLAG and an LLM-advisor FLAG were byte-identical
# in the journal (``verdict="flagged"``), so the pre-scan's false-positive
# rate — the evidence needed to defend the fail-closed posture — was
# unmeasurable.
AdvisorCheckpointVerdictSource = Literal["prescan", "model"]

_ADVISOR_CHECKPOINT_PASSES_COUNTER = metrics.get_meter(__name__).create_counter(
    "composer.advisor_checkpoint.passes_used",
    description="Completed composer advisor checkpoint passes",
)

slog = structlog.get_logger()


def _acknowledge_telemetry_failure(*, operation: str, error_type: str) -> None:
    """Acknowledge an exporter failure through the last available channel.

    Only the exception CLASS name is projected, never its text.
    """
    try:
        slog.error("composer.advisor_telemetry_failed", operation=operation, error_type=error_type)
    except contract_errors.TIER_1_ERRORS:
        raise
    except Exception:
        # Ordinary failure of the last-resort logger cannot replace an audit
        # row that has already committed.
        return


def record_advisor_checkpoint_pass(
    *,
    session_id: str | None,
    phase: AdvisorCheckpointPhase,
    pass_index: int,
    verdict: AdvisorCheckpointTelemetryVerdict,
    source: AdvisorCheckpointVerdictSource,
    findings_hash: str,
) -> None:
    """Event and metric increment mirroring one persisted checkpoint pass.

    Called by ``advisor_audit.persist_advisor_checkpoint_pass`` AFTER the
    ``advisor_checkpoint_pass_audit`` row is durable. ``findings_hash`` is
    the record's already-computed canonical hash: no findings text enters
    this event. The event and the metric are guarded separately so a broken
    meter provider cannot cost the event and a broken event sink cannot cost
    the count.
    """
    try:
        slog.info(
            "composer.advisor_checkpoint_pass",
            session_id=session_id,
            phase=phase,
            pass_index=pass_index,
            verdict=verdict,
            source=source,
            findings_hash=findings_hash,
        )
    except contract_errors.TIER_1_ERRORS:
        raise
    except Exception as exc:
        _acknowledge_telemetry_failure(operation="checkpoint_pass_event", error_type=type(exc).__name__)
    try:
        _ADVISOR_CHECKPOINT_PASSES_COUNTER.add(1, {"phase": phase, "verdict": verdict, "source": source})
    except contract_errors.TIER_1_ERRORS:
        raise
    except Exception as exc:
        _acknowledge_telemetry_failure(operation="checkpoint_pass_counter", error_type=type(exc).__name__)


AdvisorTerminalPublicationBranch = Literal[
    "terminal_block",
    "repair_unverified",
    "repair_success",
    "repair_handoff_signoff_failed",
    "repair_review_with_findings",
    "repair_review",
    "repair_preflight_failure",
    "repair_signoff_pending",
]
AdvisorPreflightShape = Literal["absent", "green", "pending_handoff", "red"]

_ADVISOR_TERMINAL_PUBLICATIONS_COUNTER = metrics.get_meter(__name__).create_counter(
    "composer.advisor_terminal_publication.branches",
    description="Advisor-cohort terminal publications by branch",
)


def record_advisor_terminal_publication(
    *,
    session_id: str | None,
    branch: AdvisorTerminalPublicationBranch,
    reason: str | None,
    preflight_shape: AdvisorPreflightShape,
    findings_backend_authored: bool,
) -> None:
    """Event and metric increment mirroring one persisted terminal publication.

    elspeth-fa18d54eef: a live turn published the pending-handoff "did not
    clear" notice under a journal trail (advisor pass CLEAN, no withheld
    disclosure row) that the deployed tree could not produce, and which
    branch published the message was unrecoverable after the fact. Every
    publication site now mints an ``AdvisorTerminalPublication`` that
    ``advisor_audit.persist_advisor_terminal_publication`` writes as an
    ``advisor_terminal_publication_audit`` row before calling here, so a
    recurrence is attributable from the session's own audit trail and this
    event is only its real-time view. Fields are all backend-derived (closed
    vocabularies plus the session id) — no advisor findings text and no model
    prose enter this event.
    """
    try:
        slog.info(
            "composer.advisor_terminal_publication",
            session_id=session_id,
            branch=branch,
            reason=reason,
            preflight_shape=preflight_shape,
            findings_backend_authored=findings_backend_authored,
        )
    except contract_errors.TIER_1_ERRORS:
        raise
    except Exception as exc:
        _acknowledge_telemetry_failure(operation="terminal_publication_event", error_type=type(exc).__name__)
    try:
        _ADVISOR_TERMINAL_PUBLICATIONS_COUNTER.add(
            1,
            {"branch": branch, "preflight_shape": preflight_shape, "findings_backend_authored": findings_backend_authored},
        )
    except contract_errors.TIER_1_ERRORS:
        raise
    except Exception as exc:
        _acknowledge_telemetry_failure(operation="terminal_publication_counter", error_type=type(exc).__name__)
