import { readFileSync } from "node:fs";
import { join } from "node:path";

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
import {
  claimWorkspaceViewIntent,
  isCurrentWorkspaceViewIntent,
} from "@/lib/composer-events";
import { ComposerWorkspace } from "./ComposerWorkspace";
import { useWorkspacePaneController } from "./WorkspacePaneContext";
import { useCollapsedAuthoringStatus } from "./useCollapsedAuthoringStatus";
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
    vi.restoreAllMocks();
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

  it("retains the workspace height floor in narrow mode for short-viewport scrolling", () => {
    const css = readFileSync(
      join(process.cwd(), "src/components/workspace/workspace.css"),
      "utf8",
    );
    const narrowRule = css.match(
      /\.composer-workspace\[data-layout-mode="narrow"\]\s*\{([^}]*)\}/s,
    )?.[1];

    expect(narrowRule).toBeDefined();
    expect(narrowRule).toMatch(/min-height:\s*420px;/);
    expect(narrowRule).not.toMatch(/min-height:\s*0;/);
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
      const viewTabs = root.querySelector(".workspace-view-tabs");
      expect(viewTabs).not.toBeNull();
      if (mode === "narrow") {
        expect(viewTabs).toHaveAttribute("role", "tablist");
        expect(viewTabs).not.toHaveAttribute("hidden");
        expect(screen.getAllByRole("tab")).toHaveLength(2);
        expect(screen.getAllByRole("tabpanel", { hidden: true })).toHaveLength(2);
      } else {
        expect(viewTabs).not.toHaveAttribute("role");
        expect(viewTabs).toHaveAttribute("hidden");
        expect(screen.queryByRole("tab", { hidden: true })).not.toBeInTheDocument();
        expect(
          screen.queryByRole("tabpanel", { hidden: true }),
        ).not.toBeInTheDocument();
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
    const panels = screen.getAllByRole("tabpanel", { hidden: true });
    expect(panels).toHaveLength(2);
    expect(panels[0]).toHaveAttribute("aria-labelledby", compose.id);
    expect(panels[1]).toHaveAttribute("aria-labelledby", pipeline.id);
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
    const authoringLiveStatus = screen.getByRole("status");
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
    expect(authoringLiveStatus).toBeInTheDocument();
    expect(authoringLiveStatus.closest("[hidden]")).not.toBeNull();
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
    expect(projection).not.toBe(authoringLiveStatus);
    expect(screen.getAllByRole("status")).toEqual([projection]);
    expect(projection).toHaveTextContent("2 unread acknowledgements");
    const restore = screen.getByRole("button", {
      name: "Restore authoring pane",
    });
    expect(restore).toHaveAccessibleDescription("2 unread acknowledgements");
    expect(restore).toHaveFocus();
    expect(document.activeElement?.closest("[hidden], [inert]")).toBeNull();

    await user.click(restore);
    const collapse = screen.getByRole("button", {
      name: "Collapse authoring pane",
    });
    expect(collapse).toHaveFocus();
    expect(document.activeElement?.closest("[hidden], [inert]")).toBeNull();
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

  it("renders an App-owned collapsed projection inside the pane provider and resets it on restore", async () => {
    const user = userEvent.setup();
    useSessionStore.setState({ activeSessionId: "session-1", messages: [] });

    function ContextStatus() {
      const { state } = useWorkspacePaneController();
      const status = useCollapsedAuthoringStatus({
        activeSessionId: "session-1",
        authoringCollapsed: state.authoringCollapsed,
      });
      return <span data-tone={status.tone}>{status.text}</span>;
    }

    renderWorkspace({ collapsedStatus: <ContextStatus /> as never });
    emitWidth(1280);
    await user.click(
      screen.getByRole("button", { name: "Collapse authoring pane" }),
    );
    act(() =>
      useSessionStore.setState({
        messages: [{ id: "message-1", role: "assistant" } as never],
      }),
    );
    expect(screen.getByRole("status")).toHaveTextContent("1 new message");

    await user.click(
      screen.getByRole("button", { name: "Restore authoring pane" }),
    );
    await user.click(
      screen.getByRole("button", { name: "Collapse authoring pane" }),
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Authoring pane collapsed",
    );
  });

  it("maps App-owned ReactNode tones onto the collapsed affordance border", () => {
    const css = readFileSync(
      join(process.cwd(), "src/components/workspace/workspace.css"),
      "utf8",
    );

    expect(css).toContain(
      '.workspace-collapsed-affordance:has(.workspace-collapsed-status[data-tone="busy"]) {\n' +
        "  border-color: var(--color-warning, currentcolor);\n}",
    );
    expect(css).toContain(
      '.workspace-collapsed-affordance:has(.workspace-collapsed-status[data-tone="error"]) {\n' +
        "  border-color: var(--color-error, currentcolor);\n}",
    );
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

  it.each([
    [1200, 959, "narrow"],
    [320, 1000, "desktop"],
  ] as const)(
    "uses observed workspace width at a %ipx viewport for %ipx content",
    (viewportWidth, workspaceWidth, expectedMode) => {
      vi.spyOn(window, "innerWidth", "get").mockReturnValue(viewportWidth);
      renderWorkspace();

      const root = emitWidth(workspaceWidth);

      expect(root).toHaveAttribute("data-layout-mode", expectedMode);
      if (expectedMode === "narrow") {
        expect(screen.getByRole("tablist", { name: "Workspace view" })).toBeVisible();
        expect(screen.getAllByRole("tab")).toHaveLength(2);
        expect(screen.getAllByRole("tabpanel", { hidden: true })).toHaveLength(2);
      } else {
        expect(screen.queryByRole("tab", { hidden: true })).not.toBeInTheDocument();
        expect(
          screen.queryByRole("tabpanel", { hidden: true }),
        ).not.toBeInTheDocument();
        expect(
          screen.getByRole("region", { name: "Authoring pane" }),
        ).toBeVisible();
        expect(
          screen.getByRole("region", { name: "Pipeline artifact" }),
        ).toBeVisible();
      }
    },
  );

  it("moves focus from a disappearing narrow tab to its desktop region", async () => {
    const user = userEvent.setup();
    renderWorkspace();
    emitWidth(959);
    await user.click(screen.getByRole("tab", { name: "Pipeline" }));

    emitWidth(1000);

    expect(screen.queryByRole("tab", { hidden: true })).not.toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "Pipeline artifact" }),
    ).toHaveFocus();
  });

  it("moves focus from desktop content that becomes hidden to the selected narrow tab", () => {
    renderWorkspace();
    emitWidth(1280);
    const artifactControl = screen.getByRole("button", {
      name: "Artifact control",
    });
    artifactControl.focus();
    expect(artifactControl).toHaveFocus();

    emitWidth(959);

    expect(screen.getByRole("tab", { name: "Compose" })).toHaveFocus();
    expect(artifactControl.closest("[hidden], [inert]")).not.toBeNull();
    expect(document.activeElement?.closest("[hidden], [inert]")).toBeNull();
  });

  it("repairs desktop authoring focus when Pipeline remains the selected narrow view", async () => {
    const user = userEvent.setup();
    renderWorkspace();
    emitWidth(959);
    await user.click(screen.getByRole("tab", { name: "Pipeline" }));
    emitWidth(1280);
    const authorControl = screen.getByRole("button", { name: "Author control" });
    authorControl.focus();

    emitWidth(959);

    expect(screen.getByRole("tab", { name: "Pipeline" })).toHaveFocus();
    expect(authorControl.closest("[hidden], [inert]")).not.toBeNull();
    expect(document.activeElement?.closest("[hidden], [inert]")).toBeNull();
  });

  it("moves focus from the disappearing desktop separator to the selected narrow tab", () => {
    renderWorkspace();
    emitWidth(1280);
    const separator = screen.getByRole("separator", {
      name: "Resize authoring pane",
    });
    separator.focus();
    expect(separator).toHaveFocus();

    emitWidth(959);

    expect(screen.getByRole("tab", { name: "Compose" })).toHaveFocus();
    expect(separator.closest("[hidden]")).not.toBeNull();
    expect(document.activeElement?.closest("[hidden], [inert]")).toBeNull();
  });

  it("keeps a visible Restore target focused when collapsed Compose enters narrow mode", async () => {
    const user = userEvent.setup();
    renderWorkspace();
    emitWidth(1280);
    await user.click(
      screen.getByRole("button", { name: "Collapse authoring pane" }),
    );
    const restore = screen.getByRole("button", {
      name: "Restore authoring pane",
    });
    restore.focus();

    emitWidth(959);

    expect(screen.getByRole("tab", { name: "Compose" })).toBeVisible();
    expect(restore).toHaveFocus();
    expect(document.activeElement?.closest("[hidden], [inert]")).toBeNull();
  });

  it("keeps the narrow collapsed affordance inside the content slot after the tablist", async () => {
    const user = userEvent.setup();
    renderWorkspace({
      collapsedStatus: { text: "Authoring paused", tone: "busy" },
    });
    const root = emitWidth(320);
    await user.click(
      screen.getByRole("button", { name: "Collapse authoring pane" }),
    );

    const tablist = screen.getByRole("tablist", { name: "Workspace view" });
    const restore = screen.getByRole("button", {
      name: "Restore authoring pane",
    });
    const authoringSlot = root.querySelector(
      '[data-workspace-part="authoring"]',
    );
    expect(authoringSlot).not.toBeNull();
    expect(authoringSlot).toContainElement(restore);
    expect(tablist).not.toContainElement(restore);
    expect(
      tablist.compareDocumentPosition(authoringSlot as Node) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).not.toBe(0);
  });

  it("reserves a desktop row for collapsed restore status instead of overlaying artifacts", () => {
    const css = readFileSync(
      join(process.cwd(), "src/components/workspace/workspace.css"),
      "utf8",
    );
    expect(css).toMatch(/data-authoring-collapsed="true"\]:not\(\[data-layout-mode="narrow"\]\)\s*\{[^}]*grid-template-rows:\s*auto minmax\(0, 1fr\)/s);
    expect(css).toMatch(/data-authoring-collapsed="true"\]:not\(\[data-layout-mode="narrow"\]\)\s+\.workspace-collapsed-affordance\s*\{[^}]*position:\s*static/s);
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

  it("lets an artifact intent reveal the narrow Pipeline view", () => {
    function ArtifactIntent() {
      const controller = useWorkspacePaneController();
      return <button type="button" onClick={() => controller.actions.showPipeline()}>Open artifact</button>;
    }
    renderWorkspace({ authoring: <ArtifactIntent /> });
    emitWidth(720);
    fireEvent.click(screen.getByRole("button", { name: "Open artifact" }));
    expect(screen.getByRole("tab", { name: "Pipeline" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("region", { name: "Pipeline artifact" })).toBeVisible();
  });

  it("reveals Compose before focusing the chat input from the shared authoring intent", () => {
    renderWorkspace({ authoring: <textarea data-chat-input aria-label="Chat input" /> });
    emitWidth(720);
    fireEvent.click(screen.getByRole("tab", { name: "Pipeline" }));
    act(() => window.dispatchEvent(new Event("elspeth:focus-authoring")));
    expect(screen.getByRole("tab", { name: "Compose" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("textbox", { name: "Chat input" })).toHaveFocus();
  });

  it.each([1280, 720])(
    "restores collapsed authoring before focusing chat at %ipx",
    async (width) => {
      const user = userEvent.setup();
      renderWorkspace({
        authoring: <textarea data-chat-input aria-label="Chat input" />,
      });
      const root = emitWidth(width);
      await user.click(
        screen.getByRole("button", { name: "Collapse authoring pane" }),
      );
      if (width < 960) {
        await user.click(screen.getByRole("tab", { name: "Pipeline" }));
      }

      act(() => window.dispatchEvent(new Event("elspeth:focus-authoring")));

      expect(root).not.toHaveAttribute("data-authoring-collapsed");
      if (width < 960) {
        expect(screen.getByRole("tab", { name: "Compose" })).toHaveAttribute(
          "aria-selected",
          "true",
        );
      }
      expect(screen.getByRole("textbox", { name: "Chat input" })).toHaveFocus();
    },
  );

  it("treats authoring focus and direct workspace tabs as newer workspace intents", () => {
    renderWorkspace({ authoring: <textarea data-chat-input aria-label="Chat input" /> });
    emitWidth(720);

    const beforeFocus = claimWorkspaceViewIntent();
    act(() => window.dispatchEvent(new Event("elspeth:focus-authoring")));
    expect(isCurrentWorkspaceViewIntent(beforeFocus)).toBe(false);

    const beforePipeline = claimWorkspaceViewIntent();
    fireEvent.click(screen.getByRole("tab", { name: "Pipeline" }));
    expect(isCurrentWorkspaceViewIntent(beforePipeline)).toBe(false);

    const beforeCompose = claimWorkspaceViewIntent();
    fireEvent.click(screen.getByRole("tab", { name: "Compose" }));
    expect(isCurrentWorkspaceViewIntent(beforeCompose)).toBe(false);
  });
});
