import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "@/api/client";
import { useExecutionStore } from "@/stores/executionStore";
import { useSessionStore } from "@/stores/sessionStore";
import { makeComposition } from "@/test/composerFixtures";
import {
  OPEN_GRAPH_MODAL_EVENT,
  OPEN_YAML_MODAL_EVENT,
  REQUEST_ARTIFACT_VIEW_EVENT,
  type RequestArtifactViewDetail,
} from "@/lib/composer-events";
import { ComposerWorkspace } from "./ComposerWorkspace";
import { ArtifactWorkspace } from "./ArtifactWorkspace";

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

class PassiveResizeObserver implements ResizeObserver {
  constructor(_callback: ResizeObserverCallback) {}
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

function renderArtifactWorkspace() {
  return render(
    <ComposerWorkspace
      authoring={<button type="button">Author control</button>}
      artifact={<ArtifactWorkspace />}
      inspector={<button type="button">Validation status</button>}
      actionBar={<button type="button">Run pipeline</button>}
    />,
  );
}

function requestArtifact(detail: RequestArtifactViewDetail): void {
  window.dispatchEvent(
    new CustomEvent<RequestArtifactViewDetail>(REQUEST_ARTIFACT_VIEW_EVENT, {
      detail,
    }),
  );
}

describe("ArtifactWorkspace", () => {
  beforeEach(() => {
    vi.stubGlobal("ResizeObserver", PassiveResizeObserver);
    localStorage.clear();
    useSessionStore.setState({
      activeSessionId: "session-1",
      compositionState: null,
      compositionProposals: [],
      proposalActionPendingIds: [],
      staleProposalIds: [],
      exportedYamlBlobBinding: null,
      selectedNodeId: null,
    });
    useExecutionStore.setState({
      activeRunId: null,
      progress: null,
      runs: [],
      loadRuns: vi.fn().mockResolvedValue(undefined),
    } as never);
    vi.spyOn(api, "fetchYaml").mockResolvedValue({
      yaml: "sources:\n  input:\n    plugin: csv\n",
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("starts on Graph with one complete named tab relationship and the real empty state", () => {
    renderArtifactWorkspace();

    const tablist = screen.getByRole("tablist", {
      name: "Pipeline artifacts",
    });
    const graph = within(tablist).getByRole("tab", { name: "Graph" });
    expect(graph).toHaveAttribute("id", "artifact-tab-graph");
    expect(graph).toHaveAttribute("aria-selected", "true");
    expect(graph).toHaveAttribute("aria-controls", "artifact-panel-graph");
    expect(graph).toHaveAttribute("tabindex", "0");
    const panel = screen.getByRole("tabpanel", { name: "Graph" });
    expect(panel).toHaveAttribute("id", "artifact-panel-graph");
    expect(panel).toHaveAttribute("aria-labelledby", "artifact-tab-graph");
    expect(screen.getAllByRole("tabpanel")).toHaveLength(1);
    expect(panel).toHaveTextContent(
      "No pipeline to visualise. Start a conversation to build one.",
    );
  });

  it("disables Spec and YAML without composition content but keeps Graph and Run available", () => {
    renderArtifactWorkspace();

    expect(screen.getByRole("tab", { name: "Graph" })).toBeEnabled();
    expect(screen.getByRole("tab", { name: "Spec" })).toBeDisabled();
    expect(screen.getByRole("tab", { name: "YAML" })).toBeDisabled();
    expect(screen.getByRole("tab", { name: "Run" })).toBeEnabled();
  });

  it("selects on focus while clicking and roving with wrap, Home, and End", async () => {
    useSessionStore.setState({ compositionState: makeComposition(1) });
    const user = userEvent.setup();
    renderArtifactWorkspace();
    const spec = screen.getByRole("tab", { name: "Spec" });

    await user.click(spec);
    expect(spec).toHaveFocus();
    expect(spec).toHaveAttribute("aria-selected", "true");
    expect(spec).toHaveAttribute("aria-controls", "artifact-panel-spec");
    expect(screen.getByRole("tabpanel", { name: "Spec" })).toHaveAttribute(
      "aria-labelledby",
      spec.id,
    );

    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "YAML" })).toHaveFocus();
    expect(screen.getByRole("tabpanel", { name: "YAML" })).toBeInTheDocument();
    await user.keyboard("{End}");
    expect(screen.getByRole("tab", { name: "Run" })).toHaveFocus();
    expect(screen.getByRole("tabpanel", { name: "Run" })).toHaveTextContent(
      "No runs yet.",
    );
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Graph" })).toHaveFocus();
    await user.keyboard("{ArrowLeft}");
    expect(screen.getByRole("tab", { name: "Run" })).toHaveFocus();
    await user.keyboard("{Home}");
    expect(screen.getByRole("tab", { name: "Graph" })).toHaveFocus();
  });

  it("skips disabled tabs while roving in an empty composition", async () => {
    const user = userEvent.setup();
    renderArtifactWorkspace();
    screen.getByRole("tab", { name: "Graph" }).focus();

    await user.keyboard("{ArrowRight}");

    expect(screen.getByRole("tab", { name: "Run" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "Run" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("mounts only the active body and gives YAML and Run one request owner", async () => {
    useSessionStore.setState({ compositionState: makeComposition(1) });
    const loadRuns = vi.fn().mockResolvedValue(undefined);
    useExecutionStore.setState({ loadRuns } as never);
    const user = userEvent.setup();
    renderArtifactWorkspace();

    expect(api.fetchYaml).not.toHaveBeenCalled();
    expect(loadRuns).not.toHaveBeenCalled();
    await user.click(screen.getByRole("tab", { name: "YAML" }));
    await screen.findByRole("button", { name: "Copy YAML to clipboard" });
    expect(api.fetchYaml).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("select_columns")).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Run" }));
    await waitFor(() => expect(loadRuns).toHaveBeenCalledTimes(1));
    expect(
      screen.queryByRole("button", { name: "Copy YAML to clipboard" }),
    ).not.toBeInTheDocument();
    expect(screen.getAllByRole("tabpanel")).toHaveLength(1);
  });

  it("filters requests by session and selects and focuses an available requested tab", () => {
    useSessionStore.setState({ compositionState: makeComposition(1) });
    renderArtifactWorkspace();

    act(() => {
      requestArtifact({
        tab: "spec",
        focusMode: false,
        sessionId: "another-session",
      });
    });
    expect(screen.getByRole("tab", { name: "Graph" })).toHaveAttribute(
      "aria-selected",
      "true",
    );

    act(() => {
      requestArtifact({
        tab: "spec",
        focusMode: false,
        sessionId: "session-1",
      });
    });
    expect(screen.getByRole("tab", { name: "Spec" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "Spec" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("falls an unavailable request back to focused Graph with an accessible announcement", () => {
    renderArtifactWorkspace();

    act(() => {
      requestArtifact({
        tab: "yaml",
        focusMode: true,
        sessionId: "session-1",
      });
    });

    expect(screen.getByRole("tab", { name: "Graph" })).toHaveFocus();
    expect(screen.getByRole("status")).toHaveTextContent(
      "YAML is unavailable. Showing Graph.",
    );
  });

  it("immediately repairs active-tab focus when Spec becomes unavailable", async () => {
    useSessionStore.setState({ compositionState: makeComposition(1) });
    const user = userEvent.setup();
    renderArtifactWorkspace();
    await user.click(screen.getByRole("tab", { name: "Spec" }));

    act(() => {
      useSessionStore.setState({ compositionState: null });
    });

    expect(screen.getByRole("tab", { name: "Graph" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "Graph" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Spec is unavailable. Showing Graph.",
    );
  });

  it("focuses Graph before queueing exactly one full-screen modal request", async () => {
    useSessionStore.setState({ compositionState: makeComposition(1) });
    const user = userEvent.setup();
    renderArtifactWorkspace();
    await user.click(screen.getByRole("tab", { name: "Spec" }));
    const observations: Element[] = [];
    const listener = () => observations.push(document.activeElement!);
    window.addEventListener(OPEN_GRAPH_MODAL_EVENT, listener);

    await user.click(screen.getByRole("button", { name: "Focus Graph" }));

    await waitFor(() => expect(observations).toHaveLength(1));
    await Promise.resolve();
    expect(observations).toHaveLength(1);
    expect(observations[0]).toBe(screen.getByRole("tab", { name: "Graph" }));
    expect(screen.getByRole("tab", { name: "Graph" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    window.removeEventListener(OPEN_GRAPH_MODAL_EVENT, listener);
  });

  it("applies graph focus-mode requests in selection-focus-modal order", async () => {
    useSessionStore.setState({ compositionState: makeComposition(1) });
    renderArtifactWorkspace();
    const observations: Element[] = [];
    const listener = () => observations.push(document.activeElement!);
    window.addEventListener(OPEN_GRAPH_MODAL_EVENT, listener);

    act(() => {
      requestArtifact({
        tab: "graph",
        focusMode: true,
        sessionId: "session-1",
      });
    });

    await waitFor(() => expect(observations).toHaveLength(1));
    await Promise.resolve();
    expect(observations).toHaveLength(1);
    expect(observations[0]).toBe(screen.getByRole("tab", { name: "Graph" }));
    window.removeEventListener(OPEN_GRAPH_MODAL_EVENT, listener);
  });

  it("keeps the legacy YAML event as a persistent-tab compatibility request", () => {
    useSessionStore.setState({ compositionState: makeComposition(1) });
    renderArtifactWorkspace();

    act(() => {
      window.dispatchEvent(new Event(OPEN_YAML_MODAL_EVENT));
    });

    expect(screen.getByRole("tab", { name: "YAML" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "YAML" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tabpanel", { name: "YAML" })).toBeInTheDocument();
  });

  it("keeps outer controls usable and resets a failed body by tab and session", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    const preventExpectedWindowError = (event: ErrorEvent) => {
      event.preventDefault();
    };
    window.addEventListener("error", preventExpectedWindowError);
    useSessionStore.setState({
      compositionState: makeComposition(1, {
        sources: {
          input: { plugin: "csv", options: { unsupported: 1n } },
        },
        nodes: [],
      }),
    });
    const user = userEvent.setup();
    renderArtifactWorkspace();

    await user.click(screen.getByRole("tab", { name: "Spec" }));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Spec artifact encountered an error",
    );
    expect(screen.getByRole("tablist", { name: "Pipeline artifacts" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Focus Graph" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Validation status" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Run pipeline" })).toBeEnabled();

    await user.click(screen.getByRole("tab", { name: "Graph" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Spec" }));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Spec artifact encountered an error",
    );

    act(() => {
      useSessionStore.setState({
        activeSessionId: "session-2",
        compositionState: makeComposition(1, {
          id: "state-2",
          session_id: "session-2",
        }),
      });
    });
    expect(screen.getByRole("tab", { name: "Graph" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Spec" }));
    expect(screen.getByRole("heading", { name: "demo" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    window.removeEventListener("error", preventExpectedWindowError);
    consoleError.mockRestore();
  });

  it("unmounts Run polling on session switch", async () => {
    vi.useFakeTimers();
    const loadRuns = vi.fn().mockResolvedValue(undefined);
    useExecutionStore.setState({
      loadRuns,
      runs: [{ id: "live", session_id: "session-1", status: "running" }],
    } as never);
    useSessionStore.setState({ compositionState: makeComposition(1) });
    renderArtifactWorkspace();

    fireEvent.click(screen.getByRole("tab", { name: "Run" }));
    await act(async () => Promise.resolve());
    expect(loadRuns).toHaveBeenCalledTimes(1);
    act(() => vi.advanceTimersByTime(3000));
    expect(loadRuns).toHaveBeenCalledTimes(2);

    act(() => {
      useSessionStore.setState({
        activeSessionId: "session-2",
        compositionState: makeComposition(1, {
          id: "state-2",
          session_id: "session-2",
        }),
      });
    });
    expect(screen.getByRole("tab", { name: "Graph" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    act(() => vi.advanceTimersByTime(6000));
    expect(loadRuns).toHaveBeenCalledTimes(2);
  });
});
