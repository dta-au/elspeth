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
  useEffect,
  useLayoutEffect,
  useRef,
} from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSessionStore } from "@/stores/sessionStore";
import { useBlobStore } from "@/stores/blobStore";
import {
  claimWorkspaceViewIntent,
  dispatchAuthoringToggleIntent,
  isCurrentWorkspaceViewIntent,
} from "@/lib/composer-events";
import { AuthoringPaneToggle } from "./AuthoringPaneToggle";
import { ComposerWorkspace } from "./ComposerWorkspace";
import { useWorkspacePaneController } from "./WorkspacePaneContext";
import type { CollapsedAuthoringStatus } from "./useCollapsedAuthoringStatus";
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
  actionBar?: ReactNode;
  collapsedStatus?: CollapsedAuthoringStatus;
}

/** The artifact host's title bar, as ArtifactWorkspace renders it: the Show
 *  control opens the bar while the conversation is hidden. */
function ArtifactTitleBar({ children }: { children: ReactNode }) {
  return (
    <>
      <div className="artifact-workspace-toolbar">
        <AuthoringPaneToggle placement="artifact" />
        <h2>Pipeline</h2>
      </div>
      {children}
    </>
  );
}

function workspaceElement(props: WorkspaceFixtureProps = {}) {
  return (
    <ComposerWorkspace
      authoring={props.authoring ?? <button type="button">Author control</button>}
      artifact={
        <ArtifactTitleBar>
          {props.artifact ?? <button type="button">Artifact control</button>}
        </ArtifactTitleBar>
      }
      actionBar={props.actionBar ?? <button type="button">Primary action</button>}
      collapsedStatus={props.collapsedStatus}
    />
  );
}

function renderWorkspace(props: WorkspaceFixtureProps = {}) {
  return render(workspaceElement(props));
}

function emitWidth(width: number): HTMLElement {
  act(() => ControllableResizeObserver.emit(width));
  return screen.getByTestId("composer-workspace");
}

