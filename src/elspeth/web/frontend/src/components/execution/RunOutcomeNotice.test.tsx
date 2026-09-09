import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { RunOutcomeNotice } from "./RunOutcomeNotice";
import { useExecutionStore, type RunOutcome } from "@/stores/executionStore";
import {
  REQUEST_ARTIFACT_VIEW_EVENT,
  type RequestArtifactViewDetail,
} from "@/lib/composer-events";

function seedOutcome(outcome: RunOutcome | null): void {
  useExecutionStore.setState({ lastRunOutcome: outcome });
}

function statusRegion(): HTMLElement {
  return screen.getByTestId("run-outcome-status-region");
}

function visualBanner(): HTMLElement | null {
  return document.querySelector<HTMLElement>(".run-outcome-notice");
}

/** Every node this component contributes with an assertive live-region role.
 *  Must always be empty: the notice announces politely for ALL five terminal
 *  statuses (see the component's doc comment). */
function assertiveRegions(container: HTMLElement): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>('[role="alert"]')];
}

describe("RunOutcomeNotice", () => {
  beforeEach(() => {
    useExecutionStore.getState().reset();
  });

  // The live region must PRE-EXIST its content: a polite region inserted
  // into the DOM with its text already present is the WAI-ARIA-documented
  // unreliable pattern (see AcknowledgementLiveRegion). The component is
  // always mounted; only the text (and the visual banner) toggles.
  it("keeps ONE persistent, empty polite region mounted with no outcome", () => {
    const { container } = render(<RunOutcomeNotice />);

    const status = statusRegion();
    expect(status).toHaveAttribute("role", "status");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveAttribute("aria-atomic", "true");
    expect(status).toHaveClass("visually-hidden");
    expect(status).toHaveTextContent("");
    // Exactly one live region, and no assertive one. The reason is NOT that
    // an empty role="alert" would fail to announce — it announces fine. It
    // is that ProgressView, the declared terminal-announcement authority,
    // announces all five terminal statuses POLITELY, so escalating two of
    // them here would make the same event more urgent when the operator has
    // looked away than when watching. A permanent second assertive node also
    // makes a singular getByRole("alert") ambiguous app-wide (it broke
    // App.test's chat-error query).
    expect(container.querySelectorAll('[role="status"]')).toHaveLength(1);
    expect(assertiveRegions(container)).toHaveLength(0);
    expect(visualBanner()).toBeNull();
  });

  // Mutation-not-insertion is the whole point of the persistent region, and
  // re-querying by test id CANNOT see it: the query resolves whether React
  // mutated the node or replaced it. Hold the element identity across the
  // transition instead, so a refactor that remounts the region — which would
  // silently restore the unreliable insert-with-content pattern — fails here.
  it("MUTATES the same region node across the empty-to-outcome transition", () => {
    render(<RunOutcomeNotice />);
    const before = statusRegion();

    act(() => {
      seedOutcome({ runId: "run-1", status: "completed", sessionId: "sess-1" });
    });
    expect(statusRegion()).toBe(before);

    act(() => {
      useExecutionStore.getState().acknowledgeRunOutcome();
    });
    expect(statusRegion()).toBe(before);
    expect(before).toHaveTextContent("");
  });

  it("announces a completed run through the pre-existing polite region and shows the success banner", () => {
    render(<RunOutcomeNotice />);
    // The region exists BEFORE the outcome; the announcement is a content
    // mutation inside it, never an insertion carrying its own text.
    expect(statusRegion()).toHaveTextContent("");

    act(() => {
      seedOutcome({ runId: "run-1", status: "completed", sessionId: "sess-1" });
    });

    // Byte-exact against the vocabulary ProgressView's live region speaks
    // (runTerminalPhrases) — the two surfaces announce one event, so the
    // wording may not drift.
    expect(statusRegion()).toHaveTextContent("Pipeline completed.");
    const banner = visualBanner();
    expect(banner).toHaveTextContent("Pipeline completed.");
    expect(banner).toHaveClass("alert-banner", "alert-banner--success");
  });

  it("announces completed_with_failures politely with the warning tone", () => {
    render(<RunOutcomeNotice />);

    act(() => {
      seedOutcome({
        runId: "run-1",
        status: "completed_with_failures",
        sessionId: "sess-1",
      });
    });

    expect(statusRegion()).toHaveTextContent(
      "Pipeline completed with failures.",
    );
    expect(visualBanner()).toHaveClass("alert-banner--warning");
  });

  it("announces an empty run politely with the info tone", () => {
    render(<RunOutcomeNotice />);

    act(() => {
      seedOutcome({ runId: "run-1", status: "empty", sessionId: "sess-1" });
    });

    expect(statusRegion()).toHaveTextContent(
      "Pipeline completed — no rows processed.",
    );
    expect(visualBanner()).toHaveClass("alert-banner--info");
  });

  // The audit only ever observed the success path — the FAILED path is the
  // one the root-cause analysis found equally silent (onFailed writes only
  // Run-panel-scoped state), so it is pinned explicitly here. It is also the
  // status that most tempts a future editor back into an assertive region:
  // the notice announces it POLITELY and mounts NO role="alert" node, because
  // a background run finishing does not interrupt what the user is doing.
  it("announces a FAILED run politely — never assertively — with the error tone", () => {
    const { container } = render(<RunOutcomeNotice />);
    expect(statusRegion()).toHaveTextContent("");

    act(() => {
      seedOutcome({ runId: "run-1", status: "failed", sessionId: "sess-1" });
    });

    expect(statusRegion()).toHaveTextContent("Pipeline failed.");
    expect(assertiveRegions(container)).toHaveLength(0);
    const banner = visualBanner();
    expect(banner).toHaveClass("alert-banner");
    expect(banner).not.toHaveClass("alert-banner--success");
  });

  it("announces a cancelled run politely — never assertively — with the warning tone", () => {
    const { container } = render(<RunOutcomeNotice />);

    act(() => {
      seedOutcome({ runId: "run-1", status: "cancelled", sessionId: "sess-1" });
    });

    expect(statusRegion()).toHaveTextContent("Pipeline execution was cancelled.");
    expect(assertiveRegions(container)).toHaveLength(0);
    expect(visualBanner()).toHaveClass("alert-banner--warning");
  });

  it("'View run' dispatches the Run artifact intent for the outcome's session and acknowledges", () => {
    const artifactRequests: RequestArtifactViewDetail[] = [];
    const onArtifactRequest = (event: Event): void => {
      artifactRequests.push(
        (event as CustomEvent<RequestArtifactViewDetail>).detail,
      );
    };
    window.addEventListener(REQUEST_ARTIFACT_VIEW_EVENT, onArtifactRequest);
    seedOutcome({ runId: "run-1", status: "failed", sessionId: "sess-1" });
    render(<RunOutcomeNotice />);

    fireEvent.click(screen.getByRole("button", { name: "View run" }));

    expect(artifactRequests).toEqual([
      { tab: "run", focusMode: false, sessionId: "sess-1" },
    ]);
    expect(useExecutionStore.getState().lastRunOutcome).toBeNull();
    expect(visualBanner()).toBeNull();
    expect(statusRegion()).toHaveTextContent("");
    window.removeEventListener(REQUEST_ARTIFACT_VIEW_EVENT, onArtifactRequest);
  });

  it("Dismiss acknowledges without dispatching any artifact intent", () => {
    const onArtifactRequest = vi.fn();
    window.addEventListener(REQUEST_ARTIFACT_VIEW_EVENT, onArtifactRequest);
    seedOutcome({ runId: "run-1", status: "completed", sessionId: "sess-1" });
    render(<RunOutcomeNotice />);

    fireEvent.click(
      screen.getByRole("button", { name: "Dismiss run outcome notice" }),
    );

    expect(onArtifactRequest).not.toHaveBeenCalled();
    expect(useExecutionStore.getState().lastRunOutcome).toBeNull();
    expect(visualBanner()).toBeNull();
    expect(statusRegion()).toHaveTextContent("");
    window.removeEventListener(REQUEST_ARTIFACT_VIEW_EVENT, onArtifactRequest);
  });

  it("empties the region once the outcome is acknowledged from anywhere (Run-tab suppression path)", () => {
    seedOutcome({ runId: "run-1", status: "failed", sessionId: "sess-1" });
    render(<RunOutcomeNotice />);
    expect(statusRegion()).toHaveTextContent("Pipeline failed.");

    // ArtifactWorkspace acknowledges in a layout effect whenever the Run tab
    // is active and visible (ProgressView is the single announcement
    // authority there) — the region must empty on that store transition
    // alone, with no local interaction, while STAYING mounted.
    act(() => {
      useExecutionStore.getState().acknowledgeRunOutcome();
    });
    expect(statusRegion()).toHaveTextContent("");
    expect(visualBanner()).toBeNull();
  });

  // Focus stranding (elspeth-bcd1a9b9b3 defect class): the banner unmounts
  // on acknowledge; if focus were inside it the browser would reset focus to
  // <body>, dumping keyboard users at the top of the document.
  describe("focus handoff on acknowledge", () => {
    let mainContent: HTMLDivElement;

    beforeEach(() => {
      mainContent = document.createElement("div");
      mainContent.id = "composer-main";
      mainContent.tabIndex = -1;
      document.body.appendChild(mainContent);
    });

    afterEach(() => {
      mainContent.remove();
    });

    it("hands focus to the main content region before Dismiss unmounts the banner", () => {
      seedOutcome({ runId: "run-1", status: "completed", sessionId: "sess-1" });
      render(<RunOutcomeNotice />);
      const dismiss = screen.getByRole("button", {
        name: "Dismiss run outcome notice",
      });
      dismiss.focus();
      expect(dismiss).toHaveFocus();

      fireEvent.click(dismiss);

      expect(visualBanner()).toBeNull();
      expect(mainContent).toHaveFocus();
    });

    it("hands focus to the main content region on View run when the intent found no listener", () => {
      // Narrow-viewport / no-workspace case: nothing consumed the artifact
      // intent, so nothing re-homed focus — the notice must not strand it.
      seedOutcome({ runId: "run-1", status: "failed", sessionId: "sess-1" });
      render(<RunOutcomeNotice />);
      const viewRun = screen.getByRole("button", { name: "View run" });
      viewRun.focus();

      fireEvent.click(viewRun);

      expect(visualBanner()).toBeNull();
      expect(mainContent).toHaveFocus();
    });

    it("does not steal focus that already left the notice (mouse dismiss, intent-focused workspace)", () => {
      seedOutcome({ runId: "run-1", status: "completed", sessionId: "sess-1" });
      render(<RunOutcomeNotice />);
      const outside = document.createElement("button");
      document.body.appendChild(outside);
      outside.focus();

      fireEvent.click(
        screen.getByRole("button", { name: "Dismiss run outcome notice" }),
      );

      expect(outside).toHaveFocus();
      outside.remove();
    });
  });
});
