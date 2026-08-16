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

    // The bar row is its own grid item spanning both columns
    // (elspeth-9c94a58500), placed after both panes so the tab sequence
    // reads chat → separator → artifact → bottom bar → inspector, and
    // before the inspector so tree order keeps the drawer painted above it.
    expect([...root.children].map((child) => child.getAttribute("data-workspace-part")))
      .toEqual([
        "view-tabs",
        "authoring",
        "separator",
        "artifact",
        "action-bar",
        "inspector",
      ]);
    expect(
      screen.getAllByRole("region", { name: "Authoring pane", hidden: true }),
    ).toHaveLength(1);
    expect(
      screen.getAllByRole("region", { name: "Pipeline artifact", hidden: true }),
    ).toHaveLength(1);
    const authoringRegion = screen.getByRole("region", {
      name: "Authoring pane",
      hidden: true,
    });
    const artifactRegion = screen.getByRole("region", {
      name: "Pipeline artifact",
      hidden: true,
    });
    const artifactContent = within(artifactRegion).getByText("Artifact control");
    const action = screen.getByText("Primary action");
    const collapse = screen.getByRole("button", {
      name: "Collapse authoring pane",
    });
    // Neither bottom-bar occupant lives inside a pane region any more: the
    // action bar follows the artifact content, and the collapse control —
    // a disclosure button — sits outside the region it discloses, exactly
    // as the restore button always has. Both are the bar row's children,
    // collapse first (it is the row's authoring-column cell).
    expect(artifactRegion).not.toContainElement(action);
    expect(authoringRegion).not.toContainElement(collapse);
    const barSlot = root.querySelector('[data-workspace-part="action-bar"]');
    expect(barSlot).not.toBeNull();
    expect(barSlot).toContainElement(action);
    expect(barSlot).toContainElement(collapse);
    expect(
      artifactContent.compareDocumentPosition(collapse) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).not.toBe(0);
    expect(
      collapse.compareDocumentPosition(action) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).not.toBe(0);
  });

  it.each([
    // width, collapse?, narrow view, expected: collapse shown, bar shown
    [1280, false, "compose", true, true],
    [1280, true, "compose", false, true],
    [959, false, "compose", true, false],
    [959, false, "pipeline", false, true],
    [959, true, "compose", false, false],
    [959, true, "pipeline", false, true],
  ] as const)(
    "shows the bar row's occupants by state: %ipx collapsed=%s view=%s",
    async (width, collapsed, view, collapseShown, barShown) => {
      // The bar row hosts two things that used to hide with their former
      // parents — the « control with the authoring pane, the action bar with
      // the artifact view (elspeth-9c94a58500). Each still hides on that same
      // fact, so what is visible in every state is what it was before the
      // row existed; and the row itself hides when neither occupant shows,
      // so narrow collapsed Compose does not draw a stray rule across the
      // bottom of an empty view.
      const user = userEvent.setup();
      renderWorkspace();
      const root = emitWidth(width);
      if (collapsed) {
        await user.click(
          screen.getByRole("button", { name: "Collapse authoring pane" }),
        );
      }
      if (view === "pipeline") {
        await user.click(screen.getByRole("tab", { name: "Pipeline" }));
      }

      // By attribute, not by role: the accessible-name algorithm returns ""
      // for a node that is itself `hidden`, so a role query cannot find the
      // control in the states where it is hidden — which are the states
      // under test.
      const collapse = root.querySelector(
        'button[aria-label="Collapse authoring pane"]',
      );
      expect(collapse).not.toBeNull();
      const action = screen.getByText("Primary action");
      const barSlot = root.querySelector('[data-workspace-part="action-bar"]');
      expect(collapse!.closest("[hidden]") === null).toBe(collapseShown);
      expect(action.closest("[hidden]") === null).toBe(barShown);
      expect(barSlot).not.toBeNull();
      expect(barSlot!.hasAttribute("hidden")).toBe(!collapseShown && !barShown);
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
      screen.queryByRole("button", { name: "Restore authoring pane" }),
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
      screen.getByRole("button", { name: "Restore authoring pane" }),
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

  it("renders the collapse/restore pair as icon controls with disclosure semantics", async () => {
    // 2026-08-15 UX review: the full-text secondary-chrome collapse button
    // read as a sixth action-bar button. Both controls are now icon-only
    // (chevrons pointing the way the pane moves), keep their full names as
    // aria-label + title, and carry aria-expanded/aria-controls so the
    // disclosure state is machine-readable.
    const user = userEvent.setup();
    renderWorkspace({});
    emitWidth(1280);

    const collapse = screen.getByRole("button", {
      name: "Collapse authoring pane",
    });
    expect(collapse).toHaveAttribute("aria-expanded", "true");
    expect(collapse).toHaveAttribute("aria-controls", "workspace-authoring-pane");
    expect(collapse).toHaveAttribute("title", "Collapse authoring pane");
    expect(
      collapse.querySelector('svg[data-icon="chevrons-left"]'),
    ).not.toBeNull();
    // Icon-only: the visible label is gone; the accessible name survives via
    // aria-label (pinned by every getByRole lookup in this file).
    expect(collapse.textContent).toBe("");

    await user.click(collapse);

    const restore = screen.getByRole("button", {
      name: "Restore authoring pane",
    });
    expect(restore).toHaveAttribute("aria-expanded", "false");
    expect(restore).toHaveAttribute("aria-controls", "workspace-authoring-pane");
    expect(restore).toHaveAttribute("title", "Restore authoring pane");
    expect(
      restore.querySelector('svg[data-icon="chevrons-right"]'),
    ).not.toBeNull();
    expect(restore.textContent).toBe("");
    // aria-controls must reference a real element — the pane stays in the
    // DOM (hidden) while collapsed.
    expect(document.getElementById("workspace-authoring-pane")).not.toBeNull();
  });

  it("renders an App-owned collapsed projection inside the pane provider and resets it on restore", async () => {
    const user = userEvent.setup();
    // compositionStateLoaded models a hydrated session: the unread
    // projection deliberately ignores deltas measured against a
    // pre-hydration (empty) store, so a live-update fixture must mark the
    // history as loaded the way selectSession's fetch does.
    useSessionStore.setState({
      activeSessionId: "session-1",
      messages: [],
      compositionStateLoaded: true,
    });

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

    // elspeth-7161879770: "busy" and "warning" shared the amber border, so an
    // in-flight readiness check was indistinguishable from a real warning.
    // Assert the two tones resolve to DIFFERENT tokens rather than pinning
    // busy's hue by name — the point of the split is that the three tones stay
    // mutually distinguishable, wherever the palette moves next. The rule
    // pinned here is the production form (:has() reaching the App-owned
    // status span — the wrapper's own data-tone only exists for the
    // {text, tone} projection shape) applied to the RESTORE BUTTON's border:
    // on desktop the collapsed status text is sr-only and the banner box is
    // stripped, so the button border is the only visible tone signal
    // (operator decision 2026-08-15).
    const toneBorder = (tone: string): string => {
      const rule = new RegExp(
        `\\.workspace-collapsed-affordance:has\\(\\.workspace-collapsed-status\\[data-tone="${tone}"\\]\\) \\.workspace-restore-control \\{\\n  border-color: ([^;]+);\\n\\}`,
      ).exec(css);
      if (rule === null) {
        throw new Error(`No collapsed-affordance rule for tone ${tone}`);
      }
      return rule[1];
    };

    const tones = ["busy", "error"].map(toneBorder);
    expect(new Set(tones).size).toBe(tones.length);
    expect(toneBorder("busy")).not.toContain("--color-warning");
    expect(toneBorder("error")).toBe("var(--color-error, currentcolor)");
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

  it("anchors the desktop collapsed restore control to the action bar's bottom-edge band instead of a reserved row", () => {
    const css = readFileSync(
      join(process.cwd(), "src/components/workspace/workspace.css"),
      "utf8",
    );
    // No reserved second grid row (the banner row died with the operator's
    // 2026-08-15 same-line decision)…
    expect(css).not.toMatch(
      /data-authoring-collapsed="true"\]:not\(\[data-layout-mode="narrow"\]\)\s*\{[^}]*grid-template-rows/s,
    );
    // …the affordance bottom-anchors on the SAME inset token the action bar
    // and the expanded « control register against (elspeth-215c989bed)…
    expect(css).toMatch(
      /data-authoring-collapsed="true"\]:not\(\[data-layout-mode="narrow"\]\)\s+\.workspace-collapsed-affordance\s*\{[^}]*bottom:\s*var\(--workspace-bar-inset\)/s,
    );
    // …paints over the bar it shares pixels with because it is positioned
    // on --z-panel-controls (the base rule) while the bar row and the bar
    // are in-flow, unpositioned boxes (elspeth-9c94a58500) — the bar used to
    // sit on that rung itself with tree order in its favour, and this rule
    // then needed a raised token to out-paint it. That token is gone, so the
    // collapsed rule must NOT reintroduce a z-index and the bar must not
    // regain one; the clickability itself is a rendered fact pinned by
    // elementFromPoint in composer-workspace-geometry.spec.ts.
    expect(css).toMatch(
      /^\.workspace-collapsed-affordance\s*\{[^}]*z-index:\s*var\(--z-panel-controls\)/ms,
    );
    expect(css).not.toMatch(
      /data-authoring-collapsed="true"\]:not\(\[data-layout-mode="narrow"\]\)\s+\.workspace-collapsed-affordance\s*\{[^}]*z-index/s,
    );
    expect(css).not.toMatch(/^\.workspace-action-bar\s*\{[^}]*(?:z-index|position):/ms);
    expect(css).not.toContain("--z-panel-controls-raised");
    // …while the bar reserves the button's slot so the first status chip
    // starts clear of it instead of rendering underneath.
    expect(css).toMatch(
      /data-authoring-collapsed="true"\]:not\(\[data-layout-mode="narrow"\]\)\s+\.workspace-action-bar\s*\{[^}]*padding-left:\s*calc\(2 \* var\(--space-sm\) \+ var\(--size-control\)\)/s,
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

  it("treats Collapse and Restore as newer workspace intents", async () => {
    const user = userEvent.setup();
    renderWorkspace();

    const beforeCollapse = claimWorkspaceViewIntent();
    await user.click(
      screen.getByRole("button", { name: "Collapse authoring pane" }),
    );
    expect(isCurrentWorkspaceViewIntent(beforeCollapse)).toBe(false);

    const beforeRestore = claimWorkspaceViewIntent();
    await user.click(
      screen.getByRole("button", { name: "Restore authoring pane" }),
    );
    expect(isCurrentWorkspaceViewIntent(beforeRestore)).toBe(false);
  });
});
