import {
  useCallback,
  useInsertionEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  ARTIFACT_TABS,
  type ArtifactTab,
  type AvailableArtifactTabs,
  type InspectorTab,
  type PaneBounds,
  type StoredWorkspaceLayoutV1,
} from "./workspaceTypes";

export const WORKSPACE_LAYOUT_STORAGE_KEY =
  "elspeth_composer_workspace_layout_v1";
export const AUTHORING_MIN = 360;
export const AUTHORING_MAX = 640;
export const ARTIFACT_MIN = 640;
export const STANDARD_DESKTOP_MIN = 1536;

const STORED_LAYOUT_KEYS = new Set<PropertyKey>([
  "version",
  "preferredAuthoringWidth",
  "authoringCollapsed",
]);

export interface UseWorkspacePaneStateOptions {
  workspaceWidth: number;
  sessionId: string | null;
  availableArtifactTabs?: readonly ArtifactTab[];
}

export interface WorkspacePaneState {
  paneBounds: PaneBounds;
  preferredAuthoringWidth: number;
  effectiveAuthoringWidth: number;
  authoringCollapsed: boolean;
  availableArtifactTabs: AvailableArtifactTabs;
  activeArtifactTab: ArtifactTab;
  activeInspectorTab: InspectorTab | null;
  inspectorOpen: boolean;
  resizeTransient: (width: number) => void;
  commitResize: (finalWidth: number) => void;
  setAuthoringCollapsed: (collapsed: boolean) => void;
  selectArtifactTab: (tab: ArtifactTab) => void;
  openInspector: (tab: InspectorTab) => void;
  closeInspector: () => void;
}

interface InitialWorkspaceLayout {
  storage: Storage | null;
  preferredAuthoringWidth: number;
  authoringCollapsed: boolean;
}

interface EphemeralPaneState {
  sessionId: string | null;
  activeArtifactTab: ArtifactTab;
  activeInspectorTab: InspectorTab | null;
}

interface CommittedPaneValues {
  paneBounds: PaneBounds;
  workspaceMeasured: boolean;
  preferredAuthoringWidth: number;
  authoringCollapsed: boolean;
  availableArtifactTabs: AvailableArtifactTabs;
  sessionId: string | null;
}

function isMeasuredWidth(width: number): boolean {
  return Number.isFinite(width) && width > 0;
}

function artifactTabsKeyWithGraph(
  availableArtifactTabs: readonly ArtifactTab[],
): string {
  return ARTIFACT_TABS.filter(
    (tab) => tab === "graph" || availableArtifactTabs.includes(tab),
  ).join("|");
}

function artifactTabsFromKey(key: string): AvailableArtifactTabs {
  const available = new Set(key.split("|"));
  return [
    "graph",
    ...ARTIFACT_TABS.filter(
      (tab) => tab !== "graph" && available.has(tab),
    ),
  ];
}

function defaultEphemeralPaneState(
  sessionId: string | null,
): EphemeralPaneState {
  return {
    sessionId,
    activeArtifactTab: "graph",
    activeInspectorTab: null,
  };
}

export function paneBoundsForWidth(workspaceWidth: number): PaneBounds {
  const measuredWidth = isMeasuredWidth(workspaceWidth) ? workspaceWidth : 0;
  const max = Math.max(
    AUTHORING_MIN,
    Math.min(AUTHORING_MAX, measuredWidth - ARTIFACT_MIN),
  );
  return {
    min: AUTHORING_MIN,
    max,
    defaultWidth:
      measuredWidth < STANDARD_DESKTOP_MIN ? AUTHORING_MIN : 420,
    resizable:
      measuredWidth >= AUTHORING_MIN + ARTIFACT_MIN && max > AUTHORING_MIN,
  };
}

export function clampAuthoringWidth(
  preferred: number,
  bounds: PaneBounds,
): number {
  // Bounds describe owned layout geometry. If that contract is corrupt, fail
  // closed to the global minimum; an invalid preference uses the bounded
  // default and is never allowed to propagate NaN into state or persistence.
  if (
    !Number.isFinite(bounds.min) ||
    !Number.isFinite(bounds.max) ||
    bounds.max < bounds.min
  ) {
    return AUTHORING_MIN;
  }
  const boundedDefault = Number.isFinite(bounds.defaultWidth)
    ? Math.min(bounds.max, Math.max(bounds.min, bounds.defaultWidth))
    : bounds.min;
  const candidate = Number.isFinite(preferred) ? preferred : boundedDefault;
  return Math.min(bounds.max, Math.max(bounds.min, candidate));
}

