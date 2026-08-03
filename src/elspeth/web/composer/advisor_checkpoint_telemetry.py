"""Bounded operational telemetry for composer advisor checkpoints."""

from __future__ import annotations

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
    """Emit one event and one metric increment for a logical checkpoint call."""
    slog.info(
        "composer.advisor_checkpoint_pass",
        session_id=session_id,
        phase=phase,
        pass_index=pass_index,
        verdict=verdict,
        findings_hash=stable_hash({"advisor_findings": findings_text}),
    )
    _ADVISOR_CHECKPOINT_PASSES_COUNTER.add(1, {"phase": phase, "verdict": verdict})