describe("ComposerWorkspace", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useBlobStore.getState().reset();
    localStorage.clear();
    ControllableResizeObserver.instances = [];
    vi.stubGlobal("ResizeObserver", ControllableResizeObserver);
    useSessionStore.setState({
      activeSessionId: null,
      compositionState: null,
    });
  });

  it("reveals a collapsed authoring pane for files without focusing chat input", () => {
    localStorage.setItem(WORKSPACE_LAYOUT_STORAGE_KEY, JSON.stringify({ version: 1, preferredAuthoringWidth: 420, authoringCollapsed: true }));
    useSessionStore.setState({ activeSessionId: "session-1" });
    useBlobStore.getState().activateSession("session-1");
    renderWorkspace({ authoring: <button type="button" data-chat-input>Chat input</button> });
    emitWidth(1280);
    expect(screen.getByRole("button", { name: "Show conversation" })).toBeInTheDocument();
    act(() => useBlobStore.getState().revealBlob("session-1", "file-1", "prompt.txt"));
    expect(screen.queryByRole("button", { name: "Show conversation" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Chat input" })).not.toHaveFocus();
  });

  it("switches from narrow pipeline to compose when a file is revealed", async () => {
    useSessionStore.setState({ activeSessionId: "session-1" });
    useBlobStore.getState().activateSession("session-1");
    renderWorkspace();
    emitWidth(800);
    await userEvent.setup().click(screen.getByRole("tab", { name: "Pipeline" }));
    expect(screen.getByRole("tab", { name: "Pipeline" })).toHaveAttribute("aria-selected", "true");
    act(() => useBlobStore.getState().revealBlob("session-1", "file-1", "prompt.txt"));
    expect(screen.getByRole("tab", { name: "Compose" })).toHaveAttribute("aria-selected", "true");
    act(() => useBlobStore.getState().requestPromptEdit("llm", "prompt_template"));
    expect(screen.getByRole("tab", { name: "Pipeline" })).toHaveAttribute("aria-selected", "true");
  });

  it("returns to the narrow Pipeline view before restoring focus to a Show in files trigger", async () => {
    useSessionStore.setState({ activeSessionId: "session-1" });
    useBlobStore.getState().activateSession("session-1");
    renderWorkspace({ artifact: <button type="button">Show in files</button> });
    emitWidth(800);
    await userEvent.setup().click(screen.getByRole("tab", { name: "Pipeline" }));
    const trigger = screen.getByRole("button", { name: "Show in files" });
    act(() => useBlobStore.getState().revealBlob("session-1", "file-1", "prompt.txt", trigger));
    expect(screen.getByRole("tab", { name: "Compose" })).toHaveAttribute("aria-selected", "true");
    expect(trigger.closest("[hidden], [inert]")).not.toBeNull();
    act(() => {
      useBlobStore.getState().setFileManagerOpen(false);
      useBlobStore.getState().returnToRevealTrigger();
    });
    expect(screen.getByRole("tab", { name: "Pipeline" })).toHaveAttribute("aria-selected", "true");
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(trigger.closest("[hidden], [inert]")).toBeNull();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("renders the required semantic DOM order and one named pane of each kind", () => {
    renderWorkspace();
    const root = emitWidth(1280);

    expect([...root.children].map((child) => child.getAttribute("data-workspace-part")))
      .toEqual([
        "view-tabs",
        "authoring",
        "action-bar",
        "separator",
        "artifact",
      ]);
    expect(
      screen.getAllByRole("region", { name: "Authoring pane", hidden: true }),
    ).toHaveLength(1);
    expect(
      screen.getAllByRole("region", { name: "Pipeline artifact", hidden: true }),
    ).toHaveLength(1);
    const authoringRegion = screen.getByRole("region", { name: "Authoring pane" });
    const artifactRegion = screen.getByRole("region", { name: "Pipeline artifact" });
    const action = screen.getByText("Primary action");
    const hide = screen.getByRole("button", { name: "Hide conversation" });
    // 2026-11-20: the Hide control opens the conversation's title bar; the
    // bar row under the pipeline holds workflow actions only.
    expect(authoringRegion).toContainElement(hide);
    expect(artifactRegion).not.toContainElement(action);
    const barSlot = root.querySelector('[data-workspace-part="action-bar"]');
    expect(barSlot).toContainElement(action);
    expect(barSlot).not.toContainElement(hide);
    expect(barSlot?.querySelector("button[aria-controls]")).toBeNull();
    expect(screen.getAllByRole("button", { name: /conversation$/ })).toEqual([hide]);
  });

  it("puts the Hide control first in the conversation's tab order", async () => {
    const user = userEvent.setup();
    renderWorkspace({ authoring: <textarea aria-label="Message box" /> });
    emitWidth(1280);
    document.body.focus();

    await user.tab();

    expect(screen.getByRole("button", { name: "Hide conversation" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("textbox", { name: "Message box" })).toHaveFocus();
  });

  it("opens each title bar with the toggle: Hide in Conversation, Show in Pipeline", async () => {
    // Maintainer ruling 2026-11-20 (home A): the left end of the title bar.
    // Open, the Hide control is the first thing in the conversation's bar;
    // collapsed, the Show control is the first thing in the pipeline's bar,
    // so it stays on the same spot on screen (geometry pinned in
    // tests/e2e/composer-workspace-geometry.spec.ts).
    const user = userEvent.setup();
    renderWorkspace();
    emitWidth(1280);
    const authoringRegion = screen.getByRole("region", { name: "Authoring pane" });
    const title = within(authoringRegion).getByRole("heading", {
      level: 2,
      name: "Conversation",
    });
    const header = title.closest("header");
    expect(header).toHaveClass("workspace-pane-header");
    expect(header?.parentElement).toBe(authoringRegion);
    expect(header?.nextElementSibling).toHaveClass("workspace-authoring-body");

    const hide = screen.getByRole("button", { name: "Hide conversation" });
    expect(header?.firstElementChild).toBe(hide);
    expect(hide.nextElementSibling).toBe(title);

    await user.click(hide);
    const show = screen.getByRole("button", { name: "Show conversation" });
    const pipelineBar = show.closest(".artifact-workspace-toolbar");
    expect(pipelineBar).not.toBeNull();
    expect(pipelineBar?.firstElementChild).toBe(show);
    expect(header).not.toContainElement(show);
    expect(
      screen.queryByRole("button", { name: "Hide conversation", hidden: true }),
    ).toBeNull();
  });

  it("names the authoring title bar from authoringTitle when a host supplies one", () => {
    // ARGOnite: the tutorial run turn reuses this frame for run output, not a
    // conversation; the host names the pane, "Conversation" is the default.
    render(
      <ComposerWorkspace
        authoring={<p>Run output</p>}
        artifact={<button type="button">Artifact control</button>}
        actionBar={<button type="button">Primary action</button>}
        authoringTitle="Run results"
      />,
    );
    const pane = screen.getByRole("region", { name: "Authoring pane" });
    expect(
      within(pane).getByRole("heading", { level: 2, name: "Run results" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Conversation" })).toBeNull();
  });

  it.each([
    // width, stored collapsed?, narrow view, expected toggle, bar shown
    [1280, false, "compose", "Hide conversation", true],
    [1280, true, "compose", "Show conversation", true],
    [959, false, "compose", null, false],
    [959, false, "pipeline", null, true],
    [959, true, "compose", null, false],
    [959, true, "pipeline", null, true],
  ] as const)(
    "renders the toggle and the action row by state: %ipx collapsed=%s view=%s",
    async (width, collapsed, view, toggleName, barShown) => {
      // Under 960px the Compose / Pipeline tabs switch views, so no toggle
      // renders and a stored collapsed preference is ignored (Compose shows
      // the conversation). Narrow Compose has no action row at all.
      localStorage.setItem(
        WORKSPACE_LAYOUT_STORAGE_KEY,
        JSON.stringify({
          version: 1,
          preferredAuthoringWidth: 420,
          authoringCollapsed: collapsed,
        }),
      );
      const user = userEvent.setup();
      renderWorkspace({ authoring: <p>Conversation text</p> });
      const root = emitWidth(width);
      if (view === "pipeline") {
        await user.click(screen.getByRole("tab", { name: "Pipeline" }));
      }

      const toggles = root.querySelectorAll("[data-authoring-toggle]");
      if (toggleName === null) {
        expect(toggles).toHaveLength(0);
      } else {
        expect(toggles).toHaveLength(1);
        expect(toggles[0]).toHaveAccessibleName(toggleName);
        expect(toggles[0].closest("[hidden], [inert]")).toBeNull();
      }
      const barSlot = root.querySelector('[data-workspace-part="action-bar"]');
      expect(barSlot!.hasAttribute("hidden")).toBe(!barShown);
      if (width < 960 && view === "compose") {
        expect(screen.getByText("Conversation text")).toBeVisible();
        expect(root).not.toHaveAttribute("data-authoring-collapsed");
      }
    },
  );

  it("retains the workspace height floor in narrow mode for short-viewport scrolling", () => {
    const css = readFileSync(
      join(process.cwd(), "src/components/workspace/workspace.css"),
      "utf8",
    );
    const narrowRule = css.match(
      /\.composer-workspace\[data-layout-mode="narrow"\]\s*\{([^}]*)\}/s,
    )?.[1];

    expect(narrowRule).toBeDefined();
    // Task B: retain the reflow floor and add both ARGOnite card gutters;
    // the narrow title band is clipped separately to recover log height.
    expect(narrowRule).toMatch(/min-height:\s*calc\(480px \+ 4 \* var\(--space-pane-gutter\)\);/);
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
    window.removeEventListener("error", preventExpectedWindowError);
    consoleError.mockRestore();
  });

  it("honours a pre-mount authoring focus request over the stored collapsed preference", () => {
    // createSession can run while ComposerWorkspace is UNMOUNTED (the
    // empty-landing "+ New session" button, tutorial graduation) — a window
    // event dispatched there lands on zero listeners, so the request is a
    // STORE FLAG the workspace reconciles on mount instead. Without it, the
    // globally-persisted collapsed preference would open the brand-new
    // session with the composer hidden.
    localStorage.setItem(
      WORKSPACE_LAYOUT_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        preferredAuthoringWidth: 420,
        authoringCollapsed: true,
      }),
    );
    useSessionStore.setState({
      activeSessionId: "session-1",
      authoringFocusRequested: true,
    });
    renderWorkspace();
    emitWidth(1280);

    expect(
      screen.queryByRole("button", { name: "Show conversation" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "Authoring pane" }),
    ).not.toHaveAttribute("inert");
    // Consumed exactly once — a later remount must not re-fire it.
    expect(useSessionStore.getState().authoringFocusRequested).toBe(false);
  });

  it("keeps the stored collapsed preference when no focus request is pending", () => {
    localStorage.setItem(
      WORKSPACE_LAYOUT_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        preferredAuthoringWidth: 420,
        authoringCollapsed: true,
      }),
    );
    useSessionStore.setState({ activeSessionId: "session-1" });
    renderWorkspace();
    emitWidth(1280);

    expect(
      screen.getByRole("button", { name: "Show conversation" }),
    ).toBeInTheDocument();
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
    // Card width leaves both slots' 42px padding and the 640px artifact floor.
    expect(compact).toHaveStyle({ "--authoring-pane-width": "418px" });

    const unmeasured = emitWidth(0);
    expect(unmeasured).toHaveStyle({ "--authoring-pane-width": "418px" });

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
      return (
        <>
          <button type="button">Live draft</button>
          <div role="status">Authoring ready</div>
        </>
      );
    }

    renderWorkspace({
      authoring: <CustodyProbe />,
      collapsedStatus: { text: "2 unread acknowledgements", tone: "neutral" },
    });
    emitWidth(1280);
    const draft = screen.getByRole("button", { name: "Live draft" });
    const authoringLiveStatus = screen.getByRole("status");
    await user.click(
      screen.getByRole("button", { name: "Hide conversation" }),
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
      name: "Show conversation",
    });
    expect(restore).toHaveAccessibleDescription("2 unread acknowledgements");
    expect(restore).toHaveFocus();
    expect(document.activeElement?.closest("[hidden], [inert]")).toBeNull();

    await user.click(restore);
    const collapse = screen.getByRole("button", {
      name: "Hide conversation",
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

  it("moves focus to the counterpart control and flips the disclosure name and chevron", async () => {
    // Two instances now (one per title bar), so the clicked control unmounts
    // on every flip and focus must land on its counterpart, never on body.
    const user = userEvent.setup();
    renderWorkspace({});
    emitWidth(1280);

    const hide = screen.getByRole("button", { name: "Hide conversation" });
    expect(hide).toHaveAttribute("aria-expanded", "true");
    expect(hide).toHaveAttribute("aria-controls", "workspace-authoring-pane");
    expect(hide).toHaveAttribute("aria-keyshortcuts", "Control+Backslash");
    expect(hide).toHaveAttribute("title", "Hide conversation (Ctrl+\\)");
    expect(hide).not.toHaveAttribute("aria-describedby");
    expect(hide.querySelector('svg[data-icon="chevrons-left"]')).not.toBeNull();
    expect(hide.textContent).toBe("");

    await user.click(hide);

    const show = screen.getByRole("button", { name: "Show conversation" });
    expect(show).not.toBe(hide);
    expect(hide.isConnected).toBe(false);
    expect(show).toHaveFocus();
    expect(show).toHaveAttribute("aria-expanded", "false");
    expect(show).toHaveAttribute("aria-controls", "workspace-authoring-pane");
    // Nothing unread and a neutral status: no badge, no status line in the
    // tooltip.
    expect(show).toHaveAttribute("title", "Show conversation (Ctrl+\\)");
    expect(show.querySelector('svg[data-icon="chevrons-right"]')).not.toBeNull();
    expect(show.querySelector(".workspace-pane-toggle-badge")).toBeNull();
    const status = screen.getByRole("status");
    expect(show.nextElementSibling).toBe(status);
    expect(status).toHaveClass("visually-hidden");
    expect(show).toHaveAttribute("aria-describedby", status.id);
    expect(document.getElementById("workspace-authoring-pane")).not.toBeNull();

    await user.click(show);

    const hideAgain = screen.getByRole("button", { name: "Hide conversation" });
    expect(hideAgain).toHaveFocus();
    expect(hideAgain).toHaveAttribute("aria-expanded", "true");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("toggles on Ctrl+\\ and keeps focus where it was when that stays visible", async () => {
    const user = userEvent.setup();
    renderWorkspace({ authoring: <textarea data-chat-input aria-label="Chat input" /> });
    const root = emitWidth(1280);
    const artifactControl = screen.getByRole("button", { name: "Artifact control" });
    artifactControl.focus();

    act(() => dispatchAuthoringToggleIntent());
    expect(root).toHaveAttribute("data-authoring-collapsed", "true");
    expect(artifactControl).toHaveFocus();

    act(() => dispatchAuthoringToggleIntent());
    expect(root).not.toHaveAttribute("data-authoring-collapsed");
    expect(artifactControl).toHaveFocus();

    // From inside the conversation, hiding it rescues focus to Show.
    const input = screen.getByRole("textbox", { name: "Chat input" });
    await user.click(input);
    act(() => dispatchAuthoringToggleIntent());
    expect(screen.getByRole("button", { name: "Show conversation" })).toHaveFocus();
  });

  it("switches the narrow views on Ctrl+\\, focusing the selected tab", () => {
    renderWorkspace();
    emitWidth(720);
    act(() => dispatchAuthoringToggleIntent());
    expect(screen.getByRole("tab", { name: "Pipeline" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Pipeline" })).toHaveFocus();
    act(() => dispatchAuthoringToggleIntent());
    expect(screen.getByRole("tab", { name: "Compose" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Compose" })).toHaveFocus();
    expect(screen.getByTestId("composer-workspace")).not.toHaveAttribute(
      "data-authoring-collapsed",
    );
  });

  it("names the toggle after a host's authoringTitle", async () => {
    const user = userEvent.setup();
    render(
      <ComposerWorkspace
        authoring={<p>Run output</p>}
        artifact={<ArtifactTitleBar>{null}</ArtifactTitleBar>}
        actionBar={null}
        authoringTitle="Run results"
      />,
    );
    emitWidth(1280);
    await user.click(screen.getByRole("button", { name: "Hide run results" }));
    expect(screen.getByRole("button", { name: "Show run results" })).toHaveFocus();
  });

  it("projects the live status from the session store and resets it on restore", async () => {
    const user = userEvent.setup();
    // compositionStateLoaded models a hydrated session: the unread
    // projection deliberately ignores deltas measured against a
    // pre-hydration (empty) store.
    useSessionStore.setState({
      activeSessionId: "session-1",
      messages: [],
      compositionStateLoaded: true,
      isComposing: false,
      error: null,
    });

    renderWorkspace();
    emitWidth(1280);
    await user.click(screen.getByRole("button", { name: "Hide conversation" }));
    act(() =>
      useSessionStore.setState({
        messages: [
          { id: "message-1", role: "assistant" } as never,
          { id: "message-2", role: "assistant" } as never,
        ],
      }),
    );
    const show = screen.getByRole("button", { name: "Show conversation" });
    expect(screen.getByRole("status")).toHaveTextContent("2 new messages");
    expect(show).toHaveAccessibleDescription("2 new messages");
    expect(show).toHaveAttribute(
      "title",
      "Show conversation (Ctrl+\\)\n2 new messages",
    );
    const badge = show.querySelector(".workspace-pane-toggle-badge--count");
    expect(badge).toHaveTextContent("2");
    expect(badge).toHaveAttribute("aria-hidden", "true");
    // The accessible name stays the label; the count is not read twice.
    expect(show).toHaveAccessibleName("Show conversation");

    await user.click(show);
    await user.click(screen.getByRole("button", { name: "Hide conversation" }));
    expect(screen.getByRole("status")).toHaveTextContent(
      "Authoring pane collapsed",
    );
    expect(
      screen
        .getByRole("button", { name: "Show conversation" })
        .querySelector(".workspace-pane-toggle-badge"),
    ).toBeNull();
  });

  it.each([
    [1, "1"],
    [9, "9"],
    [12, "9+"],
  ])("caps the unread badge: %i reads %s", async (count, text) => {
    const user = userEvent.setup();
    renderWorkspace({
      collapsedStatus: { text: `${count} new messages`, tone: "busy", unreadCount: count },
    });
    emitWidth(1280);
    await user.click(screen.getByRole("button", { name: "Hide conversation" }));
    const show = screen.getByRole("button", { name: "Show conversation" });
    expect(show.querySelector(".workspace-pane-toggle-badge--count")).toHaveTextContent(
      new RegExp(`^${text.replace("+", "\\+")}$`),
    );
    expect(show.querySelector(".workspace-pane-toggle-badge--dot")).toBeNull();
  });

  it.each([
    ["Waiting for ELSPETH", "busy", "busy"],
    ["Authoring failed", "error", "error"],
    ["New response available", "neutral", null],
  ] as const)("projects collapsed status updates: %s", async (text, tone, dot) => {
    const user = userEvent.setup();
    const view = renderWorkspace({ collapsedStatus: { text, tone } });
    emitWidth(1280);
    await user.click(screen.getByRole("button", { name: "Hide conversation" }));

    const show = screen.getByRole("button", { name: "Show conversation" });
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent(text);
    expect(status).toHaveAttribute("data-tone", tone);
    const badge = show.querySelector(".workspace-pane-toggle-badge--dot");
    if (dot === null) {
      expect(badge).toBeNull();
      expect(show).toHaveAttribute("title", "Show conversation (Ctrl+\\)");
    } else {
      expect(badge).toHaveAttribute("data-tone", dot);
      expect(badge).toHaveAttribute("aria-hidden", "true");
      expect(show).toHaveAttribute("title", `Show conversation (Ctrl+\\)\n${text}`);
    }

    view.rerender(
      workspaceElement({ collapsedStatus: { text: `${text} updated`, tone } }),
    );
    expect(screen.getByRole("status")).toHaveTextContent(`${text} updated`);
    expect(
      screen.getByRole("button", { name: "Show conversation" }),
    ).toHaveAccessibleDescription(`${text} updated`);
  });

  it("maps the badge tones onto distinct tokens", () => {
    const css = readFileSync(
      join(process.cwd(), "src/components/workspace/workspace.css"),
      "utf8",
    );
    const dotFill = (selector: string): string => {
      const rule = new RegExp(
        `^${selector.replace(/[.[\]="-]/g, "\\$&")} \\{[^}]*background-color: ([^;]+);`,
        "m",
      ).exec(css);
      if (rule === null) throw new Error(`No rule for ${selector}`);
      return rule[1];
    };
    // busy and error must stay distinguishable (elspeth-7161879770 lineage).
    expect(dotFill(".workspace-pane-toggle-badge--dot")).toBe("var(--color-info)");
    expect(dotFill('.workspace-pane-toggle-badge--dot[data-tone="error"]')).toBe(
      "var(--color-error)",
    );
    expect(dotFill(".workspace-pane-toggle-badge--count")).toBe("var(--color-info)");
  });

  it("preserves authoring and artifact subtree identity across layout changes", async () => {
    const user = userEvent.setup();
    const mounts = { authoring: 0, artifact: 0 };
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
    });
    emitWidth(1280);
    await user.click(
      screen.getByRole("button", { name: "Hide conversation" }),
    );
    await user.click(
      screen.getByRole("button", { name: "Show conversation" }),
    );
    emitWidth(959);
    await user.click(screen.getByRole("tab", { name: "Pipeline" }));
    emitWidth(1280);

    expect(mounts).toEqual({ authoring: 1, artifact: 1 });
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

  it("moves focus from a focused Show control to the Compose tab when the layout goes narrow", async () => {
    // Under 960px the toggle is not rendered and the stored collapsed
    // preference is ignored, so Compose shows the conversation; focus must
    // not fall to the document body with the control.
    const user = userEvent.setup();
    renderWorkspace();
    emitWidth(1280);
    await user.click(screen.getByRole("button", { name: "Hide conversation" }));
    const show = screen.getByRole("button", { name: "Show conversation" });
    expect(show).toHaveFocus();

    emitWidth(959);

    expect(show.isConnected).toBe(false);
    expect(screen.getByRole("tab", { name: "Compose" })).toHaveFocus();
    expect(screen.getByRole("region", { name: "Authoring pane" })).toBeVisible();

    // Back on a wide layout the stored preference applies again.
    emitWidth(1280);
    expect(screen.getByRole("button", { name: "Show conversation" })).toBeInTheDocument();
  });

  it.each([false, true])(
    "repairs toggle focus when retained narrow Pipeline returns, collapsed=%s",
    async (collapsed) => {
      const user = userEvent.setup();
      renderWorkspace();
      emitWidth(720);
      await user.click(screen.getByRole("tab", { name: "Pipeline" }));
      emitWidth(1280);
      if (collapsed) {
        await user.click(screen.getByRole("button", { name: "Hide conversation" }));
      }
      const toggle = screen.getByRole("button", {
        name: collapsed ? "Show conversation" : "Hide conversation",
      });
      toggle.focus();
      expect(toggle).toHaveFocus();

      emitWidth(720);

      expect(toggle.isConnected).toBe(false);
      expect(screen.getByRole("tab", { name: "Pipeline" })).toHaveFocus();
      expect(document.activeElement?.closest("[hidden], [inert]")).toBeNull();
    },
  );

  it("repairs action focus when narrow Compose hides the workflow actions", () => {
    renderWorkspace();
    emitWidth(1280);
    const action = screen.getByRole("button", { name: "Primary action" });
    action.focus();

    emitWidth(720);

    expect(action.closest("[hidden]")).not.toBeNull();
    expect(screen.getByRole("tab", { name: "Compose" })).toHaveFocus();
    expect(document.activeElement?.closest("[hidden], [inert]")).toBeNull();
  });

  it("keeps the action row free of pane controls and overlay reservations", () => {
    const css = readFileSync(
      join(process.cwd(), "src/components/workspace/workspace.css"),
      "utf8",
    );
    expect(css).not.toMatch(
      /data-authoring-collapsed="true"\]:not\(\[data-layout-mode="narrow"\]\)\s*\{[^}]*grid-template-rows/s,
    );
    expect(css).not.toMatch(/^\.workspace-action-bar\s*\{[^}]*(?:z-index|position):/ms);
    expect(css).not.toContain("--z-panel-controls-raised");
    expect(css).not.toContain("workspace-pane-controls");
    expect(css).not.toContain("workspace-collapsed-affordance");
    expect(css).toMatch(
      /^\.workspace-action-bar-slot\s*\{[^}]*display:\s*flex;[^}]*align-items:\s*center;/ms,
    );
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

  it("brings the graph tab back when a prompt edit is requested from another artifact tab", () => {
    // The file manager's "Edit prompt" only sends the request; the inspector
    // that hosts the editor lives on the graph tab, so the workspace must show it.
    function ActiveTabProbe() {
      const controller = useWorkspacePaneController();
      return (
        <button type="button" onClick={() => controller.actions.selectArtifactTab("run")}>
          Active tab: {controller.state.activeArtifactTab}
        </button>
      );
    }
    useSessionStore.setState({ activeSessionId: "session-1" });
    useBlobStore.getState().activateSession("session-1");
    renderWorkspace({ artifact: <ActiveTabProbe /> });
    emitWidth(1280);
    fireEvent.click(screen.getByRole("button", { name: "Active tab: graph" }));
    expect(screen.getByRole("button", { name: "Active tab: run" })).toBeInTheDocument();
    act(() => useBlobStore.getState().requestPromptEdit("llm", "prompt_template"));
    expect(screen.getByRole("button", { name: "Active tab: graph" })).toBeInTheDocument();
    // The request is left for the inspector's editor to consume.
    expect(useBlobStore.getState().editRequest).toMatchObject({ nodeId: "llm", fieldPath: "prompt_template" });
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
      localStorage.setItem(
        WORKSPACE_LAYOUT_STORAGE_KEY,
        JSON.stringify({
          version: 1,
          preferredAuthoringWidth: 420,
          authoringCollapsed: true,
        }),
      );
      renderWorkspace({
        authoring: <textarea data-chat-input aria-label="Chat input" />,
      });
      emitWidth(width);
      if (width < 960) {
        await user.click(screen.getByRole("tab", { name: "Pipeline" }));
      }

      act(() => window.dispatchEvent(new Event("elspeth:focus-authoring")));

      // Ctrl+/ clears the stored preference too, so the next wide layout
      // shows the conversation.
      expect(
        JSON.parse(localStorage.getItem(WORKSPACE_LAYOUT_STORAGE_KEY) ?? "null"),
      ).toMatchObject({ authoringCollapsed: false });
      if (width < 960) {
        expect(screen.getByRole("tab", { name: "Compose" })).toHaveAttribute(
          "aria-selected",
          "true",
        );
      }
      expect(screen.getByRole("textbox", { name: "Chat input" })).toHaveFocus();
      emitWidth(1280);
      expect(screen.getByTestId("composer-workspace")).not.toHaveAttribute(
        "data-authoring-collapsed",
      );
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

  it("treats Collapse and Restore as newer workspace intents", async () => {
    const user = userEvent.setup();
    renderWorkspace();

    const beforeCollapse = claimWorkspaceViewIntent();
    await user.click(
      screen.getByRole("button", { name: "Hide conversation" }),
    );
    expect(isCurrentWorkspaceViewIntent(beforeCollapse)).toBe(false);

    const beforeRestore = claimWorkspaceViewIntent();
    await user.click(
      screen.getByRole("button", { name: "Show conversation" }),
    );
    expect(isCurrentWorkspaceViewIntent(beforeRestore)).toBe(false);
  });
});
