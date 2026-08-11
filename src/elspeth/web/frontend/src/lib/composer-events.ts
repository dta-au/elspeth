import type { ArtifactTab } from "@/components/workspace/workspaceTypes";

export const OPEN_GRAPH_MODAL_EVENT = "elspeth-open-graph-modal";
export const OPEN_IMPORT_YAML_MODAL_EVENT = "elspeth-open-import-yaml-modal";
export const OPEN_CATALOG_EVENT = "open-catalog";
export const REQUEST_RUN_EVENT = "elspeth-request-run";
export const REQUEST_ARTIFACT_VIEW_EVENT = "elspeth:request-artifact-view";

export interface RequestArtifactViewDetail {
  tab: ArtifactTab;
  focusMode: boolean;
  sessionId: string | null;
}