export function nearestAvailableArtifactTab(
  requested: ArtifactTab,
  available: AvailableArtifactTabs,
): ArtifactTab {
  return available.includes(requested) ? requested : "graph";
}

function isPlainRecord(value: unknown): value is Record<PropertyKey, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

export function readStoredWorkspaceLayout(
  storage: Storage,
): StoredWorkspaceLayoutV1 | null {
  try {
    const raw = storage.getItem(WORKSPACE_LAYOUT_STORAGE_KEY);
    if (raw === null) return null;

    const value: unknown = JSON.parse(raw);
    if (!isPlainRecord(value)) return null;

    const keys = Reflect.ownKeys(value);
    if (
      keys.length !== STORED_LAYOUT_KEYS.size ||
      !keys.every((key) => STORED_LAYOUT_KEYS.has(key)) ||
      value.version !== 1 ||
      typeof value.preferredAuthoringWidth !== "number" ||
      !Number.isFinite(value.preferredAuthoringWidth) ||
      value.preferredAuthoringWidth < AUTHORING_MIN ||
      value.preferredAuthoringWidth > AUTHORING_MAX ||
      typeof value.authoringCollapsed !== "boolean"
    ) {
      return null;
    }

    return {
      version: 1,
      preferredAuthoringWidth: value.preferredAuthoringWidth,
      authoringCollapsed: value.authoringCollapsed,
    };
  } catch {
    return null;
  }
}

function browserLocalStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

function initialWorkspaceLayout(workspaceWidth: number): InitialWorkspaceLayout {
  const storage = browserLocalStorage();
  const stored = storage === null ? null : readStoredWorkspaceLayout(storage);
  if (stored !== null) {
    return {
      storage,
      preferredAuthoringWidth: stored.preferredAuthoringWidth,
      authoringCollapsed: stored.authoringCollapsed,
    };
  }

  const defaultSourceWidth =
    isMeasuredWidth(workspaceWidth)
      ? workspaceWidth
      : typeof window === "undefined"
        ? STANDARD_DESKTOP_MIN
        : window.innerWidth;
  return {
    storage,
    preferredAuthoringWidth:
      paneBoundsForWidth(defaultSourceWidth).defaultWidth,
    authoringCollapsed: false,
  };
}

