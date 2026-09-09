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
// An earlier revision kept a SECOND permanently-mounted region with
// role="alert" carrying failed/cancelled. A design review commended that
// split; it is nevertheless the wrong shape, and this comment exists so it is
// not restored from that record.
//
// The reason is NOT a mechanical one, and an earlier version of this comment
// got that wrong: an empty, pre-existing role="alert" whose text later
// mutates announces perfectly well. The pre-exist rule above is the GENERAL
// live-region rule, not a polite-only one, and mutation-inside-a-stable-node
// is the robust form for assertive regions too (insertion is the form with
// the documented historical AT failures). So reliability does not decide
// this; the argument below does, which is why the ruling holds regardless.
//
//  1. CONSISTENCY WITH THE DECLARED AUTHORITY. ProgressView — the single
//     terminal-announcement authority named above — announces ALL FIVE
//     terminal statuses, failed and cancelled included, through a POLITE
//     role="status" region, and says so in its own comment. A second region
//     that escalated those two to assertive made the identical event MORE
//     urgent when the operator had deliberately looked away than when they
//     were watching it happen. That inversion is what convicts the split.
//     (For anyone auditing this chain later: that evidence is ProgressView's
//     region ELEMENT and its isTerminal set, read from source. A sweep soon
//     after found ProgressView's own live-region TESTS defective — they
//     mounted with the terminal status pre-set and so never drove the
//     transition — but those tests were never the evidence for this; they
//     merely failed to GUARD the behaviour, which da146cd67 fixed. The
//     ruling is unaffected.)
//
//  2. ASSERTIVE IS FOR WHAT INTERRUPTS. An assertive announcement cuts off
//     the current utterance mid-word and the interrupted content is not
//     re-read — so it costs a screen-reader user their place in whatever
//     they had left the Run tab to read. A finished background run is a
//     WCAG 4.1.3 status message (which role="status" satisfies), not an
//     action-forcing alert: nothing is lost by hearing it one utterance
//     later, because the visible banner persists until dismissed and the
//     Run-tab badge persists alongside it.
//
//  3. Incidentally but not trivially: a permanently-present second assertive
//     node made singular getByRole/findByRole("alert") ambiguous for every
//     other app-level surface, which demonstrably broke an unrelated test.
//
// The visible banner still carries the outcome TONE (error/warning/success/
// info), so urgency is expressed where it is free rather than in the live
// region's politeness setting. It borrows the .alert-banner CSS classes but
// deliberately NOT the <AlertBanner> component, which assigns role="alert"
// to strong tones and would silently reintroduce exactly what this removed.
//
// If a failure must ever genuinely interrupt, the correct build is ONE
// app-wide live-announcer utility owning a single shared assertive node —
// never a second per-feature region.
//
// Pure executionStore reader: no fetching, no polling, no run emitters
// (REQUEST_RUN_EVENT stays single-owner per App.tsx doctrine).
// ============================================================================

import { useRef } from "react";

import { Button } from "@/components/ui";
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
      {/* aria-live/aria-atomic are redundant with role="status" but stated
          explicitly, matching ProgressView's region — the belt-and-braces
          form this repo already uses for its announcement authority.
          The "-status-" in the test id predates the collapse to one region
          and names this region's ROLE; there is no sibling alert region to
          infer from it (see the header comment for why there is not). */}
      <div
        role="status"
        aria-live="polite"
        aria-atomic="true"
        className="visually-hidden"
        data-testid="run-outcome-status-region"
      >
        {message ?? ""}
      </div>
      {lastRunOutcome !== null && message !== null && (
        /* Fixed-overlay wrapper (elspeth-1c4687ff67 class): rendered in flow
           before the header, the strip's arrival displaced the entire app by
           its height and snapped it back on dismiss. The wrapper mirrors
           .app-notice-center's treatment (styles/shared.css) — fixed at the
           top of the viewport on --z-overlay with an opaque --color-surface
           ground beneath the .alert-banner tone tint (a bare 10-14% alpha
           tint over the header is text-on-text). Only the VISUAL banner moves
           into the overlay; the polite region above stays the one always-
           mounted announcement node. */
        <div className="run-outcome-notice-overlay">
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
              <Button
                variant="bare"
                className="alert-banner-action"
                onClick={handleViewRun}
              >
                View run
              </Button>
              <Button
                variant="bare"
                className="alert-banner-action"
                onClick={handleDismiss}
                aria-label="Dismiss run outcome notice"
              >
                Dismiss
              </Button>
            </span>
          </div>
        </div>
      )}
    </>
  );
}
