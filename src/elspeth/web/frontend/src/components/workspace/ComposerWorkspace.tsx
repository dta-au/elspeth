import {
  type CSSProperties,
  type KeyboardEvent,
  type ReactNode,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import { ErrorBoundary } from "@/components/common/ErrorBoundary";
import { useSessionStore } from "@/stores/sessionStore";
import { hasCompositionContent } from "@/utils/compositionState";
import { WorkspacePaneProvider } from "./WorkspacePaneContext";
import { WorkspaceSeparator } from "./WorkspaceSeparator";
import { useWorkspacePaneState } from "./useWorkspacePaneState";
import { ARTIFACT_TABS, type ArtifactTab } from "./workspaceTypes";

const EMPTY_ARTIFACT_TABS: readonly ArtifactTab[] = ["graph", "run"];

type NarrowView = "compose" | "pipeline";

export interface ComposerWorkspaceProps {
  authoring: ReactNode;
  artifact: ReactNode;
  inspector: ReactNode;
  actionBar: ReactNode;
  authoringStatus?: ReactNode;
  collapsedStatus?: {
    text: string;
    tone: "neutral" | "busy" | "error";
  };
}

function layoutMode(workspaceWidth: number):
  | "unmeasured"
  | "narrow"
  | "compact"
  | "desktop" {
  if (workspaceWidth <= 0) return "unmeasured";
  if (workspaceWidth < 960) return "narrow";
  if (workspaceWidth < 1000) return "compact";
  return "desktop";
}

export function ComposerWorkspace({
  authoring,
  artifact,
  inspector,
  actionBar,
  authoringStatus,
  collapsedStatus = {
    text: "Authoring pane collapsed",
    tone: "neutral",
  },
}: ComposerWorkspaceProps) {
  const activeSessionId = useSessionStore((state) => state.activeSessionId);
  const compositionHasContent = useSessionStore((state) =>
    hasCompositionContent(state.compositionState),
  );
  const availableArtifactTabs = compositionHasContent
    ? ARTIFACT_TABS
    : EMPTY_ARTIFACT_TABS;
  const [workspaceWidth, setWorkspaceWidth] = useState(0);
  const [narrowView, setNarrowView] = useState<NarrowView>("compose");
  const rootRef = useRef<HTMLDivElement | null>(null);
  const composeTabRef = useRef<HTMLButtonElement | null>(null);
  const pipelineTabRef = useRef<HTMLButtonElement | null>(null);
  const paneState = useWorkspacePaneState({
    workspaceWidth,
    sessionId: activeSessionId,
    availableArtifactTabs,
  });
  const mode = layoutMode(workspaceWidth);
  const narrow = mode === "narrow";
  const authoringViewHidden = narrow && narrowView === "pipeline";
  const artifactViewHidden = narrow && narrowView === "compose";
  const authoringHidden = paneState.authoringCollapsed || authoringViewHidden;

  useLayoutEffect(() => {
    const root = rootRef.current;
    if (root === null) return;
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width;
      if (width !== undefined && Number.isFinite(width) && width > 0) {
        setWorkspaceWidth(width);
      }
    });
    observer.observe(root);
    return () => observer.disconnect();
  }, []);

  const selectNarrowView = (view: NarrowView, moveFocus: boolean): void => {
    setNarrowView(view);
    if (moveFocus) {
      const target = view === "compose" ? composeTabRef : pipelineTabRef;
      target.current?.focus({ preventScroll: true });
    }
  };

  const handleViewTabKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
  ): void => {
    let nextView: NarrowView;
    switch (event.key) {
      case "ArrowLeft":
      case "ArrowRight":
        nextView = narrowView === "compose" ? "pipeline" : "compose";
        break;
      case "Home":
        nextView = "compose";
        break;
      case "End":
        nextView = "pipeline";
        break;
      default:
        return;
    }
    event.preventDefault();
    selectNarrowView(nextView, true);
  };

  const rootStyle = {
    "--authoring-pane-width": `${paneState.effectiveAuthoringWidth}px`,
  } as CSSProperties;

  return (
    <WorkspacePaneProvider paneState={paneState}>
      <div
        ref={rootRef}
        className="composer-workspace"
        data-testid="composer-workspace"
        data-layout-mode={mode}
        data-authoring-collapsed={paneState.authoringCollapsed || undefined}
        style={rootStyle}
      >
        <div
          className="workspace-view-tabs"
          data-workspace-part="view-tabs"
          role="tablist"
          aria-label="Workspace view"
          hidden={!narrow}
        >
          <button
            ref={composeTabRef}
            type="button"
            role="tab"
            id="workspace-compose-tab"
            aria-controls="workspace-authoring-panel"
            aria-selected={narrowView === "compose"}
            tabIndex={narrowView === "compose" ? 0 : -1}
            onClick={() => selectNarrowView("compose", true)}
            onKeyDown={handleViewTabKeyDown}
          >
            Compose
          </button>
          <button
            ref={pipelineTabRef}
            type="button"
            role="tab"
            id="workspace-pipeline-tab"
            aria-controls="workspace-artifact-panel"
            aria-selected={narrowView === "pipeline"}
            tabIndex={narrowView === "pipeline" ? 0 : -1}
            onClick={() => selectNarrowView("pipeline", true)}
            onKeyDown={handleViewTabKeyDown}
          >
            Pipeline
          </button>
        </div>

        <div
          className="workspace-authoring-slot"
          data-workspace-part="authoring"
          hidden={authoringViewHidden}
        >
          <section
            className="workspace-authoring-pane"
            role="region"
            aria-label="Authoring pane"
            aria-hidden={authoringHidden || undefined}
            {...(authoringHidden ? { inert: "" } : {})}
            hidden={authoringHidden}
          >
            <div
              id="workspace-authoring-panel"
              className="workspace-authoring-body"
              role="tabpanel"
              aria-labelledby="workspace-compose-tab"
            >
              <ErrorBoundary label="Authoring pane">{authoring}</ErrorBoundary>
            </div>
            <div className="workspace-authoring-status">{authoringStatus}</div>
            <button
              type="button"
              className="workspace-collapse-control"
              aria-label="Collapse authoring pane"
              onClick={() => paneState.setAuthoringCollapsed(true)}
            >
              Collapse authoring pane
            </button>
          </section>
          {paneState.authoringCollapsed && (
            <div
              className="workspace-collapsed-affordance"
              data-tone={collapsedStatus.tone}
            >
              <div id="workspace-collapsed-status" role="status">
                {collapsedStatus.text}
              </div>
              <button
                type="button"
                aria-label="Restore authoring pane"
                aria-describedby="workspace-collapsed-status"
                onClick={() => paneState.setAuthoringCollapsed(false)}
              >
                Restore authoring pane
              </button>
            </div>
          )}
        </div>

        <div
          className="workspace-separator-slot"
          data-workspace-part="separator"
          hidden={narrow || paneState.authoringCollapsed}
        >
          <WorkspaceSeparator
            value={paneState.effectiveAuthoringWidth}
            min={paneState.paneBounds.min}
            max={paneState.paneBounds.max}
            disabled={
              narrow ||
              paneState.authoringCollapsed ||
              !paneState.paneBounds.resizable
            }
            onResize={paneState.resizeTransient}
            onResizeEnd={paneState.commitResize}
          />
        </div>

        <div
          className="workspace-artifact-slot"
          data-workspace-part="artifact"
          hidden={artifactViewHidden}
        >
          <section
            className="workspace-artifact-pane"
            role="region"
            aria-label="Pipeline artifact"
            aria-hidden={artifactViewHidden || undefined}
            {...(artifactViewHidden ? { inert: "" } : {})}
            hidden={artifactViewHidden}
          >
            <div
              id="workspace-artifact-panel"
              className="workspace-artifact-body"
              role="tabpanel"
              aria-labelledby="workspace-pipeline-tab"
            >
              {artifact}
            </div>
            <div className="workspace-action-bar-slot">{actionBar}</div>
          </section>
        </div>

        <div
          className="workspace-inspector-slot"
          data-workspace-part="inspector"
        >
          {inspector}
        </div>
      </div>
    </WorkspacePaneProvider>
  );
}
