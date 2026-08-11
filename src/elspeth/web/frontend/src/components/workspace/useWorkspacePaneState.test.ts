import { act, render, renderHook } from "@testing-library/react";
import {
  createElement,
  startTransition,
  Suspense,
  useLayoutEffect,
  useState,
} from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  ARTIFACT_MIN,
  AUTHORING_MAX,
  AUTHORING_MIN,
  clampAuthoringWidth,
  nearestAvailableArtifactTab,
  paneBoundsForWidth,
  readStoredWorkspaceLayout,
  useWorkspacePaneState,
  WORKSPACE_LAYOUT_STORAGE_KEY,
} from "./useWorkspacePaneState";
import type {
  ArtifactTab,
  StoredWorkspaceLayoutV1,
} from "./workspaceTypes";
import type { WorkspacePaneState } from "./useWorkspacePaneState";

const VALID_LAYOUT: StoredWorkspaceLayoutV1 = {
  version: 1,
  preferredAuthoringWidth: 620,
  authoringCollapsed: false,
};

function storedLayout(): StoredWorkspaceLayoutV1 | null {
  const raw = localStorage.getItem(WORKSPACE_LAYOUT_STORAGE_KEY);
  return raw === null ? null : (JSON.parse(raw) as StoredWorkspaceLayoutV1);
}

