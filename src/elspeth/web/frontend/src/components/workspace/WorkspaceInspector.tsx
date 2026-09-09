import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import {
  GuidedHistory,
  projectCompletedGuidedHistory,
} from "@/components/chat/guided/GuidedHistory";
import { ErrorBoundary } from "@/components/common/ErrorBoundary";
import { Button } from "@/components/ui";
import { useSessionStore } from "@/stores/sessionStore";
import { useWorkspacePaneController } from "./WorkspacePaneContext";
import { STANDARD_DESKTOP_MIN } from "./useWorkspacePaneState";

function viewportIsCompact(): boolean {
  return (
    typeof window !== "undefined" && window.innerWidth < STANDARD_DESKTOP_MIN
  );
}

/* Since the Checks tab absorbed the Validation and Audit surfaces
   (ChecksView), this aside holds exactly one thing: the guided session's
   completed history. Its only opener is the artifact toolbar's History
   trigger (#artifact-history-trigger), which is therefore also the
   focus-restore fallback when the exact invoker has left the DOM. */
export function WorkspaceInspector(): JSX.Element {
  const { state, actions, inspectorInvokerRef } =
    useWorkspacePaneController();
  const activeSessionId = useSessionStore((current) => current.activeSessionId);
  const guidedSession = useSessionStore((current) => current.guidedSession);
  const [overlay, setOverlay] = useState(viewportIsCompact);
  const asideRef = useRef<HTMLElement>(null);
  const ownedFocusRef = useRef(false);
  const hasHistory =
    guidedSession !== null &&
    projectCompletedGuidedHistory(
      guidedSession.history,
      guidedSession.step,
      guidedSession.terminal,
    ).length > 0;

  useEffect(() => {
    const updateOverlay = (): void => setOverlay(viewportIsCompact());
    window.addEventListener("resize", updateOverlay);
    return () => window.removeEventListener("resize", updateOverlay);
  }, []);

  useEffect(() => {
    const trackOwnedFocus = (event: FocusEvent): void => {
      const target = event.target;
      ownedFocusRef.current =
        target instanceof Node &&
        asideRef.current?.contains(target) === true;
    };
    document.addEventListener("focusin", trackOwnedFocus);
    return () => document.removeEventListener("focusin", trackOwnedFocus);
  }, []);

  const restoreOpeningFocus = useCallback((): void => {
    const invoker = inspectorInvokerRef.current;
    if (invoker?.isConnected) {
      invoker.focus({ preventScroll: true });
      return;
    }
    const trigger = document.getElementById("artifact-history-trigger");
    (trigger ?? document.body).focus({ preventScroll: true });
  }, [inspectorInvokerRef]);

  const closeInspector = useCallback((): void => {
    actions.closeInspector();
    restoreOpeningFocus();
  }, [actions, restoreOpeningFocus]);

  /* The history can evaporate underneath an open drawer (a guided step
     retracts its summary, the session resets). Close rather than present an
     empty region, and hand focus back only when the drawer owned it — a
     focus grab from unrelated work is worse than a silently closed aside. */
  useLayoutEffect(() => {
    if (!state.inspectorOpen || hasHistory) return;
    const shouldRestoreFocus = ownedFocusRef.current;
    ownedFocusRef.current = false;
    actions.closeInspector();
    if (shouldRestoreFocus) restoreOpeningFocus();
  }, [actions, hasHistory, restoreOpeningFocus, state.inspectorOpen]);

  return (
    <aside
      ref={asideRef}
      id="workspace-inspector"
      className={`workspace-inspector${overlay ? " workspace-inspector--overlay" : ""}`}
      aria-labelledby="workspace-inspector-heading"
      hidden={!state.inspectorOpen}
      onKeyDown={(event) => {
        if (event.key !== "Escape" || event.defaultPrevented) return;
        event.preventDefault();
        closeInspector();
      }}
    >
      <header className="workspace-inspector-header">
        <h2 id="workspace-inspector-heading">History</h2>
        <Button
          compact
          aria-label="Close history"
          onClick={closeInspector}
        >
          Close
        </Button>
      </header>
      <div className="workspace-inspector-body">
        {hasHistory && guidedSession !== null && (
          <ErrorBoundary
            key={`${activeSessionId ?? "no-session"}:history`}
            label="History"
          >
            <GuidedHistory
              history={guidedSession.history}
              currentStep={guidedSession.step}
              terminal={guidedSession.terminal}
            />
          </ErrorBoundary>
        )}
      </div>
    </aside>
  );
}
