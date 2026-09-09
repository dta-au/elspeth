"""Bounded operational telemetry for guided shape-rejection self-repair."""

from __future__ import annotations

from contextlib import suppress
from typing import Literal

import structlog
from opentelemetry import metrics

GuidedShapeRepairStep = Literal["step_1_source", "step_2_sink"]
GuidedShapeRepairTool = Literal["resolve_source", "resolve_sink"]
GuidedShapeRepairOutcome = Literal["repaired", "exhausted"]

_GUIDED_SHAPE_REPAIR_COUNTER = metrics.get_meter(__name__).create_counter(
    "composer.guided_shape_repair.outcomes",
    description="Guided terminal-tool shape-rejection self-repair outcomes",
)

slog = structlog.get_logger()


def record_guided_shape_repair(
    *,
    step: GuidedShapeRepairStep,
    tool: GuidedShapeRepairTool,
    outcome: GuidedShapeRepairOutcome,
    attempt_index: int,
) -> None:
    """Event and best-effort metric for one shape-repair resolution.

    elspeth-79e66ff613 stage 2: ``repaired`` means a shape-rejected terminal
    tool call was corrected by the model within the same Send; ``exhausted``
    means no in-Send repair could complete — the attempt cap was spent, or
    the provider turn was inadmissible for re-threading — so the pre-repair
    user-facing error surfaced (those coincide 1:1 with the wrapper's
    ``guided.step_1_source_chat_model_shape_rejected`` events, which carry
    the session id for correlation — this emitter deliberately does not
    widen the solver signature to thread one). Fields are closed
    vocabularies plus a small integer; no model text enters this event.
    Currently wired into the step-1 arm only; the step-2 backfill is a
    tracked follow-up, not a silent widening.
    """
    # The event emission is the attribution record for this resolution, so a
    # failing event sink propagates instead of being silently swallowed: the
    # telemetry policy forbids silent failure at emission points, and only the
    # aggregate counter below is best-effort.
    slog.info(
        "composer.guided_shape_repair",
        step=step,
        tool=tool,
        outcome=outcome,
        attempt_index=attempt_index,
    )
    # Keep the metric independent from the event sink and off the correctness
    # path, matching the composer's telemetry policy.
    with suppress(Exception):
        _GUIDED_SHAPE_REPAIR_COUNTER.add(1, {"step": step, "outcome": outcome})
