import {
  type KeyboardEvent,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import { CompletionBar } from "@/components/composer/CompletionBar";
import { CatalogButton } from "@/components/sidebar/CatalogButton";
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
     tutorial, and empty-landing views never render this bar). */
  completion: boolean;
  catalog: boolean;
}

interface WorkspaceActionBarProps {
  capabilities: WorkspaceActionCapabilities;
}

type PendingMenuFocus = "first" | "last" | null;

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
  const [menuOpen, setMenuOpen] = useState(false);
  const moreButtonRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const pendingMenuFocusRef = useRef<PendingMenuFocus>(null);
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
  const hasSecondaryActions = capabilities.catalog;

  const menuButtons = (): HTMLButtonElement[] =>
    menuRef.current === null
      ? []
      : [...menuRef.current.querySelectorAll<HTMLButtonElement>("button")];

  useLayoutEffect(() => {
    if (!menuOpen || pendingMenuFocusRef.current === null) return;
    const buttons = menuButtons();
    const target =
      pendingMenuFocusRef.current === "first"
        ? buttons[0]
        : buttons[buttons.length - 1];
    pendingMenuFocusRef.current = null;
    target?.focus({ preventScroll: true });
  }, [menuOpen]);

  const closeMenu = (): void => {
    setMenuOpen(false);
    moreButtonRef.current?.focus({ preventScroll: true });
  };

  const handleMoreKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
  ): void => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      pendingMenuFocusRef.current =
        event.key === "ArrowDown" ? "first" : "last";
      setMenuOpen(true);
      return;
    }
    if (event.key === "Escape" && menuOpen) {
      event.preventDefault();
      closeMenu();
    }
  };

  const handleMenuKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    const buttons = menuButtons();
    if (buttons.length === 0) return;
    const currentIndex = buttons.indexOf(
      document.activeElement as HTMLButtonElement,
    );
    let nextIndex: number;
    switch (event.key) {
      case "ArrowDown":
        nextIndex = (currentIndex + 1 + buttons.length) % buttons.length;
        break;
      case "ArrowUp":
        nextIndex =
          (currentIndex - 1 + buttons.length) % buttons.length;
        break;
      case "Home":
        nextIndex = 0;
        break;
      case "End":
        nextIndex = buttons.length - 1;
        break;
      case "Escape":
        event.preventDefault();
        closeMenu();
        return;
      default:
        return;
    }
    event.preventDefault();
    buttons[nextIndex]?.focus({ preventScroll: true });
  };

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
      {hasSecondaryActions && (
        <div className="workspace-more-actions">
          <Button
            ref={moreButtonRef}
            compact
            aria-label="More actions"
            aria-controls="workspace-more-actions-panel"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((open) => !open)}
            onKeyDown={handleMoreKeyDown}
          >
            More actions
          </Button>
          {menuOpen && (
            <div
              id="workspace-more-actions-panel"
              ref={menuRef}
              className="workspace-more-actions-menu"
              role="group"
              aria-label="More actions"
              onKeyDown={handleMenuKeyDown}
              onClickCapture={(event) => {
                if (event.target instanceof HTMLButtonElement) {
                  setMenuOpen(false);
                }
              }}
            >
              {capabilities.catalog && <CatalogButton />}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
