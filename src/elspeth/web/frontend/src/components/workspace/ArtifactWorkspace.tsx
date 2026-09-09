import {
  type KeyboardEvent,
  useCallback,
  useEffect,
  useInsertionEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import { useAuditReadinessSync } from "@/components/audit/useAuditReadinessSync";
import { projectCompletedGuidedHistory } from "@/components/chat/guided/GuidedHistory";
import { ErrorBoundary } from "@/components/common/ErrorBoundary";
import { InlineRunResults } from "@/components/execution/InlineRunResults";
import { CatalogButton } from "@/components/sidebar/CatalogButton";
import { Button } from "@/components/ui";
import { GraphView } from "@/components/inspector/GraphView";
import { YamlView } from "@/components/inspector/YamlView";
import {
  OPEN_GRAPH_MODAL_EVENT,
  REQUEST_ARTIFACT_VIEW_EVENT,
  claimWorkspaceViewIntent,
  isCurrentWorkspaceViewIntent,
  type RequestArtifactViewDetail,
} from "@/lib/composer-events";
import { useAuditReadinessStore } from "@/stores/auditReadinessStore";
import { useExecutionStore } from "@/stores/executionStore";
import { useSessionStore } from "@/stores/sessionStore";
import type { RunStatus } from "@/types/index";
import { useWorkspacePaneController } from "./WorkspacePaneContext";
import { ChecksView } from "./ChecksView";
import { PipelineSpecView } from "./PipelineSpecView";
import {
  projectAuditWorkspaceStatus,
  projectChecksWorkspaceStatus,
  projectValidationWorkspaceStatus,
  type WorkspaceStatus,
} from "./workspaceStatus";
import { ARTIFACT_TABS, type ArtifactTab } from "./workspaceTypes";

const TAB_LABELS: Record<ArtifactTab, string> = {
  graph: "Graph",
  spec: "Spec",
  yaml: "YAML",
  checks: "Checks",
  run: "Run",
};

interface Announcement {
  id: number;
  message: string;
}

interface CommittedArtifactController {
  sessionId: string | null;
  availableTabs: readonly ArtifactTab[];
  selectArtifactTab: (tab: ArtifactTab) => void;
}

const REQUEST_DETAIL_KEYS = new Set<PropertyKey>([
  "tab",
  "focusMode",
  "sessionId",
]);

function isPlainRecord(value: unknown): value is Record<PropertyKey, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function admitArtifactRequest(event: Event): RequestArtifactViewDetail | null {
  try {
    if (!(event instanceof CustomEvent)) return null;
    const detail: unknown = event.detail;
    if (!isPlainRecord(detail)) return null;
    const keys = Reflect.ownKeys(detail);
    if (
      keys.length !== REQUEST_DETAIL_KEYS.size ||
      !keys.every((key) => REQUEST_DETAIL_KEYS.has(key))
    ) {
      return null;
    }

    const descriptors = Object.getOwnPropertyDescriptors(detail);
    const tabDescriptor = descriptors.tab;
    const focusModeDescriptor = descriptors.focusMode;
    const sessionIdDescriptor = descriptors.sessionId;
    if (
      tabDescriptor === undefined ||
      !("value" in tabDescriptor) ||
      focusModeDescriptor === undefined ||
      !("value" in focusModeDescriptor) ||
      sessionIdDescriptor === undefined ||
      !("value" in sessionIdDescriptor)
    ) {
      return null;
    }

    const tab: unknown = tabDescriptor.value;
    const focusMode: unknown = focusModeDescriptor.value;
    const sessionId: unknown = sessionIdDescriptor.value;
    if (
      typeof tab !== "string" ||
      !(ARTIFACT_TABS as readonly string[]).includes(tab) ||
      typeof focusMode !== "boolean" ||
      (typeof sessionId !== "string" && sessionId !== null)
    ) {
      return null;
    }
    return {
      tab: tab as ArtifactTab,
      focusMode,
      sessionId,
    };
  } catch {
    // Ambient event payloads may be adversarial proxies. Admission failure is
    // inert; it must not escape through the window event listener.
    return null;
  }
}

function activeArtifact(
  tab: ArtifactTab,
  runAvailable: boolean,
  checksValidationContent: React.ReactNode,
): JSX.Element {
  switch (tab) {
    case "graph":
      return <GraphView />;
    case "spec":
      return <PipelineSpecView />;
    case "yaml":
      return <YamlView />;
    case "checks":
      return <ChecksView validationContent={checksValidationContent} />;
    case "run":
      return <InlineRunResults showEmptyState runAvailable={runAvailable} />;
  }
}

/** Visible glyph inside the persistent Checks-tab badge. The Run dot may stay
 *  colour-only because it never outlives its announcing toast (workspace.css
 *  badge contract); the Checks badge is permanent, so every settled verdict
 *  carries a non-colour form — the finding count, ✓ for the all-clear, ! for
 *  a zero-count failure. Only the no-verdict tones (neutral, busy) remain
 *  plain dots. */
function checksBadgeGlyph(status: WorkspaceStatus): string | null {
  if (status.issueCount > 0) return String(status.issueCount);
  switch (status.tone) {
    case "success":
      return "✓";
    case "error":
    case "warning":
      // One shared glyph: today a zero-count warning is unreachable (every
      // warning tone carries at least one counted row), so "!" only ever
      // renders red. A future zero-count warning path would need its own
      // glyph to keep the tones distinguishable without colour.
      return "!";
    default:
      return null;
  }
}

/** Dot colour for the Run-tab outcome badge. Mirrors ProgressView's terminal
 *  colour mapping; `empty` (and any non-terminal residue) stays neutral. */
function badgeTone(status: RunStatus): "success" | "warning" | "error" | "neutral" {
  switch (status) {
    case "completed":
      return "success";
    case "completed_with_failures":
    case "cancelled":
      return "warning";
    case "failed":
      return "error";
    default:
      return "neutral";
  }
}

export function ArtifactWorkspace({
  runAvailable = false,
  catalogAvailable = false,
  checksValidationContent,
}: {
  /** True only when the REQUEST_RUN_EVENT owner (ExecuteButton) is mounted —
   *  App passes capabilities.completion; the tutorial shell leaves it false. */
  runAvailable?: boolean;
  /** Mounts the Plugin-catalog trigger in the toolbar. App passes the same
   *  availability fact that used to gate the action bar's More-actions
   *  popover (!guidedBuildActive); the tutorial shell leaves it false. */
  catalogAvailable?: boolean;
  /** Tutorial-shell content override for the Checks tab's validation half
   *  (PipelineValidationSummary); see ChecksView. */
  checksValidationContent?: React.ReactNode;
} = {}): JSX.Element {
  const activeSessionId = useSessionStore((state) => state.activeSessionId);
  return (
    <ArtifactWorkspaceSurface
      activeSessionId={activeSessionId}
      runAvailable={runAvailable}
      catalogAvailable={catalogAvailable}
      checksValidationContent={checksValidationContent}
    />
  );
}

export function ArtifactWorkspaceSurface({
  activeSessionId,
  runAvailable = false,
  catalogAvailable = false,
  checksValidationContent,
}: {
  activeSessionId: string | null;
  runAvailable?: boolean;
  catalogAvailable?: boolean;
  checksValidationContent?: React.ReactNode;
}): JSX.Element {
  const { state, actions } = useWorkspacePaneController();
  const { activeArtifactTab, availableArtifactTabs } = state;
  const tabRefs = useRef<Record<ArtifactTab, HTMLButtonElement | null>>({
    graph: null,
    spec: null,
    yaml: null,
    checks: null,
    run: null,
  });
  const previousActiveTabRef = useRef(activeArtifactTab);
  const activePanelOwnedFocusRef = useRef(false);
  const pendingTabFocusRef = useRef<ArtifactTab | null>(null);
  const mountedRef = useRef(false);
  const committedControllerRef = useRef<CommittedArtifactController>({
    sessionId: activeSessionId,
    availableTabs: availableArtifactTabs,
    selectArtifactTab: actions.selectArtifactTab,
  });
  const [announcement, setAnnouncement] = useState<Announcement>({
    id: 0,
    message: "",
  });

  // Run-tab badge inputs (elspeth-3a7b7c7b37): pure executionStore readers —
  // primitives only, never new pollers (the 3s loadRuns loop stays owned by
  // InlineRunResults while the Run tab is active; see the polling pin).
  const runOutcomeStatus = useExecutionStore(
    (s) => s.lastRunOutcome?.status ?? null,
  );
  const runAttached = useExecutionStore(
    (s) =>
      s.activeRunId !== null &&
      (s.progress?.status === "running" || s.progress?.status === "pending"),
  );
  const wsDisconnected = useExecutionStore((s) => s.wsDisconnected);
  // A dropped WebSocket means progress.status can no longer advance from this
  // tab, so the "live" pulse would spin forever for a run that may already be
  // finished server-side — degrade the badge to the static neutral dot.
  const runLive = runAttached && !wsDisconnected;
  const acknowledgeRunOutcome = useExecutionStore(
    (s) => s.acknowledgeRunOutcome,
  );

  // Checks-tab badge inputs: the same primitive store reads the retired
  // action-bar chips made (WorkspaceActionBar pre-Checks), merged into one
  // ambient worst-of status by projectChecksWorkspaceStatus.
  const validationResult = useExecutionStore((s) => s.validationResult);
  const isValidating = useExecutionStore((s) => s.isValidating);
  const validationError = useExecutionStore((s) => s.validationError);
  const compositionVersion = useSessionStore(
    (s) => s.compositionState?.version ?? null,
  );
  const snapshotsBySession = useAuditReadinessStore(
    (s) => s.snapshotsBySession,
  );
  const auditErrorsBySession = useAuditReadinessStore(
    (s) => s.errorBySession,
  );
  const hasGuidedHistory = useSessionStore(
    (s) =>
      s.guidedSession !== null &&
      projectCompletedGuidedHistory(
        s.guidedSession.history,
        s.guidedSession.step,
        s.guidedSession.terminal,
      ).length > 0,
  );
  // Ambient audit sync for the badge: while the Checks panel is mounted its
  // own useAuditReadinessSync instance owns the fetch, so this one stands
  // down — exactly one instance syncs at a time.
  useAuditReadinessSync(activeArtifactTab !== "checks");
  const checksStatus = projectChecksWorkspaceStatus(
    projectValidationWorkspaceStatus(
      validationResult,
      isValidating,
      validationError,
    ),
    projectAuditWorkspaceStatus({
      activeSessionId,
      compositionVersion,
      snapshotsBySession,
      errorBySession: auditErrorsBySession,
    }),
  );

  // While the Run tab is active AND the artifact pane is actually visible,
  // ProgressView is the single terminal-status announcement authority (its
  // M07 live region) — acknowledge the outcome before paint so the badge
  // clears and the App-level RunOutcomeNotice renders empty instead of
  // double-announcing. The pane-visibility guard matters on narrow
  // viewports: ComposerWorkspace keeps this surface MOUNTED but hidden/inert
  // in compose view (same mounted-but-hidden fact pendingTabFocusRef
  // consults), where ProgressView can neither be seen nor announced —
  // swallowing the outcome there would silently reinstate the very defect
  // the notice exists to fix.
  useLayoutEffect(() => {
    if (
      state.artifactVisible &&
      activeArtifactTab === "run" &&
      runOutcomeStatus !== null
    ) {
      acknowledgeRunOutcome();
    }
  }, [
    acknowledgeRunOutcome,
    activeArtifactTab,
    runOutcomeStatus,
    state.artifactVisible,
  ]);

  useInsertionEffect(() => {
    committedControllerRef.current = {
      sessionId: activeSessionId,
      availableTabs: availableArtifactTabs,
      selectArtifactTab: actions.selectArtifactTab,
    };
  }, [actions.selectArtifactTab, activeSessionId, availableArtifactTabs]);

  useLayoutEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const announceFallback = useCallback((requested: ArtifactTab): void => {
    setAnnouncement((current) => ({
      id: current.id + 1,
      message: `${TAB_LABELS[requested]} is unavailable. Showing Graph.`,
    }));
  }, []);

  const selectAndFocus = useCallback(
    (requested: ArtifactTab): ArtifactTab => {
      const committed = committedControllerRef.current;
      const selected = committed.availableTabs.includes(requested)
        ? requested
        : "graph";
      actions.showPipeline();
      committed.selectArtifactTab(requested);
      if (state.artifactVisible) {
        tabRefs.current[selected]?.focus({ preventScroll: true });
      } else {
        pendingTabFocusRef.current = selected;
      }
      if (selected !== requested) announceFallback(requested);
      return selected;
    },
    [actions, announceFallback, state.artifactVisible],
  );

  const queueGraphModal = useCallback((
    sessionId: string | null,
    intent: number,
  ): void => {
    queueMicrotask(() => {
      if (
        mountedRef.current &&
        committedControllerRef.current.sessionId === sessionId &&
        isCurrentWorkspaceViewIntent(intent)
      ) {
        window.dispatchEvent(new Event(OPEN_GRAPH_MODAL_EVENT));
      }
    });
  }, []);

  const focusGraph = useCallback((): void => {
    const sessionId = committedControllerRef.current.sessionId;
    const intent = claimWorkspaceViewIntent();
    selectAndFocus("graph");
    queueGraphModal(sessionId, intent);
  }, [queueGraphModal, selectAndFocus]);

  useEffect(() => {
    const handleRequest = (event: Event): void => {
      if (!mountedRef.current) return;
      const request = admitArtifactRequest(event);
      if (
        request === null ||
        request.sessionId !== committedControllerRef.current.sessionId
      ) {
        return;
      }
      const intent = claimWorkspaceViewIntent();
      selectAndFocus(request.tab);
      if (request.tab === "graph" && request.focusMode) {
        queueGraphModal(request.sessionId, intent);
      }
    };
    window.addEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handleRequest);
    return () => {
      window.removeEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handleRequest);
    };
  }, [queueGraphModal, selectAndFocus]);

  useLayoutEffect(() => {
    const pending = pendingTabFocusRef.current;
    if (
      pending === null ||
      !state.artifactVisible ||
      pending !== activeArtifactTab
    ) {
      return;
    }
    tabRefs.current[pending]?.focus({ preventScroll: true });
    pendingTabFocusRef.current = null;
  }, [activeArtifactTab, state.artifactVisible]);

  useLayoutEffect(() => {
    const previousActiveTab = previousActiveTabRef.current;
    if (previousActiveTab !== activeArtifactTab) {
      const previousBecameUnavailable =
        !availableArtifactTabs.includes(previousActiveTab);
      if (previousBecameUnavailable) {
        announceFallback(previousActiveTab);
      }
      if (
        activePanelOwnedFocusRef.current ||
        document.activeElement === tabRefs.current[previousActiveTab]
      ) {
        tabRefs.current[activeArtifactTab]?.focus({ preventScroll: true });
      }
    }
    previousActiveTabRef.current = activeArtifactTab;
    activePanelOwnedFocusRef.current = false;
  }, [activeArtifactTab, announceFallback, availableArtifactTabs]);

  const selectTab = (tab: ArtifactTab): void => {
    if (!availableArtifactTabs.includes(tab)) return;
    claimWorkspaceViewIntent();
    actions.showPipeline();
    actions.selectArtifactTab(tab);
    tabRefs.current[tab]?.focus({ preventScroll: true });
  };

  const handleTabKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
  ): void => {
    const currentIndex = availableArtifactTabs.indexOf(activeArtifactTab);
    let nextTab: ArtifactTab;
    switch (event.key) {
      case "ArrowRight":
        nextTab =
          availableArtifactTabs[(currentIndex + 1) % availableArtifactTabs.length];
        break;
      case "ArrowLeft":
        nextTab =
          availableArtifactTabs[
            (currentIndex - 1 + availableArtifactTabs.length) %
              availableArtifactTabs.length
          ];
        break;
      case "Home":
        nextTab = availableArtifactTabs[0];
        break;
      case "End":
        nextTab = availableArtifactTabs[availableArtifactTabs.length - 1];
        break;
      default:
        return;
    }
    event.preventDefault();
    selectTab(nextTab);
  };

  const activeLabel = TAB_LABELS[activeArtifactTab];
  return (
    <div className="artifact-workspace">
      <div className="artifact-workspace-toolbar">
        <div role="tablist" aria-label="Pipeline artifacts">
          {ARTIFACT_TABS.map((tab) => {
            const available = availableArtifactTabs.includes(tab);
            const selected = activeArtifactTab === tab;
            return (
              <Button
                key={tab}
                ref={(element) => {
                  tabRefs.current[tab] = element;
                }}
                variant="bare"
                className="artifact-tab"
                role="tab"
                id={`artifact-tab-${tab}`}
                aria-controls={`artifact-panel-${tab}`}
                aria-selected={selected}
                tabIndex={selected ? 0 : -1}
                disabled={!available}
                /* The Checks tab's accessible name carries the live merged
                   status ("Checks: 3 issues") — visible label + ": " + status,
                   the same composition the retired action-bar chips used, so
                   the visible "Checks" stays contained in the name (WCAG
                   2.5.3, elspeth-a41fd9d32b lineage). Only while available:
                   a gated tab has nothing checked and stays plain "Checks". */
                aria-label={
                  tab === "checks" && available
                    ? checksStatus.accessibleLabel
                    : undefined
                }
                onClick={() => selectTab(tab)}
                onKeyDown={handleTabKeyDown}
              >
                {TAB_LABELS[tab]}
                {/* Outcome/live badge — aria-hidden so the accessible tab
                    name stays exactly "Run" (named-tab relationship pin). */}
                {tab === "run" && (runAttached || runOutcomeStatus !== null) && (
                  <span
                    className="artifact-tab-badge"
                    aria-hidden="true"
                    data-tone={
                      runAttached
                        ? runLive
                          ? "live"
                          : "neutral"
                        : badgeTone(runOutcomeStatus as RunStatus)
                    }
                  />
                )}
                {/* Ambient checks light — aria-hidden because the tab's
                    aria-label above already speaks the same status. */}
                {tab === "checks" && available && (
                  <span
                    className={
                      checksBadgeGlyph(checksStatus) === null
                        ? "artifact-tab-badge"
                        : "artifact-tab-badge artifact-tab-badge--glyph"
                    }
                    aria-hidden="true"
                    data-tone={checksStatus.tone}
                  >
                    {checksBadgeGlyph(checksStatus)}
                  </span>
                )}
              </Button>
            );
          })}
        </div>
        {/* Right cluster (2026-08-15 UX review): Plugin catalog precedes
            Focus graph in DOM order — visual order IS tab order (WCAG 2.4.3;
            no CSS `order` on interactive controls), and Focus graph keeps
            its long-standing terminal-edge position whether or not the
            catalog renders. Focus graph itself must NEVER be removed in
            favour of a canvas gesture: it is the only keyboard-operable
            trigger for GraphModal (palette "Show graph" and Ctrl+Shift+G
            deliberately pass focusMode:false), and the canvas's click
            gestures are already bound (empty-click deselects, double-click
            zooms). */}
        <div className="artifact-toolbar-actions">
          {/* Sole opener of the History drawer since the action-bar chips
              retired with the Checks tab: gated on the same completed-history
              fact the drawer itself closes on, and its id is the drawer's
              focus-restore fallback (WorkspaceInspector). */}
          {hasGuidedHistory && (
            <Button
              compact
              id="artifact-history-trigger"
              aria-expanded={state.inspectorOpen}
              aria-controls="workspace-inspector"
              onClick={(event) =>
                actions.openInspector("history", event.currentTarget)
              }
            >
              History
            </Button>
          )}
          {catalogAvailable && <CatalogButton />}
          <Button compact onClick={focusGraph}>
            Focus graph
          </Button>
        </div>
      </div>
      <p
        key={announcement.id}
        className="visually-hidden"
        role="status"
        aria-live="polite"
      >
        {announcement.message}
      </p>
      {ARTIFACT_TABS.map((tab) => {
        const active = tab === activeArtifactTab;
        return (
          <div
            key={tab}
            className="artifact-workspace-panel"
            role="tabpanel"
            id={`artifact-panel-${tab}`}
            aria-labelledby={`artifact-tab-${tab}`}
            aria-hidden={!active || undefined}
            hidden={!active}
            {...(!active ? { inert: "" } : {})}
            onFocusCapture={() => {
              if (active) activePanelOwnedFocusRef.current = true;
            }}
            onBlurCapture={(event) => {
              const nextTarget = event.relatedTarget;
              if (
                active &&
                !(
                  nextTarget instanceof Node &&
                  event.currentTarget.contains(nextTarget)
                )
              ) {
                activePanelOwnedFocusRef.current = false;
              }
            }}
          >
            {active && (
              <ErrorBoundary
                key={`${activeSessionId ?? "no-session"}:${tab}`}
                label={`${activeLabel} artifact`}
              >
                {activeArtifact(tab, runAvailable, checksValidationContent)}
              </ErrorBoundary>
            )}
          </div>
        );
      })}
    </div>
  );
}
