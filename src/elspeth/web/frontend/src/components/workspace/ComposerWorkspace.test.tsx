import {
  act,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  type ReactNode,
  useEffect,
  useLayoutEffect,
  useRef,
} from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSessionStore } from "@/stores/sessionStore";
import { ComposerWorkspace } from "./ComposerWorkspace";
import { useWorkspacePaneController } from "./WorkspacePaneContext";
import { WORKSPACE_LAYOUT_STORAGE_KEY } from "./useWorkspacePaneState";

class ControllableResizeObserver implements ResizeObserver {
  static instances: ControllableResizeObserver[] = [];

  readonly callback: ResizeObserverCallback;
  readonly observed = new Set<Element>();

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
    ControllableResizeObserver.instances.push(this);
  }

  observe(target: Element): void {
    this.observed.add(target);
  }

  unobserve(target: Element): void {
    this.observed.delete(target);
  }

  disconnect(): void {
    this.observed.clear();
  }

  static emit(width: number): void {
    for (const instance of ControllableResizeObserver.instances) {
      const target = [...instance.observed][0];
      if (target === undefined) continue;
      const contentRect = {
        x: 0,
        y: 0,
        width,
        height: 760,
        top: 0,
        right: width,
        bottom: 760,
        left: 0,
        toJSON: () => ({ width, height: 760 }),
      } satisfies DOMRectReadOnly;
      const entry = {
        target,
        contentRect,
        borderBoxSize: [],
        contentBoxSize: [],
        devicePixelContentBoxSize: [],
      } satisfies ResizeObserverEntry;
      instance.callback([entry], instance);
    }
  }
}

interface WorkspaceFixtureProps {
  authoring?: ReactNode;
  artifact?: ReactNode;
  inspector?: ReactNode;
  actionBar?: ReactNode;
  authoringStatus?: ReactNode;
  collapsedStatus?: {
    text: string;
    tone: "neutral" | "busy" | "error";
  };
}

function renderWorkspace(props: WorkspaceFixtureProps = {}) {
  return render(
    <ComposerWorkspace
      authoring={props.authoring ?? <button type="button">Author control</button>}
      artifact={props.artifact ?? <button type="button">Artifact control</button>}
      inspector={props.inspector ?? <aside>Inspector content</aside>}
      actionBar={props.actionBar ?? <button type="button">Primary action</button>}
      authoringStatus={props.authoringStatus}
      collapsedStatus={props.collapsedStatus}
    />,
  );
}

function emitWidth(width: number): HTMLElement {
  act(() => ControllableResizeObserver.emit(width));
  return screen.getByTestId("composer-workspace");
}