export function useWorkspacePaneState({
  workspaceWidth,
  sessionId,
  availableArtifactTabs = ARTIFACT_TABS,
}: UseWorkspacePaneStateOptions): WorkspacePaneState {
  const availableArtifactTabsKey = artifactTabsKeyWithGraph(
    availableArtifactTabs,
  );
  const normalizedAvailableArtifactTabs = useMemo(
    () => artifactTabsFromKey(availableArtifactTabsKey),
    [availableArtifactTabsKey],
  );
  const workspaceMeasured = isMeasuredWidth(workspaceWidth);
  const [initialLayout] = useState(() => initialWorkspaceLayout(workspaceWidth));
  const [preferredAuthoringWidth, setPreferredAuthoringWidth] = useState(
    initialLayout.preferredAuthoringWidth,
  );
  const [transientAuthoringWidth, setTransientAuthoringWidth] = useState<
    number | null
  >(null);
  const [authoringCollapsed, setAuthoringCollapsedState] = useState(
    initialLayout.authoringCollapsed,
  );
  const [ephemeralPaneState, setEphemeralPaneState] =
    useState<EphemeralPaneState>(() => defaultEphemeralPaneState(sessionId));

  const paneBounds = useMemo(
    () => paneBoundsForWidth(workspaceWidth),
    [workspaceWidth],
  );
  const effectiveCandidate =
    transientAuthoringWidth ?? preferredAuthoringWidth;
  const effectiveAuthoringWidth =
    workspaceMeasured
      ? clampAuthoringWidth(effectiveCandidate, paneBounds)
      : effectiveCandidate;
  const ephemeralStateForSession =
    ephemeralPaneState.sessionId === sessionId
      ? ephemeralPaneState
      : defaultEphemeralPaneState(sessionId);
  const activeArtifactTab = nearestAvailableArtifactTab(
    ephemeralStateForSession.activeArtifactTab,
    normalizedAvailableArtifactTabs,
  );
  const activeInspectorTab = ephemeralStateForSession.activeInspectorTab;

  const storageRef = useRef(initialLayout.storage);
  const committedValuesRef = useRef<CommittedPaneValues>({
    paneBounds,
    workspaceMeasured,
    preferredAuthoringWidth,
    authoringCollapsed,
    availableArtifactTabs: normalizedAvailableArtifactTabs,
    sessionId,
  });

  const persist = useCallback(
    (preferredWidth: number, collapsed: boolean): void => {
      try {
        storageRef.current?.setItem(
          WORKSPACE_LAYOUT_STORAGE_KEY,
          JSON.stringify({
            version: 1,
            preferredAuthoringWidth: preferredWidth,
            authoringCollapsed: collapsed,
          } satisfies StoredWorkspaceLayoutV1),
        );
      } catch {
        // Browser storage is optional UI convenience; in-memory state remains authoritative.
      }
    },
    [],
  );

  const resizeTransient = useCallback((width: number): void => {
    const committed = committedValuesRef.current;
    if (!committed.workspaceMeasured || !isMeasuredWidth(width)) return;
    setTransientAuthoringWidth(
      clampAuthoringWidth(width, committed.paneBounds),
    );
  }, []);

  const commitResize = useCallback(
    (finalWidth: number): void => {
      const committed = committedValuesRef.current;
      if (!committed.workspaceMeasured || !isMeasuredWidth(finalWidth)) {
        setTransientAuthoringWidth(null);
        return;
      }
      const committedWidth = clampAuthoringWidth(
        finalWidth,
        committed.paneBounds,
      );
      committedValuesRef.current = {
        ...committed,
        preferredAuthoringWidth: committedWidth,
      };
      setPreferredAuthoringWidth(committedWidth);
      setTransientAuthoringWidth(null);
      persist(committedWidth, committed.authoringCollapsed);
    },
    [persist],
  );

  const setAuthoringCollapsed = useCallback(
    (collapsed: boolean): void => {
      const committed = committedValuesRef.current;
      committedValuesRef.current = {
        ...committed,
        authoringCollapsed: collapsed,
      };
      setAuthoringCollapsedState(collapsed);
      persist(committed.preferredAuthoringWidth, collapsed);
    },
    [persist],
  );

  const selectArtifactTab = useCallback((tab: ArtifactTab): void => {
    const committed = committedValuesRef.current;
    setEphemeralPaneState((current) => {
      const sessionState =
        current.sessionId === committed.sessionId
          ? current
          : defaultEphemeralPaneState(committed.sessionId);
      return {
        ...sessionState,
        activeArtifactTab: nearestAvailableArtifactTab(
          tab,
          committed.availableArtifactTabs,
        ),
      };
    });
  }, []);

  const openInspector = useCallback((tab: InspectorTab): void => {
    const committed = committedValuesRef.current;
    setEphemeralPaneState((current) => {
      const sessionState =
        current.sessionId === committed.sessionId
          ? current
          : defaultEphemeralPaneState(committed.sessionId);
      return {
        ...sessionState,
        activeInspectorTab: tab,
      };
    });
  }, []);

  const closeInspector = useCallback((): void => {
    const committed = committedValuesRef.current;
    setEphemeralPaneState((current) => {
      const sessionState =
        current.sessionId === committed.sessionId
          ? current
          : defaultEphemeralPaneState(committed.sessionId);
      if (sessionState.activeInspectorTab === null) return sessionState;
      return {
        ...sessionState,
        activeInspectorTab: null,
      };
    });
  }, []);

  // Stable actions can run from descendant layout effects. Publish only the
  // newly committed values here, before those effects; state normalization
  // stays in the layout effect below and speculative renders never write it.
  useInsertionEffect(() => {
    committedValuesRef.current = {
      paneBounds,
      workspaceMeasured,
      preferredAuthoringWidth,
      authoringCollapsed,
      availableArtifactTabs: normalizedAvailableArtifactTabs,
      sessionId,
    };
  }, [
    authoringCollapsed,
    normalizedAvailableArtifactTabs,
    paneBounds,
    preferredAuthoringWidth,
    sessionId,
    workspaceMeasured,
  ]);

  useLayoutEffect(() => {
    setEphemeralPaneState((current) => {
      if (current.sessionId !== sessionId) {
        return defaultEphemeralPaneState(sessionId);
      }
      const normalizedTab = nearestAvailableArtifactTab(
        current.activeArtifactTab,
        normalizedAvailableArtifactTabs,
      );
      if (normalizedTab === current.activeArtifactTab) return current;
      return {
        ...current,
        activeArtifactTab: normalizedTab,
      };
    });
  }, [
    normalizedAvailableArtifactTabs,
    sessionId,
  ]);

  return {
    paneBounds,
    preferredAuthoringWidth,
    effectiveAuthoringWidth,
    authoringCollapsed,
    availableArtifactTabs: normalizedAvailableArtifactTabs,
    activeArtifactTab,
    activeInspectorTab,
    inspectorOpen: activeInspectorTab !== null,
    resizeTransient,
    commitResize,
    setAuthoringCollapsed,
    selectArtifactTab,
    openInspector,
    closeInspector,
  };
}
