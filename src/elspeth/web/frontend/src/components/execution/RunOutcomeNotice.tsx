// ============================================================================
// RunOutcomeNotice
//
// Always-mounted (App-level) toast for a run reaching a terminal state while
// the workspace is NOT on the Run artifact (elspeth-3a7b7c7b37). Every other
// completion surface (ProgressView's live region, DiscardSummaryWarning,
// RunOutputsPanel) lives inside the Run panel, whose body unmounts whenever
// another artifact tab is active — without this notice, completion is silent.
//
// Announcement contract (M07 / WCAG 4.1.3): the component itself is ALWAYS
// rendered — ONE persistent, visually-hidden role="status" node whose TEXT
// toggles with the outcome, carrying all five terminal statuses. A polite
// live region inserted into the DOM with its content already present is the
// WAI-ARIA / MDN-documented unreliable pattern (see AcknowledgementLiveRegion
// in AcknowledgementStack.tsx — the codebase precedent this follows), so the
// announcement text must be a content mutation inside a pre-existing region.
// Only the VISUAL banner is conditional. ProgressView owns the single
// terminal-status live region while the Run tab is visible; ArtifactWorkspace
// acknowledges the outcome in a layout effect there, which empties this
// region before paint — so a screen reader hears each terminal status exactly
// once, from exactly one region.
//
// WHY POLITE FOR EVERY OUTCOME, AND WHY THERE IS NO role="alert" NODE HERE.
// An earlier revision of this component kept a SECOND permanently-mounted
// region with role="alert", carrying the failed/cancelled statuses. A design
// review commended that split; it is nevertheless the wrong shape, and this
// comment exists so it is not restored from that record. Two reasons:
//
//  1. This codebase has two distinct live-region conventions, and the pair
//     was mixing them. POLITE regions are persistent-and-mutate
//     (AcknowledgementLiveRegion, for the reason above). ASSERTIVE
//     role="alert" regions are CONDITIONALLY INSERTED — every one in the tree
//     does this (AppNoticeCenter, ErrorBoundary, DefaultModeChangedBanner),
//     because role="alert" carries implicit aria-live="assertive" plus
//     aria-atomic="true" and IS the canonical announce-on-insertion role. A
//     permanently-empty alert node therefore buys nothing it does not already
//     get from being inserted, while making a singular
//     getByRole/findByRole("alert") ambiguous for every other app-level
//     surface — which it demonstrably did.
//
//  2. Assertive is reserved for messages needing immediate attention, i.e.
//     ones that interrupt what the user is doing. A background pipeline run
//     reaching a terminal state does not block the user even when it fails:
//     the visible banner persists until dismissed, the Run-tab badge persists
//     alongside it, and the diagnostics are a click away. Interrupting a
//     screen-reader user mid-sentence to say so is disproportionate.
//
// The visible banner still carries the outcome TONE (error/warning/success/
// info), so urgency is expressed where it is free rather than in the live
// region's politeness setting.
//
// Pure executionStore reader: no fetching, no polling, no run emitters
// (REQUEST_RUN_EVENT stays single-owner per App.tsx doctrine).
// ============================================================================

import { useRef } from "react";

import { useExecutionStore } from "@/stores/executionStore";
import { dispatchArtifactViewIntent } from "@/lib/composer-events";
import { terminalRunPhrase } from "./runTerminalPhrases";
import { isTerminalRunStatus, type RunStatus } from "@/types/index";

/** Toast copy per terminal status; null for non-terminal statuses, which the
 *  store never records as an outcome — treated as "render nothing" rather
 *  than trusting the invariant. The wording is NOT owned here: it is the
 *  shared vocabulary ProgressView's live region also speaks, so the two
 *  announcement surfaces for one event cannot drift. */
function outcomeMessage(status: RunStatus): string | null {
  return isTerminalRunStatus(status) ? terminalRunPhrase(status) : null;
}

/** Reuses the shared .alert-banner tone classes (header.css); the base class
 *  without a modifier is the error tone, used for `failed`. */
function toneClassName(status: RunStatus): string {
  switch (status) {
    case "completed":
      return "alert-banner alert-banner--success";
    case "completed_with_failures":
    case "cancelled":
      return "alert-banner alert-banner--warning";
    case "empty":
      return "alert-banner alert-banner--info";
    default:
      return "alert-banner";
  }
}

export function RunOutcomeNotice(): JSX.Element {
  const lastRunOutcome = useExecutionStore((s) => s.lastRunOutcome);
  const acknowledgeRunOutcome = useExecutionStore(
    (s) => s.acknowledgeRunOutcome,
  );
  const bannerRef = useRef<HTMLDivElement | null>(null);

  const message =
    lastRunOutcome === null ? null : outcomeMessage(lastRunOutcome.status);

  // Acknowledging unmounts the visual banner. If focus is inside it at that
  // moment (keyboard Dismiss / View run), the browser would reset focus
  // to <body>, stranding keyboard users at the top of the document — the
  // defect class UserMenu documents (elspeth-bcd1a9b9b3). Hand focus to the
  // app's main content region (the skip-link target, tabIndex=-1) BEFORE the
  // banner detaches. The View run path normally re-homes focus itself
  // (its artifact intent lands in ArtifactWorkspace's selectAndFocus), in
  // which case focus has already left the banner and this is a no-op.
  const releaseFocusBeforeUnmount = (): void => {
    const banner = bannerRef.current;
    if (
      banner === null ||
      !(document.activeElement instanceof Node) ||
      !banner.contains(document.activeElement)
    ) {
      return;
    }
    const mainContent = document.getElementById("composer-main");
    if (mainContent instanceof HTMLElement) {
      mainContent.focus({ preventScroll: true });
    }
  };

  const handleViewRun = (): void => {
    if (lastRunOutcome === null) return;
    dispatchArtifactViewIntent({
      tab: "run",
      focusMode: false,
      sessionId: lastRunOutcome.sessionId,
    });
    releaseFocusBeforeUnmount();
    acknowledgeRunOutcome();
  };

  const handleDismiss = (): void => {
    releaseFocusBeforeUnmount();
    acknowledgeRunOutcome();
  };

  return (
    <>
      <div
        role="status"
        className="visually-hidden"
        data-testid="run-outcome-status-region"
      >
        {message ?? ""}
      </div>
      {lastRunOutcome !== null && message !== null && (
        <div
          ref={bannerRef}
          className={`${toneClassName(lastRunOutcome.status)} run-outcome-notice`}
          data-run-outcome={lastRunOutcome.status}
        >
          <span className="run-outcome-notice-message">{message}</span>
          <span className="run-outcome-notice-actions">
            {/* "View run", not "View results": a failed or empty run has no
                results, and the destination is the Run panel's diagnostics in
                both of those cases. The label must be true for all five
                terminal statuses this notice can carry. */}
            <button
              type="button"
              className="alert-banner-action"
              onClick={handleViewRun}
            >
              View run
            </button>
            <button
              type="button"
              className="alert-banner-action"
              onClick={handleDismiss}
              aria-label="Dismiss run outcome notice"
            >
              Dismiss
            </button>
          </span>
        </div>
      )}
    </>
  );
}
