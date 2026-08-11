import {
  type KeyboardEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import { AuditReadinessPanel } from "@/components/audit/AuditReadinessPanel";
import {
  GuidedHistory,
  projectCompletedGuidedHistory,
} from "@/components/chat/guided/GuidedHistory";
import { ErrorBoundary } from "@/components/common/ErrorBoundary";
import { SideRailValidationBanner } from "@/components/sidebar/SideRailValidationBanner";
import {
  dispatchArtifactViewIntent,
} from "@/lib/composer-events";
import { useSessionStore } from "@/stores/sessionStore";
import { useWorkspacePaneController } from "./WorkspacePaneContext";
import { STANDARD_DESKTOP_MIN } from "./useWorkspacePaneState";
import type { InspectorTab } from "./workspaceTypes";

const INSPECTOR_TAB_LABELS: Record<InspectorTab, string> = {
  validation: "Validation",
  audit: "Audit",
  history: "History",
};

function viewportIsCompact(): boolean {
  return (
    typeof window !== "undefined" && window.innerWidth < STANDARD_DESKTOP_MIN
  );
}

export function WorkspaceInspector({
  validationContent,
}: {
  validationContent?: ReactNode;
} = {}): JSX.Element {
  const { state, actions, inspectorInvokerRef } =
    useWorkspacePaneController();
  const activeSessionId = useSessionStore((current) => current.activeSessionId);
  const guidedSession = useSessionStore((current) => current.guidedSession);
  const [overlay, setOverlay] = useState(viewportIsCompact);
  const tabRefs = useRef<Record<InspectorTab, HTMLButtonElement | null>>({
    validation: null,
    audit: null,
    history: null,
  });
  const historyPanelRef = useRef<HTMLDivElement>(null);
  const historyOwnedFocusRef = useRef(false);
  const inspectorWasOpenRef = useRef(false);
  const hasHistory =
    guidedSession !== null &&
    projectCompletedGuidedHistory(
      guidedSession.history,
      guidedSession.step,
    ).length > 0;
  const availableTabs: readonly InspectorTab[] = hasHistory
    ? ["validation", "audit", "history"]
    : ["validation", "audit"];
  const activeTab =
    state.activeInspectorTab !== null &&
    availableTabs.includes(state.activeInspectorTab)
      ? state.activeInspectorTab
      : "validation";

  useEffect(() => {
    const updateOverlay = (): void => setOverlay(viewportIsCompact());
    window.addEventListener("resize", updateOverlay);
    return () => window.removeEventListener("resize", updateOverlay);
  }, []);

  useEffect(() => {
    const trackHistoryFocus = (event: FocusEvent): void => {
      const target = event.target;
      historyOwnedFocusRef.current =
        target instanceof Node &&
        (tabRefs.current.history?.contains(target) === true ||
          historyPanelRef.current?.contains(target) === true);
    };
    document.addEventListener("focusin", trackHistoryFocus);
    return () => document.removeEventListener("focusin", trackHistoryFocus);
  }, []);

  useLayoutEffect(() => {
    if (
      state.inspectorOpen &&
      state.activeInspectorTab === "history" &&
      !hasHistory
    ) {
      const shouldRestoreTabFocus = historyOwnedFocusRef.current;
      historyOwnedFocusRef.current = false;
      actions.openInspector(
        "validation",
        inspectorInvokerRef.current ??
          document.getElementById("workspace-status-controls") ??
          document.body,
      );
      if (shouldRestoreTabFocus) {
        tabRefs.current.validation?.focus({ preventScroll: true });
      }
      return;
    }
    if (state.inspectorOpen && !inspectorWasOpenRef.current) {
      tabRefs.current[activeTab]?.focus({ preventScroll: true });
    }
    inspectorWasOpenRef.current = state.inspectorOpen;
  }, [
    actions,
    activeTab,
    hasHistory,
    inspectorInvokerRef,
    state.activeInspectorTab,
    state.inspectorOpen,
  ]);

  const restoreOpeningFocus = useCallback((): void => {
    const invoker = inspectorInvokerRef.current;
    if (invoker?.isConnected) {
      invoker.focus({ preventScroll: true });
      return;
    }
    document
      .getElementById("workspace-status-controls")
      ?.focus({ preventScroll: true });
  }, [inspectorInvokerRef]);

  const closeInspector = useCallback((): void => {
    actions.closeInspector();
    restoreOpeningFocus();
  }, [actions, restoreOpeningFocus]);

  const selectTab = useCallback(
    (tab: InspectorTab): void => {
      const stableInvoker =
        inspectorInvokerRef.current ??
        document.getElementById("workspace-status-controls") ??
        document.body;
      actions.openInspector(tab, stableInvoker);
      tabRefs.current[tab]?.focus({ preventScroll: true });
    },
    [actions, inspectorInvokerRef],
  );

  const handleTabKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
  ): void => {
    const currentIndex = availableTabs.indexOf(activeTab);
    let nextTab: InspectorTab;
    switch (event.key) {
      case "ArrowRight":
        nextTab = availableTabs[(currentIndex + 1) % availableTabs.length];
        break;
      case "ArrowLeft":
        nextTab =
          availableTabs[
            (currentIndex - 1 + availableTabs.length) % availableTabs.length
          ];
        break;
      case "Home":
        nextTab = availableTabs[0];
        break;
      case "End":
        nextTab = availableTabs[availableTabs.length - 1];
        break;
      default:
        return;
    }
    event.preventDefault();
    selectTab(nextTab);
  };

  const selectComponent = useCallback(
    (componentId: string): void => {
      useSessionStore.getState().selectNode(componentId);
      dispatchArtifactViewIntent({
        tab: "graph",
        focusMode: false,
        sessionId: activeSessionId,
      });
    },
    [activeSessionId],
  );

  return (
    <aside
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
        <h2 id="workspace-inspector-heading">Inspector</h2>
        <button
          type="button"
          aria-label="Close inspector"
          onClick={closeInspector}
        >
          Close
        </button>
      </header>
      <div
        className="workspace-inspector-tabs"
        role="tablist"
        aria-label="Inspector sections"
      >
        {availableTabs.map((tab) => {
          const selected = tab === activeTab;
          return (
            <button
              key={tab}
              ref={(element) => {
                tabRefs.current[tab] = element;
              }}
              type="button"
              role="tab"
              id={`workspace-inspector-tab-${tab}`}
              aria-controls={`workspace-inspector-panel-${tab}`}
              aria-selected={selected}
              tabIndex={selected ? 0 : -1}
              onClick={() => selectTab(tab)}
              onKeyDown={handleTabKeyDown}
            >
              {INSPECTOR_TAB_LABELS[tab]}
            </button>
          );
        })}
      </div>
      <div className="workspace-inspector-body">
        <div
          role="tabpanel"
          id="workspace-inspector-panel-validation"
          aria-labelledby="workspace-inspector-tab-validation"
          hidden={activeTab !== "validation"}
          {...(activeTab !== "validation" ? { inert: "" } : {})}
        >
          <ErrorBoundary
            key={`${activeSessionId ?? "no-session"}:validation`}
            label="Validation inspector"
          >
            {validationContent === undefined ? (
              <SideRailValidationBanner onSelectComponent={selectComponent} />
            ) : (
              validationContent
            )}
          </ErrorBoundary>
        </div>
        <div
          role="tabpanel"
          id="workspace-inspector-panel-audit"
          aria-labelledby="workspace-inspector-tab-audit"
          hidden={activeTab !== "audit"}
          {...(activeTab !== "audit" ? { inert: "" } : {})}
        >
          <ErrorBoundary
            key={`${activeSessionId ?? "no-session"}:audit`}
            label="Audit inspector"
          >
            <AuditReadinessPanel onSelectComponent={selectComponent} />
          </ErrorBoundary>
        </div>
        {hasHistory && guidedSession !== null && (
          <div
            ref={historyPanelRef}
            role="tabpanel"
            id="workspace-inspector-panel-history"
            aria-labelledby="workspace-inspector-tab-history"
            hidden={activeTab !== "history"}
            {...(activeTab !== "history" ? { inert: "" } : {})}
          >
            <ErrorBoundary
              key={`${activeSessionId ?? "no-session"}:history`}
              label="History inspector"
            >
              <GuidedHistory
                history={guidedSession.history}
                currentStep={guidedSession.step}
              />
            </ErrorBoundary>
          </div>
        )}
      </div>
    </aside>
  );
}
