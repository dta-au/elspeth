import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { makeComposition } from "@/test/composerFixtures";
import { resetStore } from "@/test/store-helpers";
import { useSessionStore } from "@/stores/sessionStore";
import {
  useWorkspacePaneController,
  WorkspacePaneProvider,
} from "./WorkspacePaneContext";
import { WorkspaceInspector } from "./WorkspaceInspector";
import type { WorkspacePaneState } from "./useWorkspacePaneState";

const historyPanelState = vi.hoisted(() => ({ throws: false }));

vi.mock("@/components/chat/guided/GuidedHistory", async (importOriginal) => {
  const actual = await importOriginal<
    typeof import("@/components/chat/guided/GuidedHistory")
  >();
  return {
    ...actual,
    GuidedHistory: () => {
      if (historyPanelState.throws) throw new Error("history exploded");
      return (
        <div>
          History panel <button type="button">History detail action</button>
        </div>
      );
    },
  };
});

interface HarnessProps {
  removeInvokerOnOpen?: boolean;
  mountFallbackTrigger?: boolean;
}

/* Post-Checks-tab contract: the Inspector is the HISTORY drawer. Validation
   and Audit render inline in the artifact pane's Checks tab (ChecksView);
   the only opener left is the artifact toolbar's History trigger, whose
   element id is the focus-restore fallback when the exact invoker is gone. */
function InspectorHarness({
  removeInvokerOnOpen = false,
  mountFallbackTrigger = false,
}: HarnessProps): JSX.Element {
  const [open, setOpen] = useState(false);
  const [showInvoker, setShowInvoker] = useState(true);
  const paneState: WorkspacePaneState = {
    paneBounds: { min: 360, max: 640, defaultWidth: 420, resizable: true },
    preferredAuthoringWidth: 420,
    effectiveAuthoringWidth: 420,
    authoringCollapsed: false,
    availableArtifactTabs: ["graph", "spec", "yaml", "checks", "run"],
    activeArtifactTab: "graph",
    activeInspectorTab: open ? "history" : null,
    inspectorOpen: open,
    resizeTransient: vi.fn(),
    commitResize: vi.fn(),
    setAuthoringCollapsed: vi.fn(),
    selectArtifactTab: vi.fn(),
    openInspector: () => {
      setOpen(true);
      if (removeInvokerOnOpen) setShowInvoker(false);
    },
    closeInspector: () => setOpen(false),
  };

  return (
    <WorkspacePaneProvider paneState={paneState}>
      {showInvoker && <OpenHistoryButton />}
      {mountFallbackTrigger && (
        <button type="button" id="artifact-history-trigger">
          History fallback trigger
        </button>
      )}
      <button type="button">Unrelated control</button>
      <WorkspaceInspector />
    </WorkspacePaneProvider>
  );
}

function OpenHistoryButton(): JSX.Element {
  const { actions } = useWorkspacePaneController();
  return (
    <button
      type="button"
      onClick={(event) => actions.openInspector("history", event.currentTarget)}
    >
      Open history
    </button>
  );
}

function activeGuidedSession() {
  return {
    step: "step_3_transforms" as const,
    history: [
      {
        step: "step_1_source" as const,
        turn_type: "single_select" as const,
        payload_hash: "payload",
        response_hash: "response",
        summary: "Use a CSV source",
        emitter: "server" as const,
      },
    ],
    terminal: null,
    chat_history: [],
    chat_turn_seq: 0,
    reviewed_components: { sources: [], outputs: [] },
    profile: null,
  };
}

