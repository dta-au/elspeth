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
import { makeComposition } from "@/test/composerFixtures";
import {
  OPEN_GRAPH_MODAL_EVENT,
  REQUEST_ARTIFACT_VIEW_EVENT,
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
}

function renderArtifactWorkspace(
  options: RenderArtifactWorkspaceOptions = {},
) {
  return render(
    <ComposerWorkspace
      authoring={<button type="button">Author control</button>}
      artifact={<ArtifactWorkspace />}
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

    await user.click(screen.getByRole("button", { name: "Focus Graph" }));

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
      fireEvent.click(screen.getByRole("button", { name: "Focus Graph" }));
      useSessionStore.setState({ activeSessionId: "session-2" });
    });

    expect(modalCount).toBe(0);
    window.removeEventListener(OPEN_GRAPH_MODAL_EVENT, listener);
  });

  it("suppresses a queued Graph modal after unmount", () => {
    const view = renderArtifactWorkspace();
    let modalCount = 0;
    const listener = () => {
      modalCount += 1;
    };
    window.addEventListener(OPEN_GRAPH_MODAL_EVENT, listener);

    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "Focus Graph" }));
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

    fireEvent.click(screen.getByRole("button", { name: "Focus Graph" }));
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
