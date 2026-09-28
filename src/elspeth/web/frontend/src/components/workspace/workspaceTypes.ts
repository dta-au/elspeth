export const ARTIFACT_TABS = [
  "graph",
  "approvals",
  "spec",
  "yaml",
  "checks",
  "run",
] as const;

export type ArtifactTab = (typeof ARTIFACT_TABS)[number];

export type AvailableArtifactTabs = readonly ["graph", ...ArtifactTab[]];

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
