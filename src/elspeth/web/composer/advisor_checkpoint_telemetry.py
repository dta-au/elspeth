"""Bounded operational telemetry for composer advisor checkpoints."""

from __future__ import annotations

from contextlib import suppress
from typing import Literal

import structlog
from opentelemetry import metrics

from elspeth.contracts.hashing import stable_hash

AdvisorCheckpointPhase = Literal["early", "end"]
AdvisorCheckpointTelemetryVerdict = Literal["clean", "flagged", "unavailable", "malformed"]

_ADVISOR_CHECKPOINT_PASSES_COUNTER = metrics.get_meter(__name__).create_counter(
    "composer.advisor_checkpoint.passes_used",
    description="Completed composer advisor checkpoint passes",
)

slog = structlog.get_logger()


def record_advisor_checkpoint_pass(
    *,
    session_id: str | None,
    phase: AdvisorCheckpointPhase,
    pass_index: int,
    verdict: AdvisorCheckpointTelemetryVerdict,
    findings_text: str,
) -> None:
    """Best-effort event and metric increment for a logical checkpoint call."""
    # The checkpoint verdict is already complete. Optional telemetry must
    # neither replace it nor recursively log raw advisor findings.
    #
    # ``stable_hash`` is ELSPETH-owned and runs ABOVE the suppression on
    # purpose: a canonicalization refusal is a programmer error about our own
    # payload, not an exporter outage, so it must propagate rather than be
    # swallowed with the emit. Same guard ordering the signed
    # ``telemetry_phase8`` exemption relies on.
    findings_hash = stable_hash({"advisor_findings": findings_text})
    with suppress(Exception):
        slog.info(
            "composer.advisor_checkpoint_pass",
            session_id=session_id,
            phase=phase,
            pass_index=pass_index,
            verdict=verdict,
            findings_hash=findings_hash,
        )
    # Keep the metric independent from the event sink and off the correctness
    # path, matching the composer's telemetry policy.
    with suppress(Exception):
        _ADVISOR_CHECKPOINT_PASSES_COUNTER.add(1, {"phase": phase, "verdict": verdict})


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
) -> None:
    """Best-effort attribution event for one advisor-cohort terminal publication.

    elspeth-fa18d54eef: a live turn published the pending-handoff "did not
    clear" notice under a journal trail (advisor pass CLEAN, no withheld
    disclosure row) that the deployed tree could not produce, and which
    branch published the message was unrecoverable after the fact. Every
    publication site now names its branch here, so a recurrence is
    attributable from one journal read. Fields are all backend-derived
    (closed vocabularies plus the session id) — no advisor findings text and
    no model prose enter this event.
    """
    with suppress(Exception):
        slog.info(
            "composer.advisor_terminal_publication",
            session_id=session_id,
            branch=branch,
            reason=reason,
            preflight_shape=preflight_shape,
        )
    # Keep the metric independent from the event sink and off the correctness
    # path, matching the composer's telemetry policy.
    with suppress(Exception):
        _ADVISOR_TERMINAL_PUBLICATIONS_COUNTER.add(1, {"branch": branch, "preflight_shape": preflight_shape})
