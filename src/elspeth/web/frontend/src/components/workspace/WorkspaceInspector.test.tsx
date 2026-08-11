import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  REQUEST_ARTIFACT_VIEW_EVENT,
  type RequestArtifactViewDetail,
} from "@/lib/composer-events";
import { makeComposition } from "@/test/composerFixtures";
import { resetStore } from "@/test/store-helpers";
import { useSessionStore } from "@/stores/sessionStore";
import {
  useWorkspacePaneController,
  WorkspacePaneProvider,
} from "./WorkspacePaneContext";
import { WorkspaceInspector } from "./WorkspaceInspector";
import type { WorkspacePaneState } from "./useWorkspacePaneState";
import type { InspectorTab } from "./workspaceTypes";

const panelState = vi.hoisted(() => ({
  auditThrows: false,
  auditMounts: 0,
  auditUnmounts: 0,
  validationMounts: 0,
  validationUnmounts: 0,
}));

vi.mock("@/components/audit/AuditReadinessPanel", () => ({
  AuditReadinessPanel: ({
    onSelectComponent,
  }: {
    onSelectComponent?: (componentId: string) => void;
  }) => {
    useEffect(() => {
      panelState.auditMounts += 1;
      return () => {
        panelState.auditUnmounts += 1;
      };
    }, []);
    if (panelState.auditThrows) throw new Error("audit exploded");
    return (
      <div>
        Audit panel
        <button
          type="button"
          onClick={() => onSelectComponent?.("select_columns")}
        >
          Audit component
        </button>
      </div>
    );
  },
}));

vi.mock("@/components/sidebar/SideRailValidationBanner", () => ({
  SideRailValidationBanner: ({
    onSelectComponent,
  }: {
    onSelectComponent?: (componentId: string) => void;
  }) => {
    useEffect(() => {
      panelState.validationMounts += 1;
      return () => {
        panelState.validationUnmounts += 1;
      };
    }, []);
    return (
      <div>
        Validation panel
        <button
          type="button"
          onClick={() => onSelectComponent?.("select_columns")}
        >
          Validation component
        </button>
      </div>
    );
  },
}));

interface HarnessProps {
  initialTab?: InspectorTab | null;
  removeInvokerOnOpen?: boolean;
}

function InspectorHarness({
  initialTab = null,
  removeInvokerOnOpen = false,
}: HarnessProps): JSX.Element {
  const [activeTab, setActiveTab] = useState<InspectorTab | null>(initialTab);
  const [showInvoker, setShowInvoker] = useState(true);
  const paneState: WorkspacePaneState = {
    paneBounds: { min: 360, max: 640, defaultWidth: 420, resizable: true },
    preferredAuthoringWidth: 420,
    effectiveAuthoringWidth: 420,
    authoringCollapsed: false,
    availableArtifactTabs: ["graph", "spec", "yaml", "run"],
    activeArtifactTab: "graph",
    activeInspectorTab: activeTab,
    inspectorOpen: activeTab !== null,
    resizeTransient: vi.fn(),
    commitResize: vi.fn(),
    setAuthoringCollapsed: vi.fn(),
    selectArtifactTab: vi.fn(),
    openInspector: (tab) => {
      setActiveTab(tab);
      if (removeInvokerOnOpen) setShowInvoker(false);
    },
    closeInspector: () => setActiveTab(null),
  };

  return (
    <WorkspacePaneProvider paneState={paneState}>
      <div
        id="workspace-status-controls"
        data-workspace-status-controls="true"
        role="group"
        aria-label="Workspace status"
        tabIndex={-1}
      >
        {showInvoker && (
          <OpenInspectorButton tab="validation">
            Open validation
          </OpenInspectorButton>
        )}
        <OpenInspectorButton tab="audit">Open audit</OpenInspectorButton>
      </div>
      <WorkspaceInspector />
    </WorkspacePaneProvider>
  );
}

function OpenInspectorButton({
  tab,
  children,
}: {
  tab: InspectorTab;
  children: string;
}): JSX.Element {
  const { actions } = useWorkspacePaneController();
  return (
    <button
      type="button"
      onClick={(event) => actions.openInspector(tab, event.currentTarget)}
    >
      {children}
    </button>
  );
}