describe("workspace pane state", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
  });

  describe("stored layout admission", () => {
    it("returns null when storage has no layout", () => {
      expect(readStoredWorkspaceLayout(localStorage)).toBeNull();
    });

    it("accepts a valid layout and constructs a fresh owned object", () => {
      localStorage.setItem(
        WORKSPACE_LAYOUT_STORAGE_KEY,
        JSON.stringify(VALID_LAYOUT),
      );

      const admitted = readStoredWorkspaceLayout(localStorage);

      expect(admitted).toEqual(VALID_LAYOUT);
      expect(admitted).not.toBe(VALID_LAYOUT);
      expect(Object.getPrototypeOf(admitted)).toBe(Object.prototype);
    });

    it("returns null for corrupt JSON", () => {
      localStorage.setItem(WORKSPACE_LAYOUT_STORAGE_KEY, "not-json");

      expect(readStoredWorkspaceLayout(localStorage)).toBeNull();
    });

    it.each([
      ["null", "null"],
      ["an array", JSON.stringify([VALID_LAYOUT])],
      ["an extra key", JSON.stringify({ ...VALID_LAYOUT, extra: true })],
    ])("returns null for %s", (_label, raw) => {
      localStorage.setItem(WORKSPACE_LAYOUT_STORAGE_KEY, raw);

      expect(readStoredWorkspaceLayout(localStorage)).toBeNull();
    });

    it("rejects a value with a custom prototype", () => {
      localStorage.setItem(WORKSPACE_LAYOUT_STORAGE_KEY, "{}");
      const prototypeBearing = Object.assign(
        Object.create({ inherited: true }) as Record<string, unknown>,
        VALID_LAYOUT,
      );
      vi.spyOn(JSON, "parse").mockReturnValueOnce(prototypeBearing);

      expect(readStoredWorkspaceLayout(localStorage)).toBeNull();
    });

    it("accepts a null-prototype value but still returns an owned object", () => {
      localStorage.setItem(WORKSPACE_LAYOUT_STORAGE_KEY, "{}");
      const nullPrototype = Object.assign(
        Object.create(null) as Record<string, unknown>,
        VALID_LAYOUT,
      );
      vi.spyOn(JSON, "parse").mockReturnValueOnce(nullPrototype);

      const admitted = readStoredWorkspaceLayout(localStorage);

      expect(admitted).toEqual(VALID_LAYOUT);
      expect(Object.getPrototypeOf(admitted)).toBe(Object.prototype);
    });

    it("rejects the wrong version", () => {
      localStorage.setItem(
        WORKSPACE_LAYOUT_STORAGE_KEY,
        JSON.stringify({ ...VALID_LAYOUT, version: 2 }),
      );

      expect(readStoredWorkspaceLayout(localStorage)).toBeNull();
    });

    it.each([
      ["NaN", Number.NaN],
      ["positive infinity", Number.POSITIVE_INFINITY],
      ["negative infinity", Number.NEGATIVE_INFINITY],
      ["below the minimum", AUTHORING_MIN - 1],
      ["above the maximum", AUTHORING_MAX + 1],
      ["a string", "420"],
      ["null", null],
    ])("rejects a preferred width that is %s", (_label, preferredAuthoringWidth) => {
      localStorage.setItem(WORKSPACE_LAYOUT_STORAGE_KEY, "{}");
      vi.spyOn(JSON, "parse").mockReturnValueOnce({
        ...VALID_LAYOUT,
        preferredAuthoringWidth,
      });

      expect(readStoredWorkspaceLayout(localStorage)).toBeNull();
    });

    it.each(["false", 0, null])(
      "rejects collapse state with the wrong type: %j",
      (authoringCollapsed) => {
        localStorage.setItem(
          WORKSPACE_LAYOUT_STORAGE_KEY,
          JSON.stringify({ ...VALID_LAYOUT, authoringCollapsed }),
        );

        expect(readStoredWorkspaceLayout(localStorage)).toBeNull();
      },
    );

    it("returns null when storage reads fail", () => {
      const storage = {
        getItem: () => {
          throw new DOMException("storage disabled", "SecurityError");
        },
      } as Pick<Storage, "getItem"> as Storage;

      expect(readStoredWorkspaceLayout(storage)).toBeNull();
    });
  });

  describe("pane geometry", () => {
    it("defaults compact desktops to 360px and standard desktops to 420px", () => {
      expect(paneBoundsForWidth(1280).defaultWidth).toBe(360);
      expect(paneBoundsForWidth(1536).defaultWidth).toBe(420);
    });

    it("reserves the artifact minimum and caps the authoring maximum", () => {
      expect(paneBoundsForWidth(900)).toEqual({
        min: AUTHORING_MIN,
        max: AUTHORING_MIN,
        defaultWidth: AUTHORING_MIN,
        resizable: false,
      });
      expect(paneBoundsForWidth(1000)).toEqual({
        min: AUTHORING_MIN,
        max: AUTHORING_MIN,
        defaultWidth: AUTHORING_MIN,
        resizable: false,
      });
      expect(paneBoundsForWidth(1100).max).toBe(1100 - ARTIFACT_MIN);
      expect(paneBoundsForWidth(1100).resizable).toBe(true);
      expect(paneBoundsForWidth(1280).max).toBe(AUTHORING_MAX);
      expect(paneBoundsForWidth(1280).resizable).toBe(true);
    });

    it("clamps preferred widths into the supplied bounds", () => {
      const bounds = paneBoundsForWidth(1100);

      expect(clampAuthoringWidth(300, bounds)).toBe(360);
      expect(clampAuthoringWidth(420, bounds)).toBe(420);
      expect(clampAuthoringWidth(620, bounds)).toBe(460);
    });

    it.each([
      ["NaN", Number.NaN],
      ["positive infinity", Number.POSITIVE_INFINITY],
      ["negative infinity", Number.NEGATIVE_INFINITY],
    ])("uses the bounded default for a non-finite preferred width: %s", (
      _label,
      preferred,
    ) => {
      const bounds = paneBoundsForWidth(1536);

      expect(clampAuthoringWidth(preferred, bounds)).toBe(bounds.defaultWidth);
    });

    it.each([
      [
        "a non-finite minimum",
        { min: Number.NaN, max: 640, defaultWidth: 420 },
      ],
      [
        "a non-finite maximum",
        { min: 360, max: Number.POSITIVE_INFINITY, defaultWidth: 420 },
      ],
      ["an inverted range", { min: 640, max: 360, defaultWidth: 420 }],
    ])("falls back to the authoring minimum for %s", (_label, partialBounds) => {
      expect(
        clampAuthoringWidth(500, { ...partialBounds, resizable: true }),
      ).toBe(AUTHORING_MIN);
    });

    it("uses the lower bound when the fallback default is non-finite", () => {
      expect(
        clampAuthoringWidth(500, {
          min: 360,
          max: 460,
          defaultWidth: Number.NaN,
          resizable: true,
        }),
      ).toBe(460);
      expect(
        clampAuthoringWidth(Number.NaN, {
          min: 360,
          max: 460,
          defaultWidth: Number.NaN,
          resizable: true,
        }),
      ).toBe(360);
    });

    it.each([
      ["NaN", Number.NaN],
      ["positive infinity", Number.POSITIVE_INFINITY],
      ["negative infinity", Number.NEGATIVE_INFINITY],
      ["a negative width", -1],
      ["zero", 0],
    ])("treats %s workspace width as unmeasured", (_label, width) => {
      expect(paneBoundsForWidth(width)).toEqual(paneBoundsForWidth(0));
    });
  });

  describe("workspace hook", () => {
    it("initializes from valid storage", () => {
      localStorage.setItem(
        WORKSPACE_LAYOUT_STORAGE_KEY,
        JSON.stringify({ ...VALID_LAYOUT, authoringCollapsed: true }),
      );

      const { result } = renderHook(() =>
        useWorkspacePaneState({ workspaceWidth: 1600, sessionId: "s1" }),
      );

      expect(result.current.preferredAuthoringWidth).toBe(620);
      expect(result.current.effectiveAuthoringWidth).toBe(620);
      expect(result.current.authoringCollapsed).toBe(true);
    });

    it("uses the measured viewport default when storage is missing or unreadable", () => {
      const getItem = vi
        .spyOn(Storage.prototype, "getItem")
        .mockImplementationOnce(() => {
          throw new DOMException("storage disabled", "SecurityError");
        });
      const unreadable = renderHook(() =>
        useWorkspacePaneState({ workspaceWidth: 1536, sessionId: "s1" }),
      );
      getItem.mockRestore();
      const missing = renderHook(() =>
        useWorkspacePaneState({ workspaceWidth: 1280, sessionId: "s1" }),
      );

      expect(unreadable.result.current.preferredAuthoringWidth).toBe(420);
      expect(missing.result.current.preferredAuthoringWidth).toBe(360);
    });

    it("preserves a preferred width while clamping the effective width", () => {
      localStorage.setItem(
        WORKSPACE_LAYOUT_STORAGE_KEY,
        JSON.stringify(VALID_LAYOUT),
      );
      const { result, rerender } = renderHook(
        ({ width }) =>
          useWorkspacePaneState({ workspaceWidth: width, sessionId: "s1" }),
        { initialProps: { width: 1600 } },
      );

      expect(result.current.effectiveAuthoringWidth).toBe(620);
      rerender({ width: 1100 });

      expect(result.current.preferredAuthoringWidth).toBe(620);
      expect(result.current.effectiveAuthoringWidth).toBe(460);
      expect(storedLayout()?.preferredAuthoringWidth).toBe(620);
    });

    it("does not persist transient resize state", () => {
      const { result } = renderHook(() =>
        useWorkspacePaneState({ workspaceWidth: 1280, sessionId: "s1" }),
      );

      act(() => result.current.resizeTransient(520));

      expect(result.current.effectiveAuthoringWidth).toBe(520);
      expect(result.current.preferredAuthoringWidth).toBe(360);
      expect(localStorage.getItem(WORKSPACE_LAYOUT_STORAGE_KEY)).toBeNull();
    });

    it("commitResize persists its explicit final clamped width", () => {
      const { result } = renderHook(() =>
        useWorkspacePaneState({ workspaceWidth: 1100, sessionId: "s1" }),
      );

      act(() => {
        result.current.resizeTransient(400);
        result.current.commitResize(620);
      });

      expect(result.current.preferredAuthoringWidth).toBe(460);
      expect(result.current.effectiveAuthoringWidth).toBe(460);
      expect(storedLayout()).toEqual({
        version: 1,
        preferredAuthoringWidth: 460,
        authoringCollapsed: false,
      });
    });

    it("persists collapse and restore actions", () => {
      const { result } = renderHook(() =>
        useWorkspacePaneState({ workspaceWidth: 1536, sessionId: "s1" }),
      );

      act(() => result.current.setAuthoringCollapsed(true));
      expect(result.current.authoringCollapsed).toBe(true);
      expect(storedLayout()).toEqual({
        version: 1,
        preferredAuthoringWidth: 420,
        authoringCollapsed: true,
      });

      act(() => result.current.setAuthoringCollapsed(false));
      expect(result.current.authoringCollapsed).toBe(false);
      expect(storedLayout()).toEqual({
        version: 1,
        preferredAuthoringWidth: 420,
        authoringCollapsed: false,
      });
    });

    it("keeps in-memory state when a storage write fails", () => {
      vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
        throw new DOMException("quota exceeded", "QuotaExceededError");
      });
      const { result } = renderHook(() =>
        useWorkspacePaneState({ workspaceWidth: 1536, sessionId: "s1" }),
      );

      expect(() => {
        act(() => {
          result.current.commitResize(500);
          result.current.setAuthoringCollapsed(true);
        });
      }).not.toThrow();
      expect(result.current.preferredAuthoringWidth).toBe(500);
      expect(result.current.authoringCollapsed).toBe(true);
    });

    it("falls back to Graph when Spec or YAML becomes unavailable", () => {
      expect(nearestAvailableArtifactTab("spec", ["graph", "run"])).toBe(
        "graph",
      );
      expect(nearestAvailableArtifactTab("yaml", ["graph", "run"])).toBe(
        "graph",
      );

      let available: readonly ArtifactTab[] = ["graph", "spec", "yaml", "run"];
      const { result, rerender } = renderHook(() =>
        useWorkspacePaneState({
          workspaceWidth: 1536,
          sessionId: "s1",
          availableArtifactTabs: available,
        }),
      );
      act(() => result.current.selectArtifactTab("yaml"));
      expect(result.current.activeArtifactTab).toBe("yaml");

      available = ["graph", "run"];
      rerender();

      expect(result.current.activeArtifactTab).toBe("graph");
    });

    it("does not persist artifact or inspector state", () => {
      const { result } = renderHook(() =>
        useWorkspacePaneState({ workspaceWidth: 1536, sessionId: "s1" }),
      );

      act(() => {
        result.current.selectArtifactTab("run");
        result.current.openInspector("audit");
      });

      expect(result.current.activeArtifactTab).toBe("run");
      expect(result.current.activeInspectorTab).toBe("audit");
      expect(result.current.inspectorOpen).toBe(true);
      expect(localStorage.getItem(WORKSPACE_LAYOUT_STORAGE_KEY)).toBeNull();

      act(() => result.current.closeInspector());
      expect(result.current.activeInspectorTab).toBeNull();
      expect(result.current.inspectorOpen).toBe(false);
    });

    it("resets artifact and inspector state when the session changes", () => {
      let sessionId: string | null = "s1";
      const { result, rerender } = renderHook(() =>
        useWorkspacePaneState({ workspaceWidth: 1536, sessionId }),
      );
      act(() => {
        result.current.selectArtifactTab("run");
        result.current.openInspector("history");
      });

      sessionId = "s2";
      rerender();

      expect(result.current.activeArtifactTab).toBe("graph");
      expect(result.current.activeInspectorTab).toBeNull();
      expect(result.current.inspectorOpen).toBe(false);
    });

    it("exposes normalized ephemeral state to layout-effect consumers on the first transition commit", () => {
      interface Observation {
        sessionId: string;
        activeArtifactTab: ArtifactTab;
        activeInspectorTab: WorkspacePaneState["activeInspectorTab"];
      }

      interface ObserverProps {
        paneState: WorkspacePaneState;
        sessionId: string;
        observations: Observation[];
      }

      function LayoutEffectObserver({
        paneState,
        sessionId,
        observations,
      }: ObserverProps) {
        useLayoutEffect(() => {
          committedController = paneState;
          observations.push({
            sessionId,
            activeArtifactTab: paneState.activeArtifactTab,
            activeInspectorTab: paneState.activeInspectorTab,
          });
        }, [
          observations,
          paneState,
          paneState.activeArtifactTab,
          paneState.activeInspectorTab,
          sessionId,
        ]);
        return null;
      }

      interface HarnessProps {
        sessionId: string;
        availableArtifactTabs: readonly ArtifactTab[];
        observations: Observation[];
      }

      function ConsumerHarness({
        sessionId,
        availableArtifactTabs,
        observations,
      }: HarnessProps) {
        const paneState = useWorkspacePaneState({
          workspaceWidth: 1536,
          sessionId,
          availableArtifactTabs,
        });
        return createElement(LayoutEffectObserver, {
          paneState,
          sessionId,
          observations,
        });
      }

      let committedController: WorkspacePaneState | null = null;
      const observations: Observation[] = [];
      const allTabs: readonly ArtifactTab[] = [
        "graph",
        "spec",
        "yaml",
        "run",
      ];
      const { rerender } = render(
        createElement(ConsumerHarness, {
          sessionId: "s1",
          availableArtifactTabs: allTabs,
          observations,
        }),
      );

      act(() => {
        if (committedController === null) throw new Error("controller not committed");
        committedController.selectArtifactTab("run");
        committedController.openInspector("audit");
      });
      observations.length = 0;

      rerender(
        createElement(ConsumerHarness, {
          sessionId: "s2",
          availableArtifactTabs: allTabs,
          observations,
        }),
      );

      expect(observations[0]).toEqual({
        sessionId: "s2",
        activeArtifactTab: "graph",
        activeInspectorTab: null,
      });

      act(() => {
        if (committedController === null) throw new Error("controller not committed");
        committedController.selectArtifactTab("yaml");
      });
      observations.length = 0;

      rerender(
        createElement(ConsumerHarness, {
          sessionId: "s2",
          availableArtifactTabs: ["graph", "run"],
          observations,
        }),
      );

      expect(observations[0]).toEqual({
        sessionId: "s2",
        activeArtifactTab: "graph",
        activeInspectorTab: null,
      });
    });

    it("does not leak interrupted render values into committed callbacks", () => {
      const neverResolves = new Promise<never>(() => undefined);
      let committedController: WorkspacePaneState | null = null;
      let beginSuspendedTransition: (() => void) | null = null;
      let suspendedRenderObserved = false;

      function ConcurrentProbe() {
        const [workspaceWidth, setWorkspaceWidth] = useState(1536);
        const paneState = useWorkspacePaneState({
          workspaceWidth,
          sessionId: "s1",
        });
        useLayoutEffect(() => {
          committedController = paneState;
          beginSuspendedTransition = () => {
            startTransition(() => setWorkspaceWidth(1100));
          };
        }, [paneState]);

        if (workspaceWidth === 1100) {
          suspendedRenderObserved = true;
          throw neverResolves;
        }
        return null;
      }

      render(
        createElement(
          Suspense,
          { fallback: null },
          createElement(ConcurrentProbe),
        ),
      );

      act(() => {
        if (beginSuspendedTransition === null) {
          throw new Error("transition trigger not committed");
        }
        beginSuspendedTransition();
      });
      expect(suspendedRenderObserved).toBe(true);

      act(() => {
        if (committedController === null) throw new Error("controller not committed");
        committedController.commitResize(620);
      });

      expect(storedLayout()?.preferredAuthoringWidth).toBe(620);
    });

    it("publishes the new session before descendant layout-effect actions", () => {
      interface DescendantProps {
        paneState: WorkspacePaneState;
        sessionId: string;
      }

      function SessionLayoutAction({ paneState, sessionId }: DescendantProps) {
        const { selectArtifactTab } = paneState;
        useLayoutEffect(() => {
          if (sessionId === "s2") selectArtifactTab("run");
        }, [selectArtifactTab, sessionId]);
        return createElement(
          "output",
          { "data-testid": "active-artifact" },
          paneState.activeArtifactTab,
        );
      }

      function SessionHarness({ sessionId }: { sessionId: string }) {
        const paneState = useWorkspacePaneState({
          workspaceWidth: 1536,
          sessionId,
        });
        return createElement(SessionLayoutAction, { paneState, sessionId });
      }

      const view = render(createElement(SessionHarness, { sessionId: "s1" }));
      view.rerender(createElement(SessionHarness, { sessionId: "s2" }));

      expect(
        view.getByTestId("active-artifact").textContent,
      ).toBe("run");
    });

    it("publishes new bounds before descendant layout-effect resize commits", () => {
      interface DescendantProps {
        paneState: WorkspacePaneState;
        workspaceWidth: number;
      }

      function ResizeLayoutAction({
        paneState,
        workspaceWidth,
      }: DescendantProps) {
        const { commitResize } = paneState;
        useLayoutEffect(() => {
          if (workspaceWidth === 1100) commitResize(620);
        }, [commitResize, workspaceWidth]);
        return null;
      }

      function ResizeHarness({ workspaceWidth }: { workspaceWidth: number }) {
        const paneState = useWorkspacePaneState({
          workspaceWidth,
          sessionId: "s1",
        });
        return createElement(ResizeLayoutAction, {
          paneState,
          workspaceWidth,
        });
      }

      const view = render(
        createElement(ResizeHarness, { workspaceWidth: 1536 }),
      );
      view.rerender(createElement(ResizeHarness, { workspaceWidth: 1100 }));

      expect(storedLayout()?.preferredAuthoringWidth).toBe(460);
    });

    it.each([
      ["NaN", Number.NaN],
      ["positive infinity", Number.POSITIVE_INFINITY],
      ["negative infinity", Number.NEGATIVE_INFINITY],
      ["a negative width", -1],
      ["zero", 0],
    ])("ignores transient and committed resize values that are %s", (_label, width) => {
      localStorage.setItem(
        WORKSPACE_LAYOUT_STORAGE_KEY,
        JSON.stringify(VALID_LAYOUT),
      );
      const { result } = renderHook(() =>
        useWorkspacePaneState({ workspaceWidth: 1536, sessionId: "s1" }),
      );

      act(() => result.current.resizeTransient(500));
      act(() => {
        result.current.resizeTransient(width);
        result.current.commitResize(width);
      });

      expect(result.current.preferredAuthoringWidth).toBe(620);
      expect(result.current.effectiveAuthoringWidth).toBe(620);
      expect(storedLayout()).toEqual(VALID_LAYOUT);
    });

    it("keeps derived bounds and availability stable during transient resize", () => {
      const availableArtifactTabs: readonly ArtifactTab[] = ["run"];
      const { result } = renderHook(() =>
        useWorkspacePaneState({
          workspaceWidth: 1536,
          sessionId: "s1",
          availableArtifactTabs,
        }),
      );
      const paneBounds = result.current.paneBounds;
      const normalizedTabs = result.current.availableArtifactTabs;

      act(() => result.current.resizeTransient(500));

      expect(result.current.paneBounds).toBe(paneBounds);
      expect(result.current.availableArtifactTabs).toBe(normalizedTabs);
    });

    it.each([
      ["NaN", Number.NaN],
      ["positive infinity", Number.POSITIVE_INFINITY],
      ["negative infinity", Number.NEGATIVE_INFINITY],
      ["a negative width", -1],
      ["zero", 0],
    ])("does not clamp or persist preference for an unmeasured %s observer width", (
      _label,
      width,
    ) => {
      localStorage.setItem(
        WORKSPACE_LAYOUT_STORAGE_KEY,
        JSON.stringify(VALID_LAYOUT),
      );
      const { result } = renderHook(() =>
        useWorkspacePaneState({ workspaceWidth: width, sessionId: "s1" }),
      );

      expect(result.current.paneBounds).toEqual(paneBoundsForWidth(0));
      expect(result.current.preferredAuthoringWidth).toBe(620);
      expect(result.current.effectiveAuthoringWidth).toBe(620);
      expect(storedLayout()).toEqual(VALID_LAYOUT);
    });

    it("enforces Graph availability at the hook boundary", () => {
      const { result } = renderHook(() =>
        useWorkspacePaneState({
          workspaceWidth: 1536,
          sessionId: "s1",
          availableArtifactTabs: ["run"],
        }),
      );

      expect(result.current.availableArtifactTabs).toEqual(["graph", "run"]);
      act(() => result.current.selectArtifactTab("yaml"));
      expect(result.current.activeArtifactTab).toBe("graph");
    });

    it("treats an initial zero width as unmeasured", () => {
      localStorage.setItem(
        WORKSPACE_LAYOUT_STORAGE_KEY,
        JSON.stringify(VALID_LAYOUT),
      );
      const { result, rerender } = renderHook(
        ({ width }) =>
          useWorkspacePaneState({ workspaceWidth: width, sessionId: "s1" }),
        { initialProps: { width: 0 } },
      );

      expect(result.current.preferredAuthoringWidth).toBe(620);
      expect(result.current.effectiveAuthoringWidth).toBe(620);
      rerender({ width: 1100 });

      expect(result.current.preferredAuthoringWidth).toBe(620);
      expect(result.current.effectiveAuthoringWidth).toBe(460);
      expect(storedLayout()?.preferredAuthoringWidth).toBe(620);
    });

    it("keeps every action identity stable across renders", () => {
      let workspaceWidth = 1536;
      const { result, rerender } = renderHook(() =>
        useWorkspacePaneState({ workspaceWidth, sessionId: "s1" }),
      );
      const actions = {
        resizeTransient: result.current.resizeTransient,
        commitResize: result.current.commitResize,
        setAuthoringCollapsed: result.current.setAuthoringCollapsed,
        selectArtifactTab: result.current.selectArtifactTab,
        openInspector: result.current.openInspector,
        closeInspector: result.current.closeInspector,
      };

      workspaceWidth = 1100;
      rerender();

      expect(result.current.resizeTransient).toBe(actions.resizeTransient);
      expect(result.current.commitResize).toBe(actions.commitResize);
      expect(result.current.setAuthoringCollapsed).toBe(
        actions.setAuthoringCollapsed,
      );
      expect(result.current.selectArtifactTab).toBe(actions.selectArtifactTab);
      expect(result.current.openInspector).toBe(actions.openInspector);
      expect(result.current.closeInspector).toBe(actions.closeInspector);
    });
  });
});
