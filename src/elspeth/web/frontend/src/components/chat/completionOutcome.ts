// src/components/chat/completionOutcome.ts
//
// Shared derivation for Composer completion labels (elspeth-bf9c296ee5).
//
// Terminal copy must distinguish four states — Response ready, Pipeline
// updated, Review required, executable Pipeline ready — and every surface
// that renders a completion claim (the freeform terminal badge in
// ComposingIndicator, the guided CompletionSummary heading, ChatPanel's
// workflow-stepper terminal step) must derive it from the SAME signals that
// actually gate execution, or the surfaces drift into claiming "done" while
// Run is refused.
//
// The signals deliberately mirror ExecuteButton's gate inputs:
//   - reviewPending  ≙ ExecuteButton.isRunBlocked:
//       `!optedOut && Object.keys(pendingBySession[sid] ?? {}).length > 0`
//     (unfiltered pending rows — every pending row blocks Run, so every
//     pending row must also block a "ready" claim);
//   - executionReady ≙ `validationResult?.readiness?.execution_ready === true`
//     (backend execution admission, the same term `canExecute` requires).
// A label that claims ready while ExecuteButton refuses to run is the defect
// this module exists to prevent; if the gate's predicate changes, this hook
// must change with it.
//
// The ladder can only UNDER-claim, never over-claim:
//   1. An un-mutated turn is "response_ready" no matter the ambient state —
//      the badge describes the turn, and an answer-only turn asserts nothing
//      about the pipeline.
//   2. A mutated turn with pending review is "review_required" even when the
//      backend has admitted execution (the run gate refuses in that state).
//   3. "pipeline_ready" requires backend admission. R4-H3 clears the
//      validation verdict on every composition-version change, so admission
//      can never be stale-true for the state the label describes.
//   4. Anything else mutated is "pipeline_updated".

import { useExecutionStore } from "@/stores/executionStore";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";

export type CompletionOutcome =
  | "response_ready"
  | "pipeline_updated"
  | "review_required"
  | "pipeline_ready";

export function deriveCompletionOutcome(input: {
  /** The completed turn changed the composition-state version (freeform), or
   *  the surface is a guided completion (which always carries a composed
   *  pipeline — pass `true`). */
  pipelineMutated: boolean;
  /** Pending interpretation rows block the run gate (opt-out complement
   *  already applied). */
  reviewPending: boolean;
  /** Backend execution admission for the CURRENT composition version. */
  executionReady: boolean;
}): CompletionOutcome {
  if (!input.pipelineMutated) return "response_ready";
  if (input.reviewPending) return "review_required";
  if (input.executionReady) return "pipeline_ready";
  return "pipeline_updated";
}

/** The four-state terminal-copy taxonomy, shared verbatim by the freeform
 *  badge and the guided completion heading so the surfaces cannot drift. */
export const COMPLETION_OUTCOME_LABELS: Record<CompletionOutcome, string> = {
  response_ready: "Response ready",
  pipeline_updated: "Pipeline updated",
  review_required: "Review required",
  pipeline_ready: "Pipeline ready",
};

/**
 * Live outcome for `sessionId`, derived from the run gate's own stores.
 *
 * `pipelineMutated` is the caller's axis: guided completion surfaces pass
 * `true` (a completed terminal always carries a composed pipeline); the
 * freeform badge passes the per-turn verdict recorded in
 * `sessionStore.lastComposeChangedPipeline`.
 */
export function useCompletionOutcome(
  sessionId: string | null,
  pipelineMutated: boolean,
): CompletionOutcome {
  const pendingBySession = useInterpretationEventsStore(
    (s) => s.pendingBySession,
  );
  const optedOutBySession = useInterpretationEventsStore(
    (s) => s.optedOutBySession,
  );
  const validationResult = useExecutionStore((s) => s.validationResult);

  const optedOut =
    sessionId !== null ? optedOutBySession[sessionId] ?? false : false;
  const pendingCount =
    sessionId !== null
      ? Object.keys(pendingBySession[sessionId] ?? {}).length
      : 0;

  return deriveCompletionOutcome({
    pipelineMutated,
    reviewPending: !optedOut && pendingCount > 0,
    executionReady: validationResult?.readiness?.execution_ready === true,
  });
}
