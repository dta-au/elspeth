import { readFileSync } from "node:fs";
import { join } from "node:path";

import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  type ReactNode,
  Suspense,
  startTransition,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "@/api/client";
import { useExecutionStore } from "@/stores/executionStore";
import { useSessionStore } from "@/stores/sessionStore";
import { makeComposition, makeValidationResult } from "@/test/composerFixtures";
import { useHashRouter } from "@/hooks/useHashRouter";
import {
  OPEN_GRAPH_MODAL_EVENT,
  REQUEST_ARTIFACT_VIEW_EVENT,
  dispatchArtifactViewIntent,
  type RequestArtifactViewDetail,
} from "@/lib/composer-events";
import { ComposerWorkspace } from "./ComposerWorkspace";
import {
  ArtifactWorkspace,
  ArtifactWorkspaceSurface,
} from "./ArtifactWorkspace";
import { WorkspacePaneProvider } from "./WorkspacePaneContext";

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

interface RenderArtifactWorkspaceOptions {
  inspector?: ReactNode;
  runAvailable?: boolean;
}

function renderArtifactWorkspace(
  options: RenderArtifactWorkspaceOptions = {},
) {
  return render(
    <ComposerWorkspace
      authoring={<button type="button">Author control</button>}
      artifact={<ArtifactWorkspace runAvailable={options.runAvailable} />}
      inspector={
        options.inspector ?? <button type="button">Validation status</button>
      }
      actionBar={<button type="button">Run pipeline</button>}
    />,
  );
}

function CommitRequestProbe({
  watch,
  tab,
}: {
  watch: "session" | "availability";
  tab: "spec" | "yaml";
}): null {
  const sessionId = useSessionStore((state) => state.activeSessionId);
  const hasContent = useSessionStore(
    (state) => state.compositionState !== null,
  );
  const value = watch === "session" ? sessionId : hasContent;
  const previousRef = useRef(value);

  useLayoutEffect(() => {
    if (previousRef.current === value) return;
    previousRef.current = value;
    requestArtifact({ tab, focusMode: false, sessionId });
  }, [sessionId, tab, value]);
  return null;
}

function LayoutCleanupRequestProbe({
  onAfterDispatch,
}: {
  onAfterDispatch: () => void;
}): null {
  useLayoutEffect(
    () => () => {
      requestArtifact({
        tab: "spec",
        focusMode: false,
        sessionId: "session-1",
      });
      onAfterDispatch();
    },
    [onAfterDispatch],
  );
  return null;
}

