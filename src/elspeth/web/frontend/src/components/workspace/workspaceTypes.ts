export const ARTIFACT_TABS = ["graph", "spec", "yaml", "run"] as const;

export type ArtifactTab = (typeof ARTIFACT_TABS)[number];

export type InspectorTab = "validation" | "audit" | "history";

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
