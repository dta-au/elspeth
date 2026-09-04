import { CompletionBar } from "@/components/composer/CompletionBar";

export interface WorkspaceActionCapabilities {
  /* `completion` also carries Import YAML: the bar's import trigger rides
     inside CompletionBar, and everywhere this bar mounts the two
     availability facts are the same one (!guidedBuildActive — the shared,
     tutorial, and empty-landing views never render this bar).

     The catalog capability left this bar entirely (2026-08-15 UX review):
     the More-actions popover it gated held a single item, so the Plugin
     catalog trigger moved to the artifact-workspace toolbar
     (ArtifactWorkspace's `catalogAvailable` prop) and the popover — state,
     roving-focus keyboard code, and CSS — was deleted.

     The Validation and Audit status chips left next: their live status is
     now the Checks tab's badge and their content renders inline in that
     tab's panel (ChecksView), so the bar carries completion actions alone. */
  completion: boolean;
}

interface WorkspaceActionBarProps {
  capabilities: WorkspaceActionCapabilities;
}

export function WorkspaceActionBar({
  capabilities,
}: WorkspaceActionBarProps): JSX.Element {
  return (
    <div
      className="workspace-action-bar"
      role="group"
      aria-label="Workspace actions"
    >
      {capabilities.completion && <CompletionBar />}
    </div>
  );
}
