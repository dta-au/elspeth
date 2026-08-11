import {
  createContext,
  type MutableRefObject,
  type ReactNode,
  useCallback,
  useContext,
  useMemo,
  useRef,
} from "react";

import type { WorkspacePaneState } from "./useWorkspacePaneState";
import type {
  ArtifactTab,
  AvailableArtifactTabs,
  InspectorTab,
} from "./workspaceTypes";

export interface WorkspaceControllerState {
  authoringCollapsed: boolean;
  availableArtifactTabs: AvailableArtifactTabs;
  activeArtifactTab: ArtifactTab;
  activeInspectorTab: InspectorTab | null;
  inspectorOpen: boolean;
}

export interface WorkspaceControllerActions {
  resizeTransient: WorkspacePaneState["resizeTransient"];
  commitResize: WorkspacePaneState["commitResize"];
  setAuthoringCollapsed: WorkspacePaneState["setAuthoringCollapsed"];
  selectArtifactTab: WorkspacePaneState["selectArtifactTab"];
  openInspector: (tab: InspectorTab, invoker: HTMLElement) => void;
  closeInspector: WorkspacePaneState["closeInspector"];
}

export interface WorkspacePaneController {
  state: WorkspaceControllerState;
  actions: WorkspaceControllerActions;
  inspectorInvokerRef: MutableRefObject<HTMLElement | null>;
}

export class WorkspacePaneContextError extends Error {
  constructor() {
    super(
      "useWorkspacePaneController must be rendered inside ComposerWorkspace; " +
        "workspace surfaces must share its single pane-state controller.",
    );
    this.name = "WorkspacePaneContextError";
  }
}

const WorkspacePaneContext = createContext<WorkspacePaneController | null>(
  null,
);

interface WorkspacePaneProviderProps {
  paneState: WorkspacePaneState;
  children: ReactNode;
}

export function WorkspacePaneProvider({
  paneState,
  children,
}: WorkspacePaneProviderProps) {
  const {
    authoringCollapsed,
    availableArtifactTabs,
    activeArtifactTab,
    activeInspectorTab,
    inspectorOpen,
    resizeTransient,
    commitResize,
    setAuthoringCollapsed,
    selectArtifactTab,
    openInspector: openPaneInspector,
    closeInspector,
  } = paneState;
  const inspectorInvokerRef = useRef<HTMLElement | null>(null);
  const openInspector = useCallback(
    (tab: InspectorTab, invoker: HTMLElement): void => {
      inspectorInvokerRef.current = invoker;
      openPaneInspector(tab);
    },
    [openPaneInspector],
  );

  const state = useMemo<WorkspaceControllerState>(
    () => ({
      authoringCollapsed,
      availableArtifactTabs,
      activeArtifactTab,
      activeInspectorTab,
      inspectorOpen,
    }),
    [
      activeArtifactTab,
      activeInspectorTab,
      authoringCollapsed,
      availableArtifactTabs,
      inspectorOpen,
    ],
  );
  const actions = useMemo<WorkspaceControllerActions>(
    () => ({
      resizeTransient,
      commitResize,
      setAuthoringCollapsed,
      selectArtifactTab,
      openInspector,
      closeInspector,
    }),
    [
      closeInspector,
      commitResize,
      openInspector,
      resizeTransient,
      selectArtifactTab,
      setAuthoringCollapsed,
    ],
  );
  const controller = useMemo<WorkspacePaneController>(
    () => ({ state, actions, inspectorInvokerRef }),
    [actions, state],
  );

  return (
    <WorkspacePaneContext.Provider value={controller}>
      {children}
    </WorkspacePaneContext.Provider>
  );
}

export function useWorkspacePaneController(): WorkspacePaneController {
  const controller = useContext(WorkspacePaneContext);
  if (controller === null) throw new WorkspacePaneContextError();
  return controller;
}
