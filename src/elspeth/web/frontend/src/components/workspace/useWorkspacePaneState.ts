import { useCallback, useEffect, useRef, useState } from "react";

import {
  ARTIFACT_TABS,
  type ArtifactTab,
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
  availableArtifactTabs: readonly ArtifactTab[];
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

export function paneBoundsForWidth(workspaceWidth: number): PaneBounds {
  const max = Math.max(
    AUTHORING_MIN,
    Math.min(AUTHORING_MAX, workspaceWidth - ARTIFACT_MIN),
  );
  return {
    min: AUTHORING_MIN,
    max,
    defaultWidth:
      workspaceWidth < STANDARD_DESKTOP_MIN ? AUTHORING_MIN : 420,
    resizable:
      workspaceWidth >= AUTHORING_MIN + ARTIFACT_MIN && max > AUTHORING_MIN,
  };
}

export function clampAuthoringWidth(
  preferred: number,
  bounds: PaneBounds,
): number {
  return Math.min(bounds.max, Math.max(bounds.min, preferred));
}

export function nearestAvailableArtifactTab(
  requested: ArtifactTab,
  available: readonly ArtifactTab[],
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
    workspaceWidth > 0
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
  const [activeArtifactTab, setActiveArtifactTab] =
    useState<ArtifactTab>("graph");
  const [activeInspectorTab, setActiveInspectorTab] =
    useState<InspectorTab | null>(null);

  const paneBounds = paneBoundsForWidth(workspaceWidth);
  const effectiveCandidate =
    transientAuthoringWidth ?? preferredAuthoringWidth;
  const effectiveAuthoringWidth =
    workspaceWidth > 0
      ? clampAuthoringWidth(effectiveCandidate, paneBounds)
      : effectiveCandidate;

  const storageRef = useRef(initialLayout.storage);
  const boundsRef = useRef(paneBounds);
  const preferredWidthRef = useRef(preferredAuthoringWidth);
  const collapsedRef = useRef(authoringCollapsed);
  const availableTabsRef = useRef(availableArtifactTabs);
  boundsRef.current = paneBounds;
  preferredWidthRef.current = preferredAuthoringWidth;
  collapsedRef.current = authoringCollapsed;
  availableTabsRef.current = availableArtifactTabs;

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
    setTransientAuthoringWidth(clampAuthoringWidth(width, boundsRef.current));
  }, []);

  const commitResize = useCallback(
    (finalWidth: number): void => {
      const committedWidth = clampAuthoringWidth(
        finalWidth,
        boundsRef.current,
      );
      preferredWidthRef.current = committedWidth;
      setPreferredAuthoringWidth(committedWidth);
      setTransientAuthoringWidth(null);
      persist(committedWidth, collapsedRef.current);
    },
    [persist],
  );

  const setAuthoringCollapsed = useCallback(
    (collapsed: boolean): void => {
      collapsedRef.current = collapsed;
      setAuthoringCollapsedState(collapsed);
      persist(preferredWidthRef.current, collapsed);
    },
    [persist],
  );

  const selectArtifactTab = useCallback((tab: ArtifactTab): void => {
    setActiveArtifactTab(
      nearestAvailableArtifactTab(tab, availableTabsRef.current),
    );
  }, []);

  const openInspector = useCallback((tab: InspectorTab): void => {
    setActiveInspectorTab(tab);
  }, []);

  const closeInspector = useCallback((): void => {
    setActiveInspectorTab(null);
  }, []);

  useEffect(() => {
    setActiveArtifactTab((current) =>
      nearestAvailableArtifactTab(current, availableArtifactTabs),
    );
  }, [availableArtifactTabs]);

  useEffect(() => {
    setActiveArtifactTab("graph");
    setActiveInspectorTab(null);
  }, [sessionId]);

  return {
    paneBounds,
    preferredAuthoringWidth,
    effectiveAuthoringWidth,
    authoringCollapsed,
    availableArtifactTabs,
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
