import {
  createContext,
  type ReactNode,
  useContext,
  useMemo,
} from "react";

import type { WorkspacePaneState } from "./useWorkspacePaneState";
import type {
  ArtifactTab,
  AvailableArtifactTabs,
} from "./workspaceTypes";

export interface WorkspaceControllerState {
  authoringCollapsed: boolean;
  availableArtifactTabs: AvailableArtifactTabs;
  activeArtifactTab: ArtifactTab;
  artifactVisible: boolean;
  authoringVisible: boolean;
}

export interface WorkspaceControllerActions {
  resizeTransient: WorkspacePaneState["resizeTransient"];
  commitResize: WorkspacePaneState["commitResize"];
  setAuthoringCollapsed: WorkspacePaneState["setAuthoringCollapsed"];
  selectArtifactTab: WorkspacePaneState["selectArtifactTab"];
  showPipeline: () => void;
  showCompose: () => void;
}

export interface WorkspacePaneController {
  state: WorkspaceControllerState;
  actions: WorkspaceControllerActions;
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

const NOOP_SHOW_PIPELINE = (): void => undefined;
const NOOP_SHOW_COMPOSE = (): void => undefined;

interface WorkspacePaneProviderProps {
  paneState: WorkspacePaneState;
  artifactVisible?: boolean;
  authoringVisible?: boolean;
  showPipeline?: () => void;
  showCompose?: () => void;
  children: ReactNode;
}

export function WorkspacePaneProvider({
  paneState,
  artifactVisible = true,
  authoringVisible = true,
  showPipeline = NOOP_SHOW_PIPELINE,
  showCompose = NOOP_SHOW_COMPOSE,
  children,
}: WorkspacePaneProviderProps) {
  const {
    authoringCollapsed,
    availableArtifactTabs,
    activeArtifactTab,
    resizeTransient,
    commitResize,
    setAuthoringCollapsed,
    selectArtifactTab,
  } = paneState;

  const state = useMemo<WorkspaceControllerState>(
    () => ({
      authoringCollapsed,
      availableArtifactTabs,
      activeArtifactTab,
      artifactVisible,
      authoringVisible,
    }),
    [
      activeArtifactTab,
      authoringCollapsed,
      availableArtifactTabs,
      artifactVisible,
      authoringVisible,
    ],
  );
  const actions = useMemo<WorkspaceControllerActions>(
    () => ({
      resizeTransient,
      commitResize,
      setAuthoringCollapsed,
      selectArtifactTab,
      showPipeline,
      showCompose,
    }),
    [
      commitResize,
      resizeTransient,
      selectArtifactTab,
      setAuthoringCollapsed,
      showCompose,
      showPipeline,
    ],
  );
  const controller = useMemo<WorkspacePaneController>(
    () => ({ state, actions }),
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