describe("WorkspaceInspector", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
    useSessionStore.setState({
      activeSessionId: "session-1",
      compositionState: makeComposition(4),
      selectNode: vi.fn(),
    } as never);
    Object.assign(panelState, {
      auditThrows: false,
      auditMounts: 0,
      auditUnmounts: 0,
      validationMounts: 0,
      validationUnmounts: 0,
    });
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 1920,
      writable: true,
    });
  });

  it("stays mounted while hidden and preserves both persistent panel bodies", async () => {
    const user = userEvent.setup();
    const { container } = render(<InspectorHarness />);
    const aside = container.querySelector("aside");
    expect(aside).not.toBeNull();
    expect(aside).toHaveAttribute("hidden");
    expect(panelState.validationMounts).toBe(1);
    expect(panelState.auditMounts).toBe(1);

    await user.click(screen.getByRole("button", { name: "Open audit" }));
    expect(aside).not.toHaveAttribute("hidden");
    await user.click(screen.getByRole("button", { name: "Close inspector" }));

    expect(aside).toHaveAttribute("hidden");
    expect(panelState.validationMounts).toBe(1);
    expect(panelState.auditMounts).toBe(1);
    expect(panelState.validationUnmounts).toBe(0);
    expect(panelState.auditUnmounts).toBe(0);
  });

  it("renders Validation, Audit, and conditional History as roving tabs", async () => {
    const user = userEvent.setup();
    useSessionStore.setState({
      guidedSession: {
        step: "step_3_transforms",
        history: [
          {
            step: "step_1_source",
            turn_type: "single_select",
            payload_hash: "payload",
            response_hash: "response",
            summary: "Use a CSV source",
            emitter: "server",
          },
        ],
        terminal: null,
        chat_history: [],
        chat_turn_seq: 0,
        profile: null,
      },
    } as never);
    render(<InspectorHarness />);
    await user.click(screen.getByRole("button", { name: "Open validation" }));

    const validation = screen.getByRole("tab", { name: "Validation" });
    const audit = screen.getByRole("tab", { name: "Audit" });
    const history = screen.getByRole("tab", { name: "History" });
    expect(validation).toHaveAttribute("aria-selected", "true");
    expect(validation).toHaveAttribute("tabindex", "0");
    expect(audit).toHaveAttribute("tabindex", "-1");
    expect(screen.getAllByRole("tabpanel", { hidden: true })).toHaveLength(3);

    validation.focus();
    await user.keyboard("{ArrowRight}");
    expect(audit).toHaveFocus();
    expect(audit).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{End}");
    expect(history).toHaveFocus();
    await user.keyboard("{Home}");
    expect(validation).toHaveFocus();
  });

  it("restores focus to the exact invoker on Close and Escape", async () => {
    const user = userEvent.setup();
    render(<InspectorHarness />);
    const validationInvoker = screen.getByRole("button", {
      name: "Open validation",
    });
    await user.click(validationInvoker);
    await user.click(screen.getByRole("button", { name: "Close inspector" }));
    expect(validationInvoker).toHaveFocus();

    const auditInvoker = screen.getByRole("button", { name: "Open audit" });
    await user.click(auditInvoker);
    screen.getByRole("tab", { name: "Audit" }).focus();
    await user.keyboard("{Escape}");
    expect(auditInvoker).toHaveFocus();
  });

  it("focuses the status-control group when the exact invoker disconnects", async () => {
    const user = userEvent.setup();
    render(<InspectorHarness removeInvokerOnOpen />);
    await user.click(screen.getByRole("button", { name: "Open validation" }));
    expect(
      screen.queryByRole("button", { name: "Open validation" }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Close inspector" }));
    expect(
      screen.getByRole("group", { name: "Workspace status" }),
    ).toHaveFocus();
  });

  it("applies the compact overlay class below 1536 without modal semantics", async () => {
    Object.defineProperty(window, "innerWidth", { value: 1280, writable: true });
    render(<InspectorHarness />);
    await userEvent.setup().click(
      screen.getByRole("button", { name: "Open audit" }),
    );

    const inspector = screen.getByRole("complementary", { name: "Inspector" });
    expect(inspector).toHaveClass("workspace-inspector--overlay");
    expect(inspector).not.toHaveAttribute("aria-modal");
  });

  it("keeps tabs and Close operable when one inspector body throws", async () => {
    panelState.auditThrows = true;
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    const preventExpectedWindowError = (event: ErrorEvent) => {
      event.preventDefault();
    };
    window.addEventListener("error", preventExpectedWindowError);
    const user = userEvent.setup();
    render(<InspectorHarness />);
    await user.click(screen.getByRole("button", { name: "Open audit" }));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Audit inspector encountered an error",
    );
    expect(screen.getByRole("tab", { name: "Validation" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Close inspector" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Close inspector" }));
    expect(screen.getByRole("button", { name: "Open audit" })).toHaveFocus();
    window.removeEventListener("error", preventExpectedWindowError);
    consoleError.mockRestore();
  });

  it("routes component navigation to Graph focus without opening the graph modal", async () => {
    const user = userEvent.setup();
    const selectNode = vi.fn();
    const artifactRequests: RequestArtifactViewDetail[] = [];
    const graphModal = vi.fn();
    useSessionStore.setState({ selectNode } as never);
    const onArtifactRequest = (event: Event) => {
      artifactRequests.push(
        (event as CustomEvent<RequestArtifactViewDetail>).detail,
      );
    };
    window.addEventListener(REQUEST_ARTIFACT_VIEW_EVENT, onArtifactRequest);
    window.addEventListener("elspeth-open-graph-modal", graphModal);
    try {
      render(<InspectorHarness />);
      await user.click(screen.getByRole("button", { name: "Open validation" }));
      await user.click(
        screen.getByRole("button", { name: "Validation component" }),
      );

      expect(selectNode).toHaveBeenCalledExactlyOnceWith("select_columns");
      expect(artifactRequests).toEqual([
        { tab: "graph", focusMode: false, sessionId: "session-1" },
      ]);
      expect(graphModal).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener(REQUEST_ARTIFACT_VIEW_EVENT, onArtifactRequest);
      window.removeEventListener("elspeth-open-graph-modal", graphModal);
    }
  });
});