function HashRouterProbe(): null {
  useHashRouter();
  return null;
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
      activeRunSessionId: null,
      progress: null,
      runs: [],
      lastRunOutcome: null,
      validationResult: null,
      wsDisconnected: false,
      loadRuns: vi.fn().mockResolvedValue("loaded"),
    } as never);
    vi.spyOn(api, "fetchYaml").mockResolvedValue({
      yaml: "sources:\n  input:\n    plugin: csv\n",
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
    window.history.replaceState(null, "", window.location.pathname);
  });

  it("fills the artifact body with one active-panel scroll owner", () => {
    const css = readFileSync(
      join(process.cwd(), "src/components/workspace/workspace.css"),
      "utf8",
    );

    expect(css).toMatch(
      /\.artifact-workspace\s*\{[^}]*display:\s*flex;[^}]*flex-direction:\s*column;[^}]*height:\s*100%;[^}]*min-height:\s*0;[^}]*overflow:\s*hidden;/s,
    );
    expect(css).toMatch(
      /\.artifact-workspace-toolbar\s*\{[^}]*flex:\s*0 0 auto;/s,
    );
    expect(css).toMatch(
      /\.artifact-workspace-panel\s*\{[^}]*flex:\s*1 1 auto;[^}]*min-height:\s*0;[^}]*overflow:\s*auto;/s,
    );
    expect(css).toMatch(
      /\.artifact-workspace-panel\[hidden\]\s*\{[^}]*display:\s*none;/s,
    );
  });

  it("gives artifact toolbar controls the compact 36px hit-target token", () => {
    const css = readFileSync(
      join(process.cwd(), "src/components/workspace/workspace.css"),
      "utf8",
    );

    expect(css).toMatch(
      /\.artifact-workspace-toolbar\s+button[^\{]*\{[^}]*min-height:\s*var\(--size-control-compact\);/s,
    );
  });

  it("lets the Run artifact panel own long-output scrolling", () => {
    const workspaceCss = readFileSync(join(process.cwd(), "src/components/workspace/workspace.css"), "utf8");
    expect(workspaceCss).toMatch(/\.artifact-workspace-panel\s+\.inline-run-results\s*\{[^}]*max-height:\s*none;[^}]*overflow:\s*visible;[^}]*margin:\s*0;/s);
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
    expect(screen.getAllByRole("tabpanel", { hidden: true })).toHaveLength(4);
    for (const tab of ["graph", "spec", "yaml", "run"] as const) {
      const tabElement = screen.getByRole("tab", {
        name: tab === "yaml" ? "YAML" : `${tab[0]!.toUpperCase()}${tab.slice(1)}`,
      });
      const controlledId = tabElement.getAttribute("aria-controls");
      expect(controlledId).toBe(`artifact-panel-${tab}`);
      const controlledPanel = document.getElementById(controlledId!);
      expect(controlledPanel).not.toBeNull();
      expect(controlledPanel).toHaveAttribute("aria-labelledby", tabElement.id);
      if (tab === "graph") {
        expect(controlledPanel).not.toHaveAttribute("hidden");
        expect(controlledPanel).not.toHaveAttribute("inert");
      } else {
        expect(controlledPanel).toHaveAttribute("hidden");
        expect(controlledPanel).toHaveAttribute("inert");
        expect(controlledPanel).toBeEmptyDOMElement();
      }
    }
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

  it("enables YAML for a pending pipeline proposal on an empty base state", () => {
    useSessionStore.setState({
      activeSessionId: "session-1",
      compositionState: null,
      compositionProposals: [
        {
          id: "proposal-1",
          session_id: "session-1",
          tool_call_id: "call-1",
          tool_name: "set_pipeline",
          status: "pending",
          summary: "Replace the pipeline.",
          rationale: "Requested by the current composer turn.",
          affects: ["graph", "validation", "yaml"],
          arguments_redacted_json: {},
          base_state_id: null,
          committed_state_id: null,
          audit_event_id: "event-1",
          created_at: "2026-08-12T00:00:00Z",
          updated_at: "2026-08-12T00:00:00Z",
        },
      ],
    });

    renderArtifactWorkspace();

    expect(screen.getByRole("tab", { name: "YAML" })).toBeEnabled();
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

  it("keeps a newer direct Run selection after an older deferred Spec hash loads", async () => {
    useSessionStore.setState({
      compositionStateLoaded: false,
      compositionState: null,
      sessions: [{ id: "session-1", title: "Session 1" }],
    } as never);
    window.history.replaceState(null, "", "#/session-1/spec");
    const user = userEvent.setup();
    renderArtifactWorkspace({ inspector: <HashRouterProbe /> });

    await user.click(screen.getByRole("tab", { name: "Run" }));
    act(() => {
      useSessionStore.setState({
        compositionStateLoaded: true,
        compositionState: makeComposition(1),
      });
    });
    await act(async () => Promise.resolve());

    expect(screen.getByRole("tab", { name: "Run" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: "Run" })).toHaveFocus();
  });

  it("keeps a real Export YAML intent after an older deferred Spec hash loads", async () => {
    useSessionStore.setState({
      compositionStateLoaded: false,
      compositionState: makeComposition(1),
      sessions: [{ id: "session-1", title: "Session 1" }],
    } as never);
    window.history.replaceState(null, "", "#/session-1/spec");
    renderArtifactWorkspace({ inspector: <HashRouterProbe /> });

    // The palette's Export YAML command and the Ctrl+Shift+Y shortcut both
    // publish this exact intent; dispatch it directly.
    act(() =>
      dispatchArtifactViewIntent({
        tab: "yaml",
        focusMode: false,
        sessionId: "session-1",
      }),
    );
    act(() => useSessionStore.setState({ compositionStateLoaded: true }));
    await act(async () => Promise.resolve());

    expect(screen.getByRole("tab", { name: "YAML" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: "YAML" })).toHaveFocus();
  });

  it("keeps a real Focus Graph intent after an older deferred Spec hash loads", async () => {
    useSessionStore.setState({
      compositionStateLoaded: false,
      compositionState: makeComposition(1),
      sessions: [{ id: "session-1", title: "Session 1" }],
    } as never);
    window.history.replaceState(null, "", "#/session-1/spec");
    const user = userEvent.setup();
    renderArtifactWorkspace({ inspector: <HashRouterProbe /> });

    await user.click(screen.getByRole("button", { name: "Focus graph" }));
    act(() => useSessionStore.setState({ compositionStateLoaded: true }));
    await act(async () => Promise.resolve());

    expect(screen.getByRole("tab", { name: "Graph" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
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
    const loadRuns = vi.fn().mockResolvedValue("loaded");
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

  it("routes a new-session request from the first committed layout effect", () => {
    useSessionStore.setState({ compositionState: makeComposition(1) });
    renderArtifactWorkspace({
      inspector: <CommitRequestProbe watch="session" tab="spec" />,
    });

    act(() => {
      useSessionStore.setState({
        activeSessionId: "session-2",
        compositionState: makeComposition(1, {
          id: "state-2",
          session_id: "session-2",
        }),
      });
    });

    expect(screen.getByRole("tab", { name: "Spec" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "Spec" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("uses newly committed availability when content becomes available", async () => {
    renderArtifactWorkspace({
      inspector: <CommitRequestProbe watch="availability" tab="yaml" />,
    });

    act(() => {
      useSessionStore.setState({ compositionState: makeComposition(1) });
    });

    expect(screen.getByRole("tab", { name: "YAML" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "YAML" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await screen.findByRole("button", { name: "Copy YAML to clipboard" });
    expect(
      document.querySelector(".artifact-workspace > [role='status']"),
    ).toBeEmptyDOMElement();
  });

  it("uses newly committed fallback when content becomes unavailable", async () => {
    useSessionStore.setState({ compositionState: makeComposition(1) });
    const user = userEvent.setup();
    renderArtifactWorkspace({
      inspector: <CommitRequestProbe watch="availability" tab="yaml" />,
    });
    await user.click(screen.getByRole("tab", { name: "Spec" }));

    act(() => {
      useSessionStore.setState({ compositionState: null });
    });

    expect(screen.getByRole("tab", { name: "Graph" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "YAML" })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent(
      "YAML is unavailable. Showing Graph.",
    );
  });

  const malformedRequests: Array<[string, Event]> = [
    ["plain Event", new Event(REQUEST_ARTIFACT_VIEW_EVENT)],
    [
      "missing detail",
      new CustomEvent(REQUEST_ARTIFACT_VIEW_EVENT),
    ],
    [
      "null detail",
      new CustomEvent(REQUEST_ARTIFACT_VIEW_EVENT, { detail: null }),
    ],
    [
      "array detail",
      new CustomEvent(REQUEST_ARTIFACT_VIEW_EVENT, {
        detail: ["spec", false, "session-1"],
      }),
    ],
    [
      "prototype-bearing detail",
      new CustomEvent(REQUEST_ARTIFACT_VIEW_EVENT, {
        detail: Object.assign(Object.create({ inherited: true }), {
          tab: "spec",
          focusMode: false,
          sessionId: "session-1",
        }),
      }),
    ],
    [
      "missing tab",
      new CustomEvent(REQUEST_ARTIFACT_VIEW_EVENT, {
        detail: { focusMode: false, sessionId: "session-1" },
      }),
    ],
    [
      "missing focusMode",
      new CustomEvent(REQUEST_ARTIFACT_VIEW_EVENT, {
        detail: { tab: "spec", sessionId: "session-1" },
      }),
    ],
    [
      "missing sessionId",
      new CustomEvent(REQUEST_ARTIFACT_VIEW_EVENT, {
        detail: { tab: "spec", focusMode: false },
      }),
    ],
    [
      "extra key",
      new CustomEvent(REQUEST_ARTIFACT_VIEW_EVENT, {
        detail: {
          tab: "spec",
          focusMode: false,
          sessionId: "session-1",
          extra: true,
        },
      }),
    ],
    [
      "extra symbol key",
      new CustomEvent(REQUEST_ARTIFACT_VIEW_EVENT, {
        detail: Object.assign(
          {
            tab: "spec",
            focusMode: false,
            sessionId: "session-1",
          },
          { [Symbol("extra")]: true },
        ),
      }),
    ],
    [
      "accessor property",
      new CustomEvent(REQUEST_ARTIFACT_VIEW_EVENT, {
        detail: Object.defineProperties(
          {},
          {
            tab: {
              enumerable: true,
              get: () => {
                throw new Error("ambient accessor must not run");
              },
            },
            focusMode: { enumerable: true, value: false },
            sessionId: { enumerable: true, value: "session-1" },
          },
        ),
      }),
    ],
    [
      "throwing proxy",
      new CustomEvent(REQUEST_ARTIFACT_VIEW_EVENT, {
        detail: new Proxy(
          {},
          {
            getPrototypeOf: () => {
              throw new Error("ambient proxy must not escape admission");
            },
          },
        ),
      }),
    ],
    [
      "unknown tab",
      new CustomEvent(REQUEST_ARTIFACT_VIEW_EVENT, {
        detail: {
          tab: "audit",
          focusMode: false,
          sessionId: "session-1",
        },
      }),
    ],
    [
      "non-boolean focusMode",
      new CustomEvent(REQUEST_ARTIFACT_VIEW_EVENT, {
        detail: {
          tab: "spec",
          focusMode: "false",
          sessionId: "session-1",
        },
      }),
    ],
    [
      "non-string sessionId",
      new CustomEvent(REQUEST_ARTIFACT_VIEW_EVENT, {
        detail: { tab: "spec", focusMode: false, sessionId: 1 },
      }),
    ],
  ];

  it.each(malformedRequests)(
    "ignores malformed ambient artifact request: %s",
    (_label, event) => {
      useSessionStore.setState({ compositionState: makeComposition(1) });
      renderArtifactWorkspace();
      const authorControl = screen.getByRole("button", {
        name: "Author control",
      });
      authorControl.focus();
      let modalCount = 0;
      const listener = () => {
        modalCount += 1;
      };
      window.addEventListener(OPEN_GRAPH_MODAL_EVENT, listener);

      expect(() => {
        act(() => {
          window.dispatchEvent(event);
        });
      }).not.toThrow();

      expect(authorControl).toHaveFocus();
      expect(screen.getByRole("tab", { name: "Graph" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      expect(screen.getByRole("status")).toBeEmptyDOMElement();
      expect(modalCount).toBe(0);
      window.removeEventListener(OPEN_GRAPH_MODAL_EVENT, listener);
    },
  );

  it("admits an exact null-prototype request detail into an owned request", () => {
    useSessionStore.setState({ compositionState: makeComposition(1) });
    renderArtifactWorkspace();
    const detail = Object.assign(Object.create(null), {
      tab: "spec",
      focusMode: false,
      sessionId: "session-1",
    });

    act(() => {
      window.dispatchEvent(
        new CustomEvent(REQUEST_ARTIFACT_VIEW_EVENT, { detail }),
      );
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

  it("moves focused selected-tab focus to Graph on a content-preserving session reset", async () => {
    useSessionStore.setState({ compositionState: makeComposition(1) });
    const user = userEvent.setup();
    renderArtifactWorkspace();
    await user.click(screen.getByRole("tab", { name: "YAML" }));
    expect(screen.getByRole("tab", { name: "YAML" })).toHaveFocus();

    act(() => {
      useSessionStore.setState({
        activeSessionId: "session-2",
        compositionState: makeComposition(1, {
          id: "state-2",
          session_id: "session-2",
        }),
      });
    });

    expect(screen.getByRole("tab", { name: "Graph" })).toHaveFocus();
    expect(
      document.querySelector(".artifact-workspace > [role='status']"),
    ).toBeEmptyDOMElement();
  });

  it("moves focused panel content to Graph on a content-preserving session reset", async () => {
    useSessionStore.setState({ compositionState: makeComposition(1) });
    const user = userEvent.setup();
    renderArtifactWorkspace();
    await user.click(screen.getByRole("tab", { name: "YAML" }));
    const copyYaml = await screen.findByRole("button", {
      name: "Copy YAML to clipboard",
    });
    copyYaml.focus();
    expect(copyYaml).toHaveFocus();

    act(() => {
      useSessionStore.setState({
        activeSessionId: "session-2",
        compositionState: makeComposition(1, {
          id: "state-2",
          session_id: "session-2",
        }),
      });
    });

    expect(screen.getByRole("tab", { name: "Graph" })).toHaveFocus();
    expect(
      document.querySelector(".artifact-workspace > [role='status']"),
    ).toBeEmptyDOMElement();
  });

  it("does not steal unrelated focus on a content-preserving session reset", () => {
    useSessionStore.setState({ compositionState: makeComposition(1) });
    renderArtifactWorkspace();
    const authorControl = screen.getByRole("button", { name: "Author control" });
    authorControl.focus();

    act(() => {
      useSessionStore.setState({
        activeSessionId: "session-2",
        compositionState: makeComposition(1, {
          id: "state-2",
          session_id: "session-2",
        }),
      });
    });

    expect(authorControl).toHaveFocus();
  });

  it("focuses Graph before queueing exactly one full-screen modal request", async () => {
    useSessionStore.setState({ compositionState: makeComposition(1) });
    const user = userEvent.setup();
    renderArtifactWorkspace();
    await user.click(screen.getByRole("tab", { name: "Spec" }));
    const observations: Array<{ focused: Element | null; selected: string | null }> = [];
    const listener = () => {
      const graph = screen.getByRole("tab", { name: "Graph" });
      observations.push({
        focused: document.activeElement,
        selected: graph.getAttribute("aria-selected"),
      });
    };
    window.addEventListener(OPEN_GRAPH_MODAL_EVENT, listener);

    await user.click(screen.getByRole("button", { name: "Focus graph" }));

    await waitFor(() => expect(observations).toHaveLength(1));
    await Promise.resolve();
    expect(observations).toHaveLength(1);
    expect(observations[0]).toEqual({
      focused: screen.getByRole("tab", { name: "Graph" }),
      selected: "true",
    });
    expect(screen.getByRole("tab", { name: "Graph" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    window.removeEventListener(OPEN_GRAPH_MODAL_EVENT, listener);
  });

  it("suppresses a queued Graph modal after a committed session switch", () => {
    renderArtifactWorkspace();
    let modalCount = 0;
    const listener = () => {
      modalCount += 1;
    };
    window.addEventListener(OPEN_GRAPH_MODAL_EVENT, listener);

    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "Focus graph" }));
      useSessionStore.setState({ activeSessionId: "session-2" });
    });

    expect(modalCount).toBe(0);
    window.removeEventListener(OPEN_GRAPH_MODAL_EVENT, listener);
  });

  it("suppresses an older queued Focus Graph modal after a newer direct Run selection", async () => {
    useSessionStore.setState({ compositionState: makeComposition(1) });
    renderArtifactWorkspace();
    const onOpenGraph = vi.fn();
    window.addEventListener(OPEN_GRAPH_MODAL_EVENT, onOpenGraph);

    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "Focus graph" }));
      fireEvent.click(screen.getByRole("tab", { name: "Run" }));
    });
    await act(async () => Promise.resolve());

    expect(onOpenGraph).not.toHaveBeenCalled();
    expect(screen.getByRole("tab", { name: "Run" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: "Run" })).toHaveFocus();
    window.removeEventListener(OPEN_GRAPH_MODAL_EVENT, onOpenGraph);
  });

  it("suppresses a queued Graph modal after unmount", () => {
    const view = renderArtifactWorkspace();
    let modalCount = 0;
    const listener = () => {
      modalCount += 1;
    };
    window.addEventListener(OPEN_GRAPH_MODAL_EVENT, listener);

    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "Focus graph" }));
      view.unmount();
    });

    expect(modalCount).toBe(0);
    window.removeEventListener(OPEN_GRAPH_MODAL_EVENT, listener);
  });

  it("ignores ambient requests between layout and passive unmount cleanup", () => {
    const outside = document.createElement("button");
    document.body.append(outside);
    let focusedDuringCleanup: Element | null = null;
    const selectArtifactTab = vi.fn();
    const recordFocus = () => {
      focusedDuringCleanup = document.activeElement;
    };
    const view = render(
      <WorkspacePaneProvider
        paneState={
          {
            authoringCollapsed: false,
            availableArtifactTabs: ["graph", "spec", "yaml", "run"],
            activeArtifactTab: "graph",
            activeInspectorTab: null,
            inspectorOpen: false,
            resizeTransient: vi.fn(),
            commitResize: vi.fn(),
            setAuthoringCollapsed: vi.fn(),
            selectArtifactTab,
            openInspector: vi.fn(),
            closeInspector: vi.fn(),
          } as never
        }
      >
        <ArtifactWorkspaceSurface activeSessionId="session-1" />
        <LayoutCleanupRequestProbe onAfterDispatch={recordFocus} />
      </WorkspacePaneProvider>,
    );
    let workspaceFocusCount = 0;
    screen.getByRole("tab", { name: "Graph" }).addEventListener("focus", () => {
      workspaceFocusCount += 1;
    });
    outside.focus();

    view.unmount();

    expect(focusedDuringCleanup).toBe(outside);
    expect(workspaceFocusCount).toBe(0);
    expect(selectArtifactTab).not.toHaveBeenCalled();
    outside.remove();
  });

  it("does not let a suspended speculative session render poison a queued modal", async () => {
    const never = new Promise<void>(() => {});
    function SuspendAfterArtifact({
      sessionId,
    }: {
      sessionId: string;
    }): null {
      if (sessionId === "session-2") throw never;
      return null;
    }
    function SpeculativeSessionHarness(): JSX.Element {
      const [sessionId, setSessionId] = useState("session-1");
      return (
        <Suspense fallback={<div>Speculative fallback</div>}>
          <ComposerWorkspace
            authoring={
              <button
                type="button"
                onClick={() => {
                  startTransition(() => setSessionId("session-2"));
                }}
              >
                Switch speculative session
              </button>
            }
            artifact={
              <ArtifactWorkspaceSurface activeSessionId={sessionId} />
            }
            inspector={<button type="button">Validation status</button>}
            actionBar={<button type="button">Run pipeline</button>}
          />
          <SuspendAfterArtifact sessionId={sessionId} />
        </Suspense>
      );
    }
    const view = render(<SpeculativeSessionHarness />);
    let modalCount = 0;
    const listener = () => {
      modalCount += 1;
    };
    window.addEventListener(OPEN_GRAPH_MODAL_EVENT, listener);

    fireEvent.click(screen.getByRole("button", { name: "Focus graph" }));
    fireEvent.click(
      screen.getByRole("button", { name: "Switch speculative session" }),
    );
    await act(async () => Promise.resolve());

    expect(screen.queryByText("Speculative fallback")).not.toBeInTheDocument();
    expect(modalCount).toBe(1);
    window.removeEventListener(OPEN_GRAPH_MODAL_EVENT, listener);
    view.unmount();
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
    expect(screen.getByRole("button", { name: "Focus graph" })).toBeEnabled();
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

  // ── Run-tab outcome badge (elspeth-3a7b7c7b37) ────────────────────────────

  it("badges the Run tab for an unacknowledged outcome and clears it on selecting Run", async () => {
    useSessionStore.setState({ compositionState: makeComposition(1) });
    useExecutionStore.setState({
      lastRunOutcome: { runId: "run-1", status: "failed", sessionId: "session-1" },
    } as never);
    renderArtifactWorkspace();

    // Accessible name stays exactly "Run": the badge is aria-hidden markup.
    const runTab = screen.getByRole("tab", { name: "Run" });
    const badge = runTab.querySelector(".artifact-tab-badge");
    expect(badge).not.toBeNull();
    expect(badge).toHaveAttribute("aria-hidden", "true");
    expect(badge).toHaveAttribute("data-tone", "error");

    fireEvent.click(runTab);
    await act(async () => Promise.resolve());

    // Selecting Run acknowledges the outcome (ProgressView owns the
    // announcement there) and retires the badge.
    expect(useExecutionStore.getState().lastRunOutcome).toBeNull();
    expect(runTab.querySelector(".artifact-tab-badge")).toBeNull();
  });

  it("maps completed outcomes to the success tone", () => {
    useSessionStore.setState({ compositionState: makeComposition(1) });
    useExecutionStore.setState({
      lastRunOutcome: {
        runId: "run-1",
        status: "completed",
        sessionId: "session-1",
      },
    } as never);
    renderArtifactWorkspace();

    expect(
      screen
        .getByRole("tab", { name: "Run" })
        .querySelector(".artifact-tab-badge"),
    ).toHaveAttribute("data-tone", "success");
  });

  // `empty` is a terminal outcome the notice gives its own info tone, but the
  // badge deliberately keeps it NEUTRAL: an empty run is not a fault, and a
  // fourth colour on a 7px dot would add a distinction the user cannot read
  // (see the decorative contract in workspace.css). Pinned so the mapping is
  // a decision rather than the untested residue of badgeTone's default arm —
  // and so the tone cannot silently drift onto warning/error, which WOULD
  // claim a fault occurred.
  it("keeps an empty-run outcome on the neutral tone rather than claiming a fault", () => {
    useSessionStore.setState({ compositionState: makeComposition(1) });
    useExecutionStore.setState({
      lastRunOutcome: {
        runId: "run-1",
        status: "empty",
        sessionId: "session-1",
      },
    } as never);
    renderArtifactWorkspace();

    const badge = screen
      .getByRole("tab", { name: "Run" })
      .querySelector(".artifact-tab-badge");
    // Badged at all — an empty run is still an unacknowledged outcome the
    // operator has not seen.
    expect(badge).not.toBeNull();
    expect(badge).toHaveAttribute("data-tone", "neutral");
  });

  it("acknowledges an outcome immediately while Run is already the active tab", async () => {
    useSessionStore.setState({ compositionState: makeComposition(1) });
    renderArtifactWorkspace();
    fireEvent.click(screen.getByRole("tab", { name: "Run" }));
    await act(async () => Promise.resolve());

    // Terminal outcome lands while the Run panel (ProgressView's single
    // announcement region) is mounted → suppressed before paint so the
    // App-level notice never double-announces.
    act(() => {
      useExecutionStore.setState({
        lastRunOutcome: {
          runId: "run-1",
          status: "completed",
          sessionId: "session-1",
        },
      } as never);
    });

    expect(useExecutionStore.getState().lastRunOutcome).toBeNull();
    expect(
      screen.getByRole("tab", { name: "Run" }).querySelector(".artifact-tab-badge"),
    ).toBeNull();
  });

  it("shows a live pulse badge for an attached in-flight run without starting Run polling", () => {
    vi.useFakeTimers();
    const loadRuns = vi.fn().mockResolvedValue("loaded");
    useSessionStore.setState({ compositionState: makeComposition(1) });
    useExecutionStore.setState({
      activeRunId: "run-live",
      progress: { status: "running" },
      runs: [],
      loadRuns,
    } as never);
    renderArtifactWorkspace();

    const badge = screen
      .getByRole("tab", { name: "Run" })
      .querySelector(".artifact-tab-badge");
    expect(badge).toHaveAttribute("data-tone", "live");
    expect(badge).toHaveAttribute("aria-hidden", "true");

    // Extends the polling pin below: the badge is a pure store reader — no
    // loadRuns traffic while the Run tab stays inactive.
    act(() => vi.advanceTimersByTime(6000));
    expect(loadRuns).not.toHaveBeenCalled();
  });

  // A dropped WebSocket freezes progress.status at "running" for a run that
  // may already be finished server-side, so the pulse would animate forever
  // and read as live evidence the tab no longer has. It degrades to the
  // static neutral dot instead.
  //
  // What this assertion is and is NOT. data-tone is a DECORATIVE proxy: the
  // badge is aria-hidden, and "live" differs from "neutral" only by an
  // animation that workspace.css disables under prefers-reduced-motion — so a
  // reduced-motion user cannot perceive this flip at all. The pin is here
  // because the tone attribute is the only observable handle on the
  // degradation decision in jsdom, not because the tone is the user-facing
  // contract. The observable contracts are elsewhere and tested elsewhere:
  // ProgressView's .progress-ws-banner states the dropped connection in
  // words, and the terminal outcome is carried by the RunOutcomeNotice toast.
  // Do not promote this into a claim about what the user perceives; see the
  // decorative contract in workspace.css.
  it("degrades the attached-run badge from the live pulse when the WebSocket drops", () => {
    useSessionStore.setState({ compositionState: makeComposition(1) });
    useExecutionStore.setState({
      activeRunId: "run-live",
      progress: { status: "running" },
      runs: [],
      wsDisconnected: true,
    } as never);
    renderArtifactWorkspace();

    const badge = screen
      .getByRole("tab", { name: "Run" })
      .querySelector(".artifact-tab-badge");
    // Still badged — the tab still owns a run — but not as live.
    expect(badge).not.toBeNull();
    expect(badge).toHaveAttribute("data-tone", "neutral");
  });

  // Acknowledgement is a claim that ProgressView's live region has taken over
  // the announcement. That claim only holds where ProgressView can actually
  // be seen: ComposerWorkspace keeps this surface MOUNTED but hidden/inert in
  // compose view on narrow viewports, and swallowing the outcome there would
  // reinstate the off-Run-tab silence the notice exists to fix.
  describe("outcome acknowledgement requires a VISIBLE artifact pane", () => {
    function renderRunTabSurface(artifactVisible: boolean) {
      return render(
        <WorkspacePaneProvider
          artifactVisible={artifactVisible}
          paneState={
            {
              authoringCollapsed: false,
              availableArtifactTabs: ["graph", "spec", "yaml", "run"],
              activeArtifactTab: "run",
              activeInspectorTab: null,
              inspectorOpen: false,
              resizeTransient: vi.fn(),
              commitResize: vi.fn(),
              setAuthoringCollapsed: vi.fn(),
              selectArtifactTab: vi.fn(),
              openInspector: vi.fn(),
              closeInspector: vi.fn(),
            } as never
          }
        >
          <ArtifactWorkspaceSurface activeSessionId="session-1" />
        </WorkspacePaneProvider>,
      );
    }

    const outcome = {
      runId: "run-1",
      status: "completed_with_failures" as const,
      sessionId: "session-1",
    };

    it("keeps the outcome unacknowledged while the Run tab is mounted but hidden", () => {
      useExecutionStore.setState({ lastRunOutcome: outcome } as never);

      renderRunTabSurface(false);

      // The outcome survives for the always-mounted RunOutcomeNotice, and the
      // badge keeps carrying it on the tab the operator has not yet reached.
      expect(useExecutionStore.getState().lastRunOutcome).toEqual(outcome);
      expect(
        screen
          .getByRole("tab", { name: "Run" })
          .querySelector(".artifact-tab-badge"),
      ).toHaveAttribute("data-tone", "warning");
    });

    it("acknowledges the outcome once the Run tab is visible", () => {
      useExecutionStore.setState({ lastRunOutcome: outcome } as never);

      renderRunTabSurface(true);

      expect(useExecutionStore.getState().lastRunOutcome).toBeNull();
      expect(
        screen
          .getByRole("tab", { name: "Run" })
          .querySelector(".artifact-tab-badge"),
      ).toBeNull();
    });
  });

  it("forwards runAvailable to the Run panel's empty-state affordance", async () => {
    useSessionStore.setState({ compositionState: makeComposition(1) });
    // The affordance needs BOTH facts: runAvailable (the REQUEST_RUN_EVENT
    // owner is mounted) threaded down from here, and execution readiness
    // read by InlineRunResults itself.
    useExecutionStore.setState({
      validationResult: makeValidationResult(),
    } as never);
    const user = userEvent.setup();
    renderArtifactWorkspace({ runAvailable: true });

    await user.click(screen.getByRole("tab", { name: "Run" }));

    const runPanel = screen.getByRole("tabpanel", { name: "Run" });
    expect(
      await within(runPanel).findByRole("button", { name: "Run pipeline" }),
    ).toBeInTheDocument();
    expect(runPanel).toHaveTextContent(
      "No runs yet. Run the pipeline to see its results here.",
    );
  });

  // The OMITTED-prop arm, pinned separately from the explicit-false arm.
  // TutorialGuidedShell does not pass runAvailable at all, so the tutorial's
  // freedom from a dead Run control rests entirely on this default — which
  // means a future `runAvailable = true` default would silently enable it
  // there while every tutorial test still passed.
  it("defaults runAvailable to off, so an omitted prop offers no Run affordance", async () => {
    useSessionStore.setState({ compositionState: makeComposition(1) });
    useExecutionStore.setState({
      validationResult: makeValidationResult(),
    } as never);
    const user = userEvent.setup();
    renderArtifactWorkspace();

    await user.click(screen.getByRole("tab", { name: "Run" }));

    const runPanel = screen.getByRole("tabpanel", { name: "Run" });
    await within(runPanel).findByText("No runs yet.");
    expect(
      within(runPanel).queryByRole("button", { name: "Run pipeline" }),
    ).not.toBeInTheDocument();
  });

  it("owns one Run polling stream only while Run is active", async () => {
    vi.useFakeTimers();
    const loadRuns = vi.fn().mockResolvedValue("loaded");
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

    fireEvent.click(screen.getByRole("tab", { name: "Graph" }));
    act(() => vi.advanceTimersByTime(6000));
    expect(loadRuns).toHaveBeenCalledTimes(2);

    fireEvent.click(screen.getByRole("tab", { name: "Run" }));
    await act(async () => Promise.resolve());
    expect(loadRuns).toHaveBeenCalledTimes(3);

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
    expect(loadRuns).toHaveBeenCalledTimes(3);
  });
});
