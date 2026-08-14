import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type ReactNode, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CommandPalette } from "./CommandPalette";
import {
  FOCUS_AUTHORING_EVENT,
  OPEN_GRAPH_MODAL_EVENT,
  REQUEST_ARTIFACT_VIEW_EVENT,
  claimWorkspaceViewIntent,
  isCurrentWorkspaceViewIntent,
  REQUEST_RUN_EVENT,
  type RequestArtifactViewDetail,
} from "@/lib/composer-events";
import { useSessionStore } from "@/stores/sessionStore";
import {
  EXECUTION_BLOCKED_VALIDATION_READINESS,
  makeValidationResult,
  READY_VALIDATION_READINESS,
} from "@/test/composerFixtures";
import { resetStore } from "@/test/store-helpers";
import type { GuidedSession } from "@/types/guided";
import type { ValidationResult } from "@/types/index";
import { ComposerWorkspace } from "@/components/workspace/ComposerWorkspace";
import { ArtifactWorkspace } from "@/components/workspace/ArtifactWorkspace";
import { useHashRouter } from "@/hooks/useHashRouter";

vi.mock("@xyflow/react", () => ({
  MarkerType: { ArrowClosed: "arrowclosed" },
  Position: { Top: "top", Bottom: "bottom" },
  ReactFlowProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
  ReactFlow: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  BaseEdge: () => null,
  Handle: () => null,
  Background: () => null,
  Controls: () => null,
  MiniMap: () => null,
}));
vi.mock("@xyflow/react/dist/style.css", () => ({}));
vi.mock("@/components/inspector/YamlView", () => ({
  YamlView: () => <div>YAML artifact</div>,
}));

