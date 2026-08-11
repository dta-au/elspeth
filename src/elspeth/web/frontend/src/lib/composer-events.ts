import type { ArtifactTab } from "@/components/workspace/workspaceTypes";

export const OPEN_GRAPH_MODAL_EVENT = "elspeth-open-graph-modal";
export const OPEN_IMPORT_YAML_MODAL_EVENT = "elspeth-open-import-yaml-modal";
export const OPEN_CATALOG_EVENT = "open-catalog";
export const REQUEST_RUN_EVENT = "elspeth-request-run";
export const REQUEST_ARTIFACT_VIEW_EVENT = "elspeth:request-artifact-view";
export const FOCUS_AUTHORING_EVENT = "elspeth:focus-authoring";

export interface RequestArtifactViewDetail {
  tab: ArtifactTab;
  focusMode: boolean;
  sessionId: string | null;
}

let artifactIntentSequence = 0;

export function claimArtifactViewIntent(): number {
  artifactIntentSequence += 1;
  return artifactIntentSequence;
}

export function isCurrentArtifactViewIntent(intent: number): boolean {
  return intent === artifactIntentSequence;
}

export function dispatchArtifactViewIntent(
  detail: RequestArtifactViewDetail,
): void {
  claimArtifactViewIntent();
  window.dispatchEvent(
    new CustomEvent<RequestArtifactViewDetail>(REQUEST_ARTIFACT_VIEW_EVENT, {
      detail,
    }),
  );
}

export function dispatchClaimedArtifactViewIntent(
  intent: number,
  detail: RequestArtifactViewDetail,
): void {
  if (!isCurrentArtifactViewIntent(intent)) return;
  window.dispatchEvent(
    new CustomEvent<RequestArtifactViewDetail>(REQUEST_ARTIFACT_VIEW_EVENT, {
      detail,
    }),
  );
}
