import {
  type KeyboardEvent,
  useCallback,
  useEffect,
  useInsertionEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import { ErrorBoundary } from "@/components/common/ErrorBoundary";
import { InlineRunResults } from "@/components/execution/InlineRunResults";
import { GraphView } from "@/components/inspector/GraphView";
import { YamlView } from "@/components/inspector/YamlView";
import {
  OPEN_GRAPH_MODAL_EVENT,
  REQUEST_ARTIFACT_VIEW_EVENT,
  claimWorkspaceViewIntent,
  type RequestArtifactViewDetail,
} from "@/lib/composer-events";
import { useSessionStore } from "@/stores/sessionStore";
import { useWorkspacePaneController } from "./WorkspacePaneContext";
import { PipelineSpecView } from "./PipelineSpecView";
import { ARTIFACT_TABS, type ArtifactTab } from "./workspaceTypes";

const TAB_LABELS: Record<ArtifactTab, string> = {
  graph: "Graph",
  spec: "Spec",
  yaml: "YAML",
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

function activeArtifact(tab: ArtifactTab): JSX.Element {
  switch (tab) {
    case "graph":
      return <GraphView />;
    case "spec":
      return <PipelineSpecView />;
    case "yaml":
      return <YamlView />;
    case "run":
      return <InlineRunResults showEmptyState />;
  }
}

export function ArtifactWorkspace(): JSX.Element {
  const activeSessionId = useSessionStore((state) => state.activeSessionId);
  return <ArtifactWorkspaceSurface activeSessionId={activeSessionId} />;
}

export function ArtifactWorkspaceSurface({
  activeSessionId,
}: {
  activeSessionId: string | null;
}): JSX.Element {
  const { state, actions } = useWorkspacePaneController();
  const { activeArtifactTab, availableArtifactTabs } = state;
  const tabRefs = useRef<Record<ArtifactTab, HTMLButtonElement | null>>({
    graph: null,
    spec: null,
    yaml: null,
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

  const queueGraphModal = useCallback((sessionId: string | null): void => {
    queueMicrotask(() => {
      if (
        mountedRef.current &&
        committedControllerRef.current.sessionId === sessionId
      ) {
        window.dispatchEvent(new Event(OPEN_GRAPH_MODAL_EVENT));
      }
    });
  }, []);

  const focusGraph = useCallback((): void => {
    const sessionId = committedControllerRef.current.sessionId;
    claimWorkspaceViewIntent();
    selectAndFocus("graph");
    queueGraphModal(sessionId);
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
      claimWorkspaceViewIntent();
      selectAndFocus(request.tab);
      if (request.tab === "graph" && request.focusMode) {
        queueGraphModal(request.sessionId);
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
              <button
                key={tab}
                ref={(element) => {
                  tabRefs.current[tab] = element;
                }}
                type="button"
                role="tab"
                id={`artifact-tab-${tab}`}
                aria-controls={`artifact-panel-${tab}`}
                aria-selected={selected}
                tabIndex={selected ? 0 : -1}
                disabled={!available}
                onClick={() => selectTab(tab)}
                onKeyDown={handleTabKeyDown}
              >
                {TAB_LABELS[tab]}
              </button>
            );
          })}
        </div>
        <button type="button" onClick={focusGraph}>
          Focus Graph
        </button>
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
                {activeArtifact(tab)}
              </ErrorBoundary>
            )}
          </div>
        );
      })}
    </div>
  );
}
