import {
  type KeyboardEvent,
  useCallback,
  useEffect,
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
  OPEN_YAML_MODAL_EVENT,
  REQUEST_ARTIFACT_VIEW_EVENT,
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
  const { state, actions } = useWorkspacePaneController();
  const { activeArtifactTab, availableArtifactTabs } = state;
  const tabRefs = useRef<Record<ArtifactTab, HTMLButtonElement | null>>({
    graph: null,
    spec: null,
    yaml: null,
    run: null,
  });
  const panelRef = useRef<HTMLDivElement | null>(null);
  const previousActiveTabRef = useRef(activeArtifactTab);
  const priorActiveOwnedFocusRef = useRef(false);
  const mountedRef = useRef(false);
  const activeSessionIdRef = useRef(activeSessionId);
  const [announcement, setAnnouncement] = useState<Announcement>({
    id: 0,
    message: "",
  });

  activeSessionIdRef.current = activeSessionId;

  useEffect(() => {
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
      const selected = availableArtifactTabs.includes(requested)
        ? requested
        : "graph";
      actions.selectArtifactTab(requested);
      tabRefs.current[selected]?.focus({ preventScroll: true });
      if (selected !== requested) announceFallback(requested);
      return selected;
    },
    [actions, announceFallback, availableArtifactTabs],
  );

  const queueGraphModal = useCallback((sessionId: string | null): void => {
    queueMicrotask(() => {
      if (
        mountedRef.current &&
        activeSessionIdRef.current === sessionId
      ) {
        window.dispatchEvent(new Event(OPEN_GRAPH_MODAL_EVENT));
      }
    });
  }, []);

  const focusGraph = useCallback((): void => {
    selectAndFocus("graph");
    queueGraphModal(activeSessionId);
  }, [activeSessionId, queueGraphModal, selectAndFocus]);

  useEffect(() => {
    const handleRequest = (event: Event): void => {
      const request = event as CustomEvent<RequestArtifactViewDetail>;
      if (request.detail.sessionId !== activeSessionId) return;
      selectAndFocus(request.detail.tab);
      if (request.detail.tab === "graph" && request.detail.focusMode) {
        queueGraphModal(activeSessionId);
      }
    };
    const handleLegacyYamlRequest = (): void => {
      selectAndFocus("yaml");
    };

    window.addEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handleRequest);
    window.addEventListener(OPEN_YAML_MODAL_EVENT, handleLegacyYamlRequest);
    return () => {
      window.removeEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handleRequest);
      window.removeEventListener(OPEN_YAML_MODAL_EVENT, handleLegacyYamlRequest);
    };
  }, [activeSessionId, queueGraphModal, selectAndFocus]);

  useLayoutEffect(() => {
    const previousActiveTab = previousActiveTabRef.current;
    if (
      previousActiveTab !== activeArtifactTab &&
      !availableArtifactTabs.includes(previousActiveTab)
    ) {
      announceFallback(previousActiveTab);
      if (
        priorActiveOwnedFocusRef.current ||
        document.activeElement === tabRefs.current[previousActiveTab]
      ) {
        tabRefs.current.graph?.focus({ preventScroll: true });
      }
    }
    previousActiveTabRef.current = activeArtifactTab;
    priorActiveOwnedFocusRef.current = false;
    const activeTabElement = tabRefs.current[activeArtifactTab];
    const activePanel = panelRef.current;

    return () => {
      const focused = document.activeElement;
      priorActiveOwnedFocusRef.current =
        focused === activeTabElement ||
        (focused !== null && activePanel?.contains(focused) === true);
    };
  }, [activeArtifactTab, announceFallback, availableArtifactTabs]);

  const selectTab = (tab: ArtifactTab): void => {
    if (!availableArtifactTabs.includes(tab)) return;
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
      <div
        ref={panelRef}
        className="artifact-workspace-panel"
        role="tabpanel"
        id={`artifact-panel-${activeArtifactTab}`}
        aria-labelledby={`artifact-tab-${activeArtifactTab}`}
      >
        <ErrorBoundary
          key={`${activeSessionId ?? "no-session"}:${activeArtifactTab}`}
          label={`${activeLabel} artifact`}
        >
          {activeArtifact(activeArtifactTab)}
        </ErrorBoundary>
      </div>
    </div>
  );
}
