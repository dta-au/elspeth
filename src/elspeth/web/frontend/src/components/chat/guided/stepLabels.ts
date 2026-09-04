// src/components/chat/guided/stepLabels.ts
//
// Shared GuidedStep → display-label vocabulary for every guided surface:
// the transcript (GuidedChatHistory, both variants), the "Decisions so far"
// summary (GuidedHistory), and ChatPanel's workflow stepper. Deliberately a
// LEAF module — it imports ONLY types/guided — so every consumer (including
// sibling widgets in this directory) can import it with no cycle possible.
// It replaces three hand-mirrored STEP_LABELS copies that had drifted.
//
// CLOSED LIST — must cover every GuidedStep member. The keys are wire names
// (protocol.py GuidedStep) and never change here; the labels are display-only
// copy. step_2_sink reads "Output" because that is the operator-facing
// vocabulary everywhere else (the stepper, the sink schema step).
//
// This is the WIZARD-step axis. interpretationStepLabel.ts is the
// plugin/node axis (per-node labels for ack cards / wire edges / validation
// phrases) — a different vocabulary; do not merge the two.

import type { GuidedStep } from "@/types/guided";

export const GUIDED_STEP_LABELS: Record<GuidedStep, string> = {
  step_1_source: "Source",
  step_2_sink: "Output",
  step_3_transforms: "Transforms",
  step_4_wire: "Wire",
};

/**
 * Transcript divider opened at the FIRST post-confirmation chat turn
 * (elspeth-986801d218). Deliberately OUTSIDE the closed map above: it is not
 * a GuidedStep and must never be reachable by a `GUIDED_STEP_LABELS[step]`
 * lookup — post-commit chat turns are persisted with `step="step_4_wire"`,
 * the same wire step as the pre-commit wire turns, so the map's
 * step-change divider cannot mark this boundary and a widened map would only
 * break its exhaustiveness contract.
 *
 * The boundary is a CLIENT-side reading of the transcript (the first user
 * turn carrying the confirmation hash as its `turn_token`, see
 * completedChatToken.ts) — no wire field, no enum member, no server change.
 */
export const GUIDED_AFTER_CONFIRMATION_LABEL = "After confirmation";
