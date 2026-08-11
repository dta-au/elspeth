/**
 * CompletionBar — three-button completion gesture surface (Phase 6B Task 3).
 *
 * Renders three co-equal buttons:
 *
 *   * Save for review  → POSTs mark-ready-for-review, opens dialog with the
 *                        signed share URL.
 *   * Run pipeline     → reuses the existing ExecuteButton primitive (which
 *                        carries the Phase 5b interpretation-gating logic).
 *   * Export YAML      → selects the persistent YAML artifact through the
 *                        existing ExportYamlButton primitive.
 *
 * Per plan 19b §"Scope boundaries": no primary emphasis — all three are
 * co-equal verbs. The "Save for review" button follows the backend-owned
 * completion-readiness axis. This is deliberately stricter than Run: an
 * advisor checkpoint can allow execution while still blocking completion.
 */

import { useShareableReviewStore } from "@/stores/shareableReviewStore";
import { useSessionStore } from "@/stores/sessionStore";
import { useExecutionStore } from "@/stores/executionStore";
import { ExecuteButton } from "@/components/sidebar/ExecuteButton";
import { ExportYamlButton } from "@/components/sidebar/ExportYamlButton";

const SAVE_FOR_REVIEW_DISABLED_TITLE =
  "Fix validation or completion blockers before sharing for review.";

export function CompletionBar(): JSX.Element | null {
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const validationResult = useExecutionStore((s) => s.validationResult);
  const openAndMark = useShareableReviewStore((s) => s.openAndMark);
  const inFlight = useShareableReviewStore((s) => s.inFlight);

  if (!activeSessionId) return null;

  // Completion readiness mirrors the backend's mark-time gate. A null result
  // is not ready because the user has not run validation yet.
  const isCompletionReady =
    validationResult?.readiness?.completion_ready === true;
  const saveDisabled = !isCompletionReady || inFlight;
  const completionBlockedTitle =
    validationResult?.readiness?.blockers[0]?.detail ??
    SAVE_FOR_REVIEW_DISABLED_TITLE;

  return (
    <div
      className="completion-bar"
      role="group"
      aria-label="Composition completion gestures"
      data-testid="completion-bar"
    >
      <button
        type="button"
        className="btn completion-bar-save-for-review"
        onClick={() => {
          if (!isCompletionReady || inFlight) return;
          // openAndMark resolves asynchronously and persists outcome in the
          // store; no need to await here at the click site.
          void openAndMark(activeSessionId);
        }}
        disabled={saveDisabled}
        aria-disabled={saveDisabled || undefined}
        title={
          saveDisabled && !isCompletionReady
            ? completionBlockedTitle
            : undefined
        }
        data-testid="completion-bar-save-for-review"
      >
        Save for review
      </button>
      <div data-testid="completion-bar-run-pipeline">
        <ExecuteButton />
      </div>
      <div data-testid="completion-bar-export-yaml">
        <ExportYamlButton />
      </div>
    </div>
  );
}
