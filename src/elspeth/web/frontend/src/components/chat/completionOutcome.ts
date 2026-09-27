
import { useExecutionStore } from "@/stores/executionStore";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";

export type CompletionOutcome =
  | "response_ready"
  | "pipeline_updated"
  | "review_required"
  | "pipeline_ready";

export function deriveCompletionOutcome(input: {
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

export const COMPLETION_OUTCOME_LABELS: Record<CompletionOutcome, string> = {
  response_ready: "Response ready",
  pipeline_updated: "Pipeline updated",
  review_required: "Review required",
  pipeline_ready: "Pipeline ready",
};

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
