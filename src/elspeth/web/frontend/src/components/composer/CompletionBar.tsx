/**
 * CompletionBar — three-button completion gesture surface (Phase 6B Task 3).
 *
 * Renders three buttons, in DOM (= visual = tab) order:
 *
 *   * Save for review  → POSTs mark-ready-for-review, opens dialog with the
 *                        signed share URL.
 *   * Import YAML      → opens the import modal through the existing
 *                        ImportYamlButton primitive. (Export lives on the
 *                        YAML artifact tab's Copy/Download controls and the
 *                        command palette; a toolbar Export button duplicated
 *                        them, so the slot carries Import instead.)
 *   * Run pipeline     → reuses the existing ExecuteButton primitive (which
 *                        carries the Phase 5b interpretation-gating logic);
 *                        isolated at the bar's right edge, see below.
 *
 * Per plan 19b §"Scope boundaries" the three verbs were fully co-equal (no
 * emphasis). Two deliberate 2026-08-15 operator decisions superseded that:
 * Run pipeline carries the danger-family red fill (it is the one verb with
 * consequences outside the composer; see ExecuteButton.tsx), and Run is
 * ISOLATED at the bar's right edge, right-aligned with the toolbar's Focus
 * graph button, while Save/Import stay in the left cluster. DOM order is
 * Save → Import → Run because visual order IS tab order (WCAG 2.4.3; no CSS
 * `order` on interactive controls — same rule as ArtifactWorkspace's right
 * cluster); workspace.css pushes the LAST child right with an auto margin.
 * The "Save for review" button follows the backend-owned
 * completion-readiness axis. This is deliberately stricter than Run: an
 * advisor checkpoint can allow execution while still blocking completion.
 *
 * STRUCTURAL SYMMETRY (elspeth-929fc5d4a7): the three verbs render as DIRECT
 * children of .completion-bar — no wrapper <div>s. The earlier shape (bare
 * save button beside two wrapped primitives) let flexbox treat the three
 * asymmetrically: the bare button stretched to the tallest wrapper's height
 * (a 98px slab when ExecuteButton's block-reason line rode inside the Run
 * wrapper), and the verbs shared no baseline. With no wrappers, the
 * per-member sizing rules (`.completion-bar > *`, workspace.css) reach the
 * buttons themselves, and workspace.css moves ExecuteButton's reason line
 * onto its own full-width flex line so helper text can never drive a
 * sibling's height again.
 */

import { Button } from "@/components/ui";
import { useShareableReviewStore } from "@/stores/shareableReviewStore";
import { useSessionStore } from "@/stores/sessionStore";
import { useExecutionStore } from "@/stores/executionStore";
import { ExecuteButton } from "@/components/sidebar/ExecuteButton";
import { ImportYamlButton } from "@/components/sidebar/ImportYamlButton";

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
      <Button
        type="button"
        className="completion-bar-save-for-review"
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
      </Button>
      <ImportYamlButton />
      <ExecuteButton />
    </div>
  );
}
