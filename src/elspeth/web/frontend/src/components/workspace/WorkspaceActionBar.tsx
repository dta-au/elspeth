import {
  type KeyboardEvent,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import { CompletionBar } from "@/components/composer/CompletionBar";
import { CatalogButton } from "@/components/sidebar/CatalogButton";
import { ImportYamlButton } from "@/components/sidebar/ImportYamlButton";
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
  completion: boolean;
  importYaml: boolean;
  catalog: boolean;
}

interface WorkspaceActionBarProps {
  capabilities: WorkspaceActionCapabilities;
}

type PendingMenuFocus = "first" | "last" | null;

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
    <button
      type="button"
      className="btn-compact workspace-status-control"
      data-tone={status.tone}
      aria-label={status.accessibleLabel}
      onClick={onClick}
    >
      <span>{kind}</span>
      <span>{status.text}</span>
    </button>
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
  const hasSecondaryActions =
    capabilities.importYaml || capabilities.catalog;

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
          <button
            ref={moreButtonRef}
            type="button"
            className="btn-compact"
            aria-label="More actions"
            aria-controls="workspace-more-actions-panel"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((open) => !open)}
            onKeyDown={handleMoreKeyDown}
          >
            More actions
          </button>
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
              {capabilities.importYaml && <ImportYamlButton />}
              {capabilities.catalog && <CatalogButton />}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