describe("ComposerWorkspace", () => {
  beforeEach(() => {
    localStorage.clear();
    ControllableResizeObserver.instances = [];
    vi.stubGlobal("ResizeObserver", ControllableResizeObserver);
    useSessionStore.setState({
      activeSessionId: null,
      compositionState: null,
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the required semantic DOM order and one named pane of each kind", () => {
    renderWorkspace();
    const root = screen.getByTestId("composer-workspace");

    expect([...root.children].map((child) => child.getAttribute("data-workspace-part")))
      .toEqual(["view-tabs", "authoring", "separator", "artifact", "inspector"]);
    expect(
      screen.getAllByRole("region", { name: "Authoring pane", hidden: true }),
    ).toHaveLength(1);
    expect(
      screen.getAllByRole("region", { name: "Pipeline artifact", hidden: true }),
    ).toHaveLength(1);
    const artifactRegion = screen.getByRole("region", {
      name: "Pipeline artifact",
      hidden: true,
    });
    const artifactContent = within(artifactRegion).getByText("Artifact control");
    const action = within(artifactRegion).getByText("Primary action");
    expect(
      artifactContent.compareDocumentPosition(action) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).not.toBe(0);
  });

  it("isolates an authoring render failure without swallowing sibling surfaces", () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    const preventExpectedWindowError = (event: ErrorEvent) => {
      event.preventDefault();
    };
    window.addEventListener("error", preventExpectedWindowError);
    function ThrowingAuthoring(): ReactNode {
      throw new Error("authoring exploded");
    }

    renderWorkspace({ authoring: <ThrowingAuthoring /> });

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Authoring pane encountered an error",
    );
    expect(screen.getByText("Artifact control")).toBeInTheDocument();
    expect(screen.getByText("Primary action")).toBeInTheDocument();
    expect(screen.getByText("Inspector content")).toBeInTheDocument();
    window.removeEventListener("error", preventExpectedWindowError);
    consoleError.mockRestore();
  });

  it("clamps observed geometry and ignores a later zero-width observation", () => {
    localStorage.setItem(
      WORKSPACE_LAYOUT_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        preferredAuthoringWidth: 620,
        authoringCollapsed: false,
      }),
    );
    renderWorkspace();

    const compact = emitWidth(1100);
    expect(compact).toHaveStyle({ "--authoring-pane-width": "460px" });

    const unmeasured = emitWidth(0);
    expect(unmeasured).toHaveStyle({ "--authoring-pane-width": "460px" });

    const wide = emitWidth(1600);
    expect(wide).toHaveStyle({ "--authoring-pane-width": "620px" });
  });

  it.each([
    [959, "narrow", "-1"],
    [960, "compact", "-1"],
    [961, "compact", "-1"],
    [999, "compact", "-1"],
    [1000, "desktop", "-1"],
  ] as const)(
    "pins inclusive responsive behavior at %ipx",
    (width, mode, separatorTabIndex) => {
      renderWorkspace();
      const root = emitWidth(width);
      const separator = screen.getByRole("separator", {
        name: "Resize authoring pane",
        hidden: true,
      });

      expect(root).toHaveAttribute("data-layout-mode", mode);
      expect(separator).toHaveAttribute("tabindex", separatorTabIndex);
      expect(root).toHaveStyle({ "--authoring-pane-width": "360px" });
      const viewTabs = root.querySelector('[role="tablist"]');
      expect(viewTabs).not.toBeNull();
      if (mode === "narrow") {
        expect(viewTabs).not.toHaveAttribute("hidden");
      } else {
        expect(viewTabs).toHaveAttribute("hidden");
      }
    },
  );

  it("implements a focus-following roving Compose/Pipeline tab pattern", async () => {
    const user = userEvent.setup();
    renderWorkspace();
    emitWidth(959);
    const compose = screen.getByRole("tab", { name: "Compose" });
    const pipeline = screen.getByRole("tab", { name: "Pipeline" });
    const authoringRegion = screen.getByRole("region", { name: "Authoring pane" });
    const artifactRegion = document.querySelector<HTMLElement>(
      '[role="region"][aria-label="Pipeline artifact"]',
    );
    expect(artifactRegion).not.toBeNull();

    expect(compose).toHaveAttribute("aria-selected", "true");
    expect(compose).toHaveAttribute("tabindex", "0");
    expect(pipeline).toHaveAttribute("aria-selected", "false");
    expect(pipeline).toHaveAttribute("tabindex", "-1");
    expect(compose).toHaveAttribute("aria-controls", "workspace-authoring-panel");
    expect(pipeline).toHaveAttribute("aria-controls", "workspace-artifact-panel");
    expect(authoringRegion).not.toHaveAttribute("inert");
    expect(artifactRegion).toHaveAttribute("inert");
    expect(artifactRegion).toHaveAttribute("aria-hidden", "true");

    await user.click(pipeline);
    expect(pipeline).toHaveFocus();
    expect(pipeline).toHaveAttribute("aria-selected", "true");
    expect(authoringRegion).toHaveAttribute("inert");
    expect(artifactRegion).not.toHaveAttribute("inert");

    await user.keyboard("{ArrowRight}");
    expect(compose).toHaveFocus();
    expect(compose).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{End}");
    expect(pipeline).toHaveFocus();
    await user.keyboard("{Home}");
    expect(compose).toHaveFocus();
    await user.keyboard("{ArrowLeft}");
    expect(pipeline).toHaveFocus();
  });

  it("keeps non-active narrow content mounted but removes it from focus and live exposure", async () => {
    const user = userEvent.setup();
    const cleanup = vi.fn();
    function CustodyProbe() {
      useEffect(() => cleanup, []);
      return (
        <div role="status">
          In flight <button type="button">Draft control</button>
        </div>
      );
    }

    renderWorkspace({ authoring: <CustodyProbe /> });
    emitWidth(959);
    const authoringProbe = screen.getByText("In flight");
    await user.click(screen.getByRole("tab", { name: "Pipeline" }));

    expect(authoringProbe).toBeInTheDocument();
    expect(authoringProbe.closest("[inert]")).not.toBeNull();
    expect(cleanup).not.toHaveBeenCalled();
    expect(
      screen.queryByRole("button", { name: "Draft control" }),
    ).not.toBeInTheDocument();
  });

  it("collapses and restores without unmounting authoring custody", async () => {
    const user = userEvent.setup();
    const cleanup = vi.fn();
    function CustodyProbe() {
      useEffect(() => cleanup, []);
      return <button type="button">Live draft</button>;
    }

    renderWorkspace({
      authoring: <CustodyProbe />,
      authoringStatus: <div role="status">Authoring ready</div>,
      collapsedStatus: { text: "2 unread acknowledgements", tone: "neutral" },
    });
    emitWidth(1280);
    const draft = screen.getByRole("button", { name: "Live draft" });
    await user.click(
      screen.getByRole("button", { name: "Collapse authoring pane" }),
    );

    const hiddenAuthoring = document.querySelector<HTMLElement>(
      '[role="region"][aria-label="Authoring pane"]',
    );
    expect(hiddenAuthoring).not.toBeNull();
    expect(hiddenAuthoring).toHaveAttribute("inert");
    expect(hiddenAuthoring).toHaveAttribute("aria-hidden", "true");
    expect(draft).toBeInTheDocument();
    expect(cleanup).not.toHaveBeenCalled();
    expect(
      screen.queryByRole("button", { name: "Live draft" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("status", { name: "Authoring ready" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("separator", {
        name: "Resize authoring pane",
        hidden: true,
      }),
    ).toHaveAttribute("tabindex", "-1");
    expect(
      JSON.parse(
        localStorage.getItem(WORKSPACE_LAYOUT_STORAGE_KEY) ?? "null",
      ),
    ).toMatchObject({ authoringCollapsed: true });

    const projection = screen.getByRole("status");
    expect(projection).toHaveTextContent("2 unread acknowledgements");
    const restore = screen.getByRole("button", {
      name: "Restore authoring pane",
    });
    expect(restore).toHaveAccessibleDescription("2 unread acknowledgements");

    await user.click(restore);
    expect(
      screen.getByRole("region", { name: "Authoring pane" }),
    ).not.toHaveAttribute("inert");
    expect(screen.getByRole("button", { name: "Live draft" })).toBe(draft);
    expect(cleanup).not.toHaveBeenCalled();
    expect(
      JSON.parse(
        localStorage.getItem(WORKSPACE_LAYOUT_STORAGE_KEY) ?? "null",
      ),
    ).toMatchObject({ authoringCollapsed: false });
  });

  it.each([
    ["Waiting for ELSPETH", "busy"],
    ["Guided decision pending", "busy"],
    ["Authoring failed", "error"],
    ["New response available", "neutral"],
  ] as const)("projects collapsed status updates: %s", async (text, tone) => {
    const user = userEvent.setup();
    const view = renderWorkspace({ collapsedStatus: { text, tone } });
    emitWidth(1280);
    await user.click(
      screen.getByRole("button", { name: "Collapse authoring pane" }),
    );

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent(text);
    expect(status.closest("[data-tone]")).toHaveAttribute("data-tone", tone);

    view.rerender(
      <ComposerWorkspace
        authoring={<button type="button">Author control</button>}
        artifact={<button type="button">Artifact control</button>}
        inspector={<aside>Inspector content</aside>}
        actionBar={<button type="button">Primary action</button>}
        collapsedStatus={{ text: `${text} updated`, tone }}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(`${text} updated`);
    expect(
      screen.getByRole("button", { name: "Restore authoring pane" }),
    ).toHaveAccessibleDescription(`${text} updated`);
  });

  it("preserves authoring, artifact, and inspector subtree identity across layout changes", async () => {
    const user = userEvent.setup();
    const mounts = { authoring: 0, artifact: 0, inspector: 0 };
    function IdentityProbe({ name }: { name: keyof typeof mounts }) {
      const mounted = useRef(false);
      if (!mounted.current) {
        mounts[name] += 1;
        mounted.current = true;
      }
      return <span>{name}</span>;
    }

    renderWorkspace({
      authoring: <IdentityProbe name="authoring" />,
      artifact: <IdentityProbe name="artifact" />,
      inspector: <IdentityProbe name="inspector" />,
    });
    emitWidth(1280);
    await user.click(
      screen.getByRole("button", { name: "Collapse authoring pane" }),
    );
    await user.click(
      screen.getByRole("button", { name: "Restore authoring pane" }),
    );
    emitWidth(959);
    await user.click(screen.getByRole("tab", { name: "Pipeline" }));
    emitWidth(1280);

    expect(mounts).toEqual({ authoring: 1, artifact: 1, inspector: 1 });
  });

  it("publishes the authoritative session reset through one workspace controller", () => {
    const observations: string[] = [];
    function ContextProbe() {
      const controller = useWorkspacePaneController();
      useLayoutEffect(() => {
        observations.push(controller.state.activeArtifactTab);
      }, [controller]);
      return (
        <button
          type="button"
          onClick={() => controller.actions.selectArtifactTab("run")}
        >
          Select run
        </button>
      );
    }

    renderWorkspace({ artifact: <ContextProbe /> });
    emitWidth(1280);
    fireEvent.click(screen.getByRole("button", { name: "Select run" }));
    expect(observations).toContain("run");

    act(() => useSessionStore.setState({ activeSessionId: "next-session" }));
    expect(observations[observations.length - 1]).toBe("graph");
  });
});