class PassiveResizeObserver implements ResizeObserver {
  constructor(_callback: ResizeObserverCallback) {}
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

vi.mock("@/api/client", () => ({
  fetchSessions: vi.fn(),
  createSession: vi.fn(),
  fetchMessages: vi.fn(),
  fetchCompositionState: vi.fn(),
  fetchComposerProgress: vi.fn(),
  sendMessage: vi.fn(),
  recompose: vi.fn(),
  forkFromMessage: vi.fn(),
  revertToVersion: vi.fn(),
  fetchStateVersions: vi.fn(),
  archiveSession: vi.fn(),
  getGuided: vi.fn(),
  respondGuided: vi.fn(),
  reenterGuided: vi.fn(),
  chatGuided: vi.fn(),
  fetchYaml: vi.fn().mockResolvedValue({ yaml: "sources: {}" }),
}));

const executionStoreState = vi.hoisted(() => ({
  execute: vi.fn(),
  validationResult: null as ValidationResult | null,
  isExecuting: false,
  progress: null as null | { status: string },
}));

vi.mock("@/stores/executionStore", () => ({
  useExecutionStore: (selector: (state: unknown) => unknown) =>
    selector(executionStoreState),
}));

const exitedGuidedSession: GuidedSession = {
  step: "step_1_source",
  history: [],
  terminal: {
    kind: "exited_to_freeform",
    reason: "user_pressed_exit",
    pipeline_yaml: null,
  },
  chat_history: [],
  chat_turn_seq: 0,
  profile: null,
};

describe("CommandPalette guided-mode commands", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    executionStoreState.validationResult = null;
    executionStoreState.isExecuting = false;
    executionStoreState.progress = null;
    Element.prototype.scrollIntoView = vi.fn();
    resetStore(useSessionStore);
    vi.stubGlobal("ResizeObserver", PassiveResizeObserver);
  });

  it("closes before YAML supersedes a deferred Spec hash so final focus lands on YAML", async () => {
    const user = userEvent.setup();
    useSessionStore.setState({
      activeSessionId: "session-1",
      sessions: [{ id: "session-1", title: "Session 1" }],
      compositionStateLoaded: false,
      compositionState: {
        id: "state-1",
        version: 1,
        sources: { source: { plugin: "csv", options: {} } },
        nodes: [],
        edges: [],
        outputs: [],
        metadata: { name: null, description: null },
      },
    } as never);
    window.history.replaceState(null, "", "#/session-1/spec");

    function Harness() {
      useHashRouter();
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            Open palette
          </button>
          <ComposerWorkspace
            authoring={<div>Authoring</div>}
            artifact={<ArtifactWorkspace />}
            inspector={<div>Inspector</div>}
            actionBar={<div>Actions</div>}
          />
          <CommandPalette
            isOpen={open}
            onClose={() => setOpen(false)}
            runAdmissionAvailable
          />
        </>
      );
    }

    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Open palette" }));
    await user.click(screen.getByRole("option", { name: /export yaml/i }));
    act(() => useSessionStore.setState({ compositionStateLoaded: true }));
    await act(async () => Promise.resolve());

    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Command palette" })).toBeNull();
      expect(screen.getByRole("tab", { name: "YAML" })).toHaveFocus();
    });
    expect(screen.getByRole("tab", { name: "YAML" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("offers Re-enter guided mode for a user-exited guided session", async () => {
    const user = userEvent.setup();
    const reenterGuided = vi.fn().mockResolvedValue(undefined);
    const onClose = vi.fn();
    useSessionStore.setState({
      activeSessionId: "session-1",
      guidedSession: exitedGuidedSession,
      guidedTerminal: exitedGuidedSession.terminal,
      reenterGuided,
    });

    render(
      <CommandPalette
        isOpen
        onClose={onClose}
        runAdmissionAvailable
      />,
    );

    await user.click(
      screen.getByRole("option", { name: /re-enter guided mode/i }),
    );

    expect(reenterGuided).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("does not offer navigation to the removed Runs tab", () => {
    render(
      <CommandPalette isOpen onClose={vi.fn()} runAdmissionAvailable />,
    );

    expect(
      screen.queryByRole("option", { name: /Switch to Runs Tab/i }),
    ).toBeNull();
  });

  it("labels command groups for assistive technology", () => {
    useSessionStore.setState({
      activeSessionId: "session-1",
      sessions: [
        {
          id: "session-2",
          title: "Earlier analysis",
          created_at: "2026-06-16T00:00:00Z",
          updated_at: "2026-06-16T00:00:00Z",
          archived: false,
        },
      ],
    });

    render(
      <CommandPalette isOpen onClose={vi.fn()} runAdmissionAvailable />,
    );

    expect(screen.getByRole("group", { name: "Actions" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Navigation" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Sessions" })).toBeInTheDocument();
  });

  it("does not offer navigation to the removed Spec tab", () => {
    render(
      <CommandPalette isOpen onClose={vi.fn()} runAdmissionAvailable />,
    );

    expect(
      screen.queryByRole("option", { name: /Switch to Spec Tab/i }),
    ).toBeNull();
  });

  it("requests the Graph artifact without opening the legacy modal", async () => {
    const artifactRequests: RequestArtifactViewDetail[] = [];
    const onArtifactRequest = (event: Event) => {
      artifactRequests.push(
        (event as CustomEvent<RequestArtifactViewDetail>).detail,
      );
    };
    const onLegacyModal = vi.fn();
    window.addEventListener(REQUEST_ARTIFACT_VIEW_EVENT, onArtifactRequest);
    window.addEventListener(OPEN_GRAPH_MODAL_EVENT, onLegacyModal);
    useSessionStore.setState({ activeSessionId: "session-1" });

    render(
      <CommandPalette isOpen onClose={vi.fn()} runAdmissionAvailable />,
    );
    const priorIntent = claimWorkspaceViewIntent();
    fireEvent.click(screen.getByText(/show graph/i));
    expect(isCurrentWorkspaceViewIntent(priorIntent)).toBe(false);
    await waitFor(() => {
      expect(artifactRequests).toEqual([
        { tab: "graph", focusMode: false, sessionId: "session-1" },
      ]);
    });

    expect(onLegacyModal).not.toHaveBeenCalled();
    window.removeEventListener(REQUEST_ARTIFACT_VIEW_EVENT, onArtifactRequest);
    window.removeEventListener(OPEN_GRAPH_MODAL_EVENT, onLegacyModal);
  });

  it("routes Focus Chat through the shared authoring-view intent", async () => {
    const onFocusAuthoring = vi.fn();
    window.addEventListener(FOCUS_AUTHORING_EVENT, onFocusAuthoring);
    render(
      <CommandPalette isOpen onClose={vi.fn()} runAdmissionAvailable />,
    );

    fireEvent.click(screen.getByText("Focus chat input"));

    await waitFor(() => {
      expect(onFocusAuthoring).toHaveBeenCalledTimes(1);
    });
    window.removeEventListener(FOCUS_AUTHORING_EVENT, onFocusAuthoring);
  });

  it("requests the YAML artifact when the pipeline has content", async () => {
    const artifactRequests: RequestArtifactViewDetail[] = [];
    const onArtifactRequest = (event: Event) => {
      artifactRequests.push(
        (event as CustomEvent<RequestArtifactViewDetail>).detail,
      );
    };
    window.addEventListener(REQUEST_ARTIFACT_VIEW_EVENT, onArtifactRequest);
    useSessionStore.setState({
      activeSessionId: "session-1",
      compositionState: {
        id: "state-1",
        version: 1,
        sources: { source: { plugin: "csv", options: {} } },
        nodes: [],
        edges: [],
        outputs: [],
        metadata: { name: null, description: null },
      },
    } as never);

    render(
      <CommandPalette isOpen onClose={vi.fn()} runAdmissionAvailable />,
    );
    fireEvent.click(screen.getByText(/export yaml/i));
    await waitFor(() => {
      expect(artifactRequests).toEqual([
        { tab: "yaml", focusMode: false, sessionId: "session-1" },
      ]);
    });

    window.removeEventListener(REQUEST_ARTIFACT_VIEW_EVENT, onArtifactRequest);
  });

  // elspeth-bff8043d33 residual: the palette command was a leftover path
  // into the near-empty Export-YAML modal. Same hasCompositionContent gate
  // as ExportYamlButton — the command is withheld entirely (disabled
  // commands are filtered from the palette, matching Validate/Execute).
  it("withholds 'Export YAML' when the pipeline is empty", () => {
    render(
      <CommandPalette isOpen onClose={vi.fn()} runAdmissionAvailable />,
    );

    expect(screen.queryByText(/export yaml/i)).toBeNull();
  });

  it("offers persistent Graph navigation and withholds YAML without content", () => {
    render(
      <CommandPalette isOpen onClose={vi.fn()} runAdmissionAvailable />,
    );

    expect(
      screen.getByRole("option", { name: /show graph/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("option", { name: /export yaml/i }),
    ).toBeNull();
  });

  it("withholds Execute when structural validity passes but execution readiness is false", () => {
    useSessionStore.setState({ activeSessionId: "session-1" });
    executionStoreState.validationResult = makeValidationResult({
      readiness: EXECUTION_BLOCKED_VALIDATION_READINESS,
    });

    render(
      <CommandPalette isOpen onClose={vi.fn()} runAdmissionAvailable />,
    );

    expect(screen.queryByText("Execute pipeline")).not.toBeInTheDocument();
  });

  it("withholds Execute when a malformed validation response omits readiness", () => {
    useSessionStore.setState({ activeSessionId: "session-1" });
    // Deliberately model untrusted wire data that violates the mandatory
    // TypeScript contract. Ordinary fixtures must use makeValidationResult.
    executionStoreState.validationResult = {
      is_valid: true,
      checks: [],
      errors: [],
      warnings: [],
    } as unknown as ValidationResult;

    render(
      <CommandPalette isOpen onClose={vi.fn()} runAdmissionAvailable />,
    );

    expect(screen.queryByText("Execute pipeline")).not.toBeInTheDocument();
  });

  it("withholds Execute when the run-admission owner is not mounted", () => {
    useSessionStore.setState({ activeSessionId: "session-1" });
    executionStoreState.validationResult = makeValidationResult({
      readiness: READY_VALIDATION_READINESS,
    });

    render(
      <CommandPalette
        isOpen
        onClose={vi.fn()}
        runAdmissionAvailable={false}
      />,
    );

    expect(screen.queryByText("Execute pipeline")).not.toBeInTheDocument();
  });

  it("dispatches run intent for an execution-ready pipeline", () => {
    const onRequestRun = vi.fn();
    const onClose = vi.fn();
    window.addEventListener(REQUEST_RUN_EVENT, onRequestRun);
    useSessionStore.setState({ activeSessionId: "session-1" });
    executionStoreState.validationResult = makeValidationResult({
      readiness: READY_VALIDATION_READINESS,
    });

    render(
      <CommandPalette
        isOpen
        onClose={onClose}
        runAdmissionAvailable
      />,
    );
    fireEvent.click(screen.getByText("Execute pipeline"));

    expect(onRequestRun).toHaveBeenCalledTimes(1);
    expect(executionStoreState.execute).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalledTimes(1);
    window.removeEventListener(REQUEST_RUN_EVENT, onRequestRun);
  });

  it.each([
    ["execution request is starting", true, null],
    ["a run is active", false, { status: "running" }],
  ])("withholds Execute while %s", (_label, isExecuting, progress) => {
    useSessionStore.setState({ activeSessionId: "session-1" });
    executionStoreState.validationResult = makeValidationResult({
      readiness: READY_VALIDATION_READINESS,
    });
    executionStoreState.isExecuting = isExecuting;
    executionStoreState.progress = progress;

    render(
      <CommandPalette isOpen onClose={vi.fn()} runAdmissionAvailable />,
    );

    expect(screen.queryByText("Execute pipeline")).not.toBeInTheDocument();
  });
});
