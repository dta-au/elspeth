import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CommandPalette } from "./CommandPalette";
import {
  OPEN_GRAPH_MODAL_EVENT,
  OPEN_YAML_MODAL_EVENT,
  REQUEST_RUN_EVENT,
} from "@/lib/composer-events";
import { useSessionStore } from "@/stores/sessionStore";
import { resetStore } from "@/test/store-helpers";
import type { GuidedSession } from "@/types/guided";

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
}));

const executionStoreState = vi.hoisted(() => ({
  execute: vi.fn(),
  validationResult: null as null | {
    is_valid: boolean;
    readiness: {
      execution_ready: boolean;
    };
  },
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

    render(<CommandPalette isOpen onClose={onClose} />);

    await user.click(
      screen.getByRole("option", { name: /re-enter guided mode/i }),
    );

    expect(reenterGuided).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("does not offer navigation to the removed Runs tab", () => {
    render(<CommandPalette isOpen onClose={vi.fn()} />);

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

    render(<CommandPalette isOpen onClose={vi.fn()} />);

    expect(screen.getByRole("group", { name: "Actions" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Navigation" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Sessions" })).toBeInTheDocument();
  });

  it("does not offer navigation to the removed Spec tab", () => {
    render(<CommandPalette isOpen onClose={vi.fn()} />);

    expect(
      screen.queryByRole("option", { name: /Switch to Spec Tab/i }),
    ).toBeNull();
  });

  it("opens the graph modal via the command 'Open graph view'", () => {
    const handler = vi.fn();
    window.addEventListener(OPEN_GRAPH_MODAL_EVENT, handler);

    render(<CommandPalette isOpen onClose={vi.fn()} />);
    fireEvent.click(screen.getByText(/open graph view/i));

    expect(handler).toHaveBeenCalled();
    window.removeEventListener(OPEN_GRAPH_MODAL_EVENT, handler);
  });

  it("opens the yaml export modal via the command 'Export YAML' when the pipeline has content", () => {
    const handler = vi.fn();
    window.addEventListener(OPEN_YAML_MODAL_EVENT, handler);
    useSessionStore.setState({
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

    render(<CommandPalette isOpen onClose={vi.fn()} />);
    fireEvent.click(screen.getByText(/export yaml/i));

    expect(handler).toHaveBeenCalled();
    window.removeEventListener(OPEN_YAML_MODAL_EVENT, handler);
  });

  // elspeth-bff8043d33 residual: the palette command was a leftover path
  // into the near-empty Export-YAML modal. Same hasCompositionContent gate
  // as ExportYamlButton — the command is withheld entirely (disabled
  // commands are filtered from the palette, matching Validate/Execute).
  it("withholds 'Export YAML' when the pipeline is empty", () => {
    render(<CommandPalette isOpen onClose={vi.fn()} />);

    expect(screen.queryByText(/export yaml/i)).toBeNull();
  });

  it("does not offer the old Graph or YAML tab commands", () => {
    render(<CommandPalette isOpen onClose={vi.fn()} />);

    expect(
      screen.queryByRole("option", { name: /Switch to Graph Tab/i }),
    ).toBeNull();
    expect(
      screen.queryByRole("option", { name: /Switch to YAML Tab/i }),
    ).toBeNull();
  });

  it("withholds Execute when structural validity passes but execution readiness is false", () => {
    useSessionStore.setState({ activeSessionId: "session-1" });
    executionStoreState.validationResult = {
      is_valid: true,
      readiness: { execution_ready: false },
    };

    render(<CommandPalette isOpen onClose={vi.fn()} />);

    expect(screen.queryByText("Execute Pipeline")).not.toBeInTheDocument();
  });

  it("dispatches run intent for an execution-ready pipeline", () => {
    const onRequestRun = vi.fn();
    const onClose = vi.fn();
    window.addEventListener(REQUEST_RUN_EVENT, onRequestRun);
    useSessionStore.setState({ activeSessionId: "session-1" });
    executionStoreState.validationResult = {
      is_valid: true,
      readiness: { execution_ready: true },
    };

    render(<CommandPalette isOpen onClose={onClose} />);
    fireEvent.click(screen.getByText("Execute Pipeline"));

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
    executionStoreState.validationResult = {
      is_valid: true,
      readiness: { execution_ready: true },
    };
    executionStoreState.isExecuting = isExecuting;
    executionStoreState.progress = progress;

    render(<CommandPalette isOpen onClose={vi.fn()} />);

    expect(screen.queryByText("Execute Pipeline")).not.toBeInTheDocument();
  });
});
