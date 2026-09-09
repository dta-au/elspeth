export const ARTIFACT_TABS = [
  "graph",
  "spec",
  "yaml",
  "checks",
  "run",
] as const;

export type ArtifactTab = (typeof ARTIFACT_TABS)[number];

export type AvailableArtifactTabs = readonly ["graph", ...ArtifactTab[]];

/* Validation and audit left the inspector for the Checks artifact tab
   (ChecksView); the drawer now holds guided history alone. */
export type InspectorTab = "history";

export interface PaneBounds {
  min: number;
  max: number;
  defaultWidth: number;
  resizable: boolean;
}

export interface StoredWorkspaceLayoutV1 {
  version: 1;
  preferredAuthoringWidth: number;
  authoringCollapsed: boolean;
}
