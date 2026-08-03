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
    with suppress(Exception):
        slog.info(
            "composer.advisor_checkpoint_pass",
            session_id=session_id,
            phase=phase,
            pass_index=pass_index,
            verdict=verdict,
            findings_hash=stable_hash({"advisor_findings": findings_text}),
        )
    # Keep the metric independent from the event sink and off the correctness
    # path, matching the composer's telemetry policy.
    with suppress(Exception):
        _ADVISOR_CHECKPOINT_PASSES_COUNTER.add(1, {"phase": phase, "verdict": verdict})
