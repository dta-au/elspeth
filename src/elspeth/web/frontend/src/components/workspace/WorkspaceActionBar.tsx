import { CompletionBar } from "@/components/composer/CompletionBar";
import { Button } from "@/components/ui";
import { useAuditReadinessStore } from "@/stores/auditReadinessStore";
import { useExecutionStore } from "@/stores/executionStore";
import { useSessionStore } from "@/stores/sessionStore";
import { useWorkspacePaneController } from "./WorkspacePaneContext";
import {
  projectAuditWorkspaceStatus,
  projectValidationWorkspaceStatus,
  type WorkspaceStatus,
} from "./workspaceStatus";

export interface WorkspaceActionCapabilities {
  /* `completion` also carries Import YAML: the bar's import trigger rides
     inside CompletionBar, and everywhere this bar mounts the two
     availability facts are the same one (!guidedBuildActive — the shared,
     tutorial, and empty-landing views never render this bar).

     The catalog capability left this bar entirely (2026-08-15 UX review):
     the More-actions popover it gated held a single item, so the Plugin
     catalog trigger moved to the artifact-workspace toolbar
     (ArtifactWorkspace's `catalogAvailable` prop) and the popover — state,
     roving-focus keyboard code, and CSS — was deleted. */
  completion: boolean;
}

interface WorkspaceActionBarProps {
  capabilities: WorkspaceActionCapabilities;
}

/* The visible separator and the accessible name are ONE expression of the same
   two strings (elspeth-a41fd9d32b). Rendering the pair bare made it read as a
   single Title Case phrase — "Validation Passed" — and the colon lived only in
   workspaceStatus.ts's `accessibleLabel`, so the visible string was not
   contained in the programmatic name: a WCAG 2.5.3 Label in Name failure for
   speech input. Composing the name here from the SAME `kind` and `status.text`
   that are rendered is what makes the two incapable of drifting apart again;
   the output string is byte-identical to the old `accessibleLabel`, which the
   tests/e2e page object locates on (/^Validation: /, composer-page.ts:109). */
function StatusButton({
  kind,
  status,
  onClick,
}: {
  kind: "Validation" | "Audit";
  status: WorkspaceStatus;
  onClick: (event: React.MouseEvent<HTMLButtonElement>) => void;
}): JSX.Element {
  return (
    <Button
      compact
      className="workspace-status-control"
      data-tone={status.tone}
      aria-label={`${kind}: ${status.text}`}
      onClick={onClick}
    >
      <span>{kind}:</span>
      <span>{status.text}</span>
    </Button>
  );
}

export function WorkspaceActionBar({
  capabilities,
}: WorkspaceActionBarProps): JSX.Element {
  const { actions } = useWorkspacePaneController();
  const validationResult = useExecutionStore(
    (state) => state.validationResult,
  );
  const isValidating = useExecutionStore((state) => state.isValidating);
  const validationError = useExecutionStore(
    (state) => state.validationError,
  );
  const activeSessionId = useSessionStore(
    (state) => state.activeSessionId,
  );
  const compositionVersion = useSessionStore(
    (state) => state.compositionState?.version ?? null,
  );
  const snapshotsBySession = useAuditReadinessStore(
    (state) => state.snapshotsBySession,
  );
  const auditErrorsBySession = useAuditReadinessStore(
    (state) => state.errorBySession,
  );
  const validationStatus =
    projectValidationWorkspaceStatus(
      validationResult,
      isValidating,
      validationError,
    );
  const auditStatus = projectAuditWorkspaceStatus({
    activeSessionId,
    compositionVersion,
    snapshotsBySession,
    errorBySession: auditErrorsBySession,
  });

  return (
    <div
      className="workspace-action-bar"
      role="group"
      aria-label="Workspace actions"
    >
      <div
        id="workspace-status-controls"
        data-workspace-status-controls="true"
        className="workspace-status-controls"
        role="group"
        aria-label="Workspace status"
        tabIndex={-1}
      >
        <StatusButton
          kind="Validation"
          status={validationStatus}
          onClick={(event) =>
            actions.openInspector("validation", event.currentTarget)
          }
        />
        <StatusButton
          kind="Audit"
          status={auditStatus}
          onClick={(event) =>
            actions.openInspector("audit", event.currentTarget)
          }
        />
      </div>
      {capabilities.completion && <CompletionBar />}
    </div>
  );
}