describe("WorkspaceInspector", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
    historyPanelState.throws = false;
    useSessionStore.setState({
      activeSessionId: "session-1",
      compositionState: makeComposition(4),
      guidedSession: activeGuidedSession(),
    } as never);
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 1920,
      writable: true,
    });
  });

  it("stays mounted while hidden and shows the guided history when opened", async () => {
    const user = userEvent.setup();
    const { container } = render(<InspectorHarness />);
    const aside = container.querySelector("aside");
    expect(aside).not.toBeNull();
    expect(aside).toHaveAttribute("hidden");

    await user.click(screen.getByRole("button", { name: "Open history" }));

    expect(aside).not.toHaveAttribute("hidden");
    expect(
      screen.getByRole("complementary", { name: "History" }),
    ).toBeVisible();
    expect(screen.getByText("History panel")).toBeInTheDocument();
  });

  it("restores focus to the exact invoker on Close and Escape", async () => {
    const user = userEvent.setup();
    render(<InspectorHarness />);
    const invoker = screen.getByRole("button", { name: "Open history" });
    await user.click(invoker);
    await user.click(screen.getByRole("button", { name: "Close history" }));
    expect(invoker).toHaveFocus();

    await user.click(invoker);
    screen.getByRole("button", { name: "History detail action" }).focus();
    await user.keyboard("{Escape}");
    expect(invoker).toHaveFocus();
  });

  it("falls back to the artifact history trigger when the exact invoker disconnects", async () => {
    const user = userEvent.setup();
    render(<InspectorHarness removeInvokerOnOpen mountFallbackTrigger />);
    await user.click(screen.getByRole("button", { name: "Open history" }));
    expect(
      screen.queryByRole("button", { name: "Open history" }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Close history" }));
    expect(
      screen.getByRole("button", { name: "History fallback trigger" }),
    ).toHaveFocus();
  });

  it("closes itself and returns focus when the open history disappears under focus", async () => {
    const user = userEvent.setup();
    const { container } = render(<InspectorHarness />);
    const invoker = screen.getByRole("button", { name: "Open history" });
    await user.click(invoker);
    screen.getByRole("button", { name: "History detail action" }).focus();

    act(() => {
      useSessionStore.setState({
        guidedSession: { ...activeGuidedSession(), history: [] },
      } as never);
    });

    await waitFor(() => {
      expect(container.querySelector("aside")).toHaveAttribute("hidden");
    });
    expect(invoker).toHaveFocus();
  });

  it("does not steal unrelated focus when the open history disappears", async () => {
    const user = userEvent.setup();
    const { container } = render(<InspectorHarness />);
    await user.click(screen.getByRole("button", { name: "Open history" }));
    const unrelated = screen.getByRole("button", { name: "Unrelated control" });
    unrelated.focus();

    act(() => {
      useSessionStore.setState({ guidedSession: null } as never);
    });

    await waitFor(() => {
      expect(container.querySelector("aside")).toHaveAttribute("hidden");
    });
    expect(unrelated).toHaveFocus();
  });

  it("applies the compact overlay class below 1536 without modal semantics", async () => {
    Object.defineProperty(window, "innerWidth", { value: 1280, writable: true });
    render(<InspectorHarness />);
    await userEvent.setup().click(
      screen.getByRole("button", { name: "Open history" }),
    );

    const inspector = screen.getByRole("complementary", { name: "History" });
    expect(inspector).toHaveClass("workspace-inspector--overlay");
    expect(inspector).not.toHaveAttribute("aria-modal");
  });

  it("keeps Close operable when the history body throws", async () => {
    historyPanelState.throws = true;
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    const preventExpectedWindowError = (event: ErrorEvent) => {
      event.preventDefault();
    };
    window.addEventListener("error", preventExpectedWindowError);
    const user = userEvent.setup();
    render(<InspectorHarness />);
    const invoker = screen.getByRole("button", { name: "Open history" });
    await user.click(invoker);

    expect(screen.getByRole("alert")).toHaveTextContent(
      "History encountered an error",
    );
    await user.click(screen.getByRole("button", { name: "Close history" }));
    expect(invoker).toHaveFocus();
    window.removeEventListener("error", preventExpectedWindowError);
    consoleError.mockRestore();
  });
});
