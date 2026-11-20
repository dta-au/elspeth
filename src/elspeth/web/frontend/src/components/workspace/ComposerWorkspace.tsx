import {
  type CSSProperties,
  type KeyboardEvent,
  type ReactNode,
  useCallback,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { ErrorBoundary } from "@/components/common/ErrorBoundary";
import { Button } from "@/components/ui";
import { useSessionStore } from "@/stores/sessionStore";
import { useBlobStore } from "@/stores/blobStore";
import { hasCompositionContent } from "@/utils/compositionState";
import {
  FOCUS_AUTHORING_EVENT,
  TOGGLE_AUTHORING_EVENT,
  claimWorkspaceViewIntent,
} from "@/lib/composer-events";
import {
  AUTHORING_TOGGLE_SELECTOR,
  AuthoringPaneToggle,
  AuthoringToggleProvider,
  type AuthoringToggleState,
} from "./AuthoringPaneToggle";
import {
  type CollapsedAuthoringStatus,
  useCollapsedAuthoringStatus,
} from "./useCollapsedAuthoringStatus";
import { WorkspacePaneProvider } from "./WorkspacePaneContext";
import { WorkspaceSeparator } from "./WorkspaceSeparator";
import { useWorkspacePaneState } from "./useWorkspacePaneState";
import { ARTIFACT_TABS, type ArtifactTab } from "./workspaceTypes";

const EMPTY_ARTIFACT_TABS: readonly ArtifactTab[] = ["graph", "run"];
const PENDING_PIPELINE_ARTIFACT_TABS: readonly ArtifactTab[] = [
  "graph",
  "yaml",
  "run",
];

type NarrowView = "compose" | "pipeline";

export interface ComposerWorkspaceProps {
  authoring: ReactNode;
  artifact: ReactNode;
  actionBar: ReactNode;
  /** The authoring pane's title-bar heading. Hosts whose authoring content
   *  is not a conversation (the tutorial run turn) name it for what it holds. */
  authoringTitle?: string;
  /** Overrides the collapsed-pane status the workspace otherwise projects
   *  from the session store (useCollapsedAuthoringStatus). Every production
   *  host, the tutorial included, uses the live projection. */
  collapsedStatus?: CollapsedAuthoringStatus;
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
  actionBar,
  authoringTitle = "Conversation",
  collapsedStatus,
}: ComposerWorkspaceProps) {
  const activeSessionId = useSessionStore((state) => state.activeSessionId);
  const compositionHasContent = useSessionStore((state) =>
    hasCompositionContent(state.compositionState),
  );
  const hasPendingYamlProposal = useSessionStore((state) =>
    state.compositionProposals.some(
      (proposal) =>
        proposal.session_id === state.activeSessionId &&
        proposal.status === "pending" &&
        proposal.affects.includes("yaml"),
    ),
  );
  const availableArtifactTabs = compositionHasContent
    ? ARTIFACT_TABS
    : hasPendingYamlProposal
      ? PENDING_PIPELINE_ARTIFACT_TABS
      : EMPTY_ARTIFACT_TABS;
  const [workspaceWidth, setWorkspaceWidth] = useState(0);
  const [narrowView, setNarrowView] = useState<NarrowView>("compose");
  const [authoringFocusRequest, setAuthoringFocusRequest] = useState(0);
  const revealRequest = useBlobStore((state) => state.revealRequest);
  const editRequest = useBlobStore((state) => state.editRequest);
  const returnRequest = useBlobStore((state) => state.returnRequest);
  const consumeReturnRequest = useBlobStore((state) => state.consumeReturnRequest);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const composeTabRef = useRef<HTMLButtonElement | null>(null);
  const pipelineTabRef = useRef<HTMLButtonElement | null>(null);
  const authoringSlotRef = useRef<HTMLDivElement | null>(null);
  const separatorSlotRef = useRef<HTMLDivElement | null>(null);
  const artifactSlotRef = useRef<HTMLDivElement | null>(null);
  const authoringPaneRef = useRef<HTMLElement | null>(null);
  const artifactPaneRef = useRef<HTMLElement | null>(null);
  const paneState = useWorkspacePaneState({
    workspaceWidth,
    sessionId: activeSessionId,
    availableArtifactTabs,
  });
  const { setAuthoringCollapsed, selectArtifactTab } = paneState;
  const mode = layoutMode(workspaceWidth);
  const narrow = mode === "narrow";
  // Under 960px there is no hide/show toggle: the Compose / Pipeline tabs
  // switch views. A collapsed preference stored on a wide window would
  // otherwise leave Compose empty with nothing to restore it, so narrow
  // layouts ignore it (and keep it for the next wide layout).
  const authoringCollapsed = paneState.authoringCollapsed && !narrow;
  const authoringViewHidden = narrow && narrowView === "pipeline";
  const artifactViewHidden = narrow && narrowView === "compose";
  const authoringHidden = authoringCollapsed || authoringViewHidden;
  const showPipeline = useCallback(() => setNarrowView("pipeline"), []);
  const showCompose = useCallback(() => setNarrowView("compose"), []);
  const previousNarrowRef = useRef(narrow);
  const liveCollapsedStatus = useCollapsedAuthoringStatus({
    activeSessionId,
    authoringCollapsed,
  });
  const status = collapsedStatus ?? liveCollapsedStatus;
  const [toggleFocusRequest, setToggleFocusRequest] = useState(0);
  // Set by a toggle instance that unmounts while focused; read by the layout
  // effects of the same commit and cleared at its end.
  const toggleFocusDroppedRef = useRef(false);
  const toggleFocusDropped = useCallback((): void => {
    toggleFocusDroppedRef.current = true;
  }, []);

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

  useLayoutEffect(() => {
    const wasNarrow = previousNarrowRef.current;
    previousNarrowRef.current = narrow;
    if (wasNarrow === narrow) return;

    const activeElement = document.activeElement;
    if (narrow) {
      const hiddenSlot =
        narrowView === "compose"
          ? artifactSlotRef.current
          : authoringSlotRef.current;
      const activeControlHidden =
        activeElement instanceof HTMLElement &&
        rootRef.current?.contains(activeElement) &&
        activeElement.closest("[hidden], [inert]") !== null;
      // The hide/show toggle does not render under 960px, so a focused one
      // is gone by now; the selected view tab takes focus instead.
      if (
        !hiddenSlot?.contains(activeElement) &&
        !separatorSlotRef.current?.contains(activeElement) &&
        !activeControlHidden &&
        !toggleFocusDroppedRef.current
      ) {
        return;
      }
      const target =
        narrowView === "compose" ? composeTabRef : pipelineTabRef;
      target.current?.focus({ preventScroll: true });
      return;
    }

    if (
      activeElement !== composeTabRef.current &&
      activeElement !== pipelineTabRef.current
    ) {
      return;
    }
    const target =
      narrowView === "compose" && !authoringCollapsed
        ? authoringPaneRef
        : artifactPaneRef;
    target.current?.focus({ preventScroll: true });
  }, [narrow, narrowView, authoringCollapsed]);

  useLayoutEffect(() => {
    if (authoringFocusRequest === 0 || authoringHidden) return;
    authoringSlotRef.current
      ?.querySelector<HTMLElement>("[data-chat-input]")
      ?.focus({ preventScroll: true });
  }, [authoringFocusRequest, authoringHidden]);

  const focusAuthoring = useCallback((): void => {
    claimWorkspaceViewIntent();
    setNarrowView("compose");
    paneState.setAuthoringCollapsed(false);
    setAuthoringFocusRequest((current) => current + 1);
  }, [paneState]);

  // Navigation to a file reveals the pane without moving focus to ChatInput.
  useLayoutEffect(() => {
    if (revealRequest?.sessionId !== activeSessionId) return;
    claimWorkspaceViewIntent();
    setNarrowView("compose");
    setAuthoringCollapsed(false);
  }, [revealRequest, activeSessionId, setAuthoringCollapsed]);

  // Closing the file manager returns focus to its Show in files trigger. In
  // the narrow layout that trigger's pane may be the hidden one, so show the
  // view that holds it; blobStore focuses it once the pane is unhidden.
  useLayoutEffect(() => {
    if (!returnRequest) return;
    const { trigger, seq } = returnRequest;
    if (artifactSlotRef.current?.contains(trigger)) {
      claimWorkspaceViewIntent();
      setNarrowView("pipeline");
    } else if (authoringSlotRef.current?.contains(trigger)) {
      claimWorkspaceViewIntent();
      setNarrowView("compose");
      setAuthoringCollapsed(false);
    }
    consumeReturnRequest(seq);
  }, [returnRequest, consumeReturnRequest, setAuthoringCollapsed]);

  useLayoutEffect(() => {
    if (!editRequest) return;
    claimWorkspaceViewIntent();
    setNarrowView("pipeline");
    selectArtifactTab("graph");
  }, [editRequest, selectArtifactTab]);

  useLayoutEffect(() => {
    window.addEventListener(FOCUS_AUTHORING_EVENT, focusAuthoring);
    return () => window.removeEventListener(FOCUS_AUTHORING_EVENT, focusAuthoring);
  }, [focusAuthoring]);

  // Store-flag twin of the FOCUS_AUTHORING_EVENT listener above:
  // createSession sets authoringFocusRequested because it can run while this
  // component is UNMOUNTED (empty landing, tutorial graduation), where the
  // window event has zero listeners and the request would be lost — leaving
  // the brand-new session's composer hidden behind the globally-persisted
  // collapsed preference. Consumed exactly once; the consume comes FIRST so
  // a re-render triggered by focusAuthoring cannot re-enter.
  const authoringFocusRequested = useSessionStore(
    (state) => state.authoringFocusRequested,
  );
  const consumeAuthoringFocusRequest = useSessionStore(
    (state) => state.consumeAuthoringFocusRequest,
  );
  useLayoutEffect(() => {
    if (!authoringFocusRequested) return;
    consumeAuthoringFocusRequest();
    focusAuthoring();
  }, [authoringFocusRequested, consumeAuthoringFocusRequest, focusAuthoring]);

  const selectNarrowView = useCallback(
    (view: NarrowView, moveFocus: boolean): void => {
      claimWorkspaceViewIntent();
      setNarrowView(view);
      if (moveFocus) {
        const target = view === "compose" ? composeTabRef : pipelineTabRef;
        target.current?.focus({ preventScroll: true });
      }
    },
    [],
  );

  // The toggle lives in two title bars (the conversation's while open, the
  // pipeline's while collapsed), so the instance that had focus unmounts or
  // goes inert on every flip. Focus moves to the instance now mounted —
  // unless focus already sits somewhere live (Ctrl+\ pressed from the
  // canvas stays on the canvas; Ctrl+/ lands in the chat input).
  const toggleAuthoring = useCallback((): void => {
    claimWorkspaceViewIntent();
    setAuthoringCollapsed(!paneState.authoringCollapsed);
    setToggleFocusRequest((current) => current + 1);
  }, [paneState.authoringCollapsed, setAuthoringCollapsed]);

  useLayoutEffect(() => {
    if (toggleFocusRequest === 0) return;
    const root = rootRef.current;
    const active = document.activeElement;
    const focusLost =
      !(active instanceof HTMLElement) ||
      active === document.body ||
      !active.isConnected ||
      (root?.contains(active) === true &&
        active.closest("[hidden], [inert]") !== null);
    if (!focusLost) return;
    root
      ?.querySelector<HTMLButtonElement>(AUTHORING_TOGGLE_SELECTOR)
      ?.focus({ preventScroll: true });
  }, [toggleFocusRequest]);

  // Ctrl+\ (App's global keymap) and the palette's command. Narrow layouts
  // have no collapsed state, so the shortcut switches between the views.
  const handleToggleIntent = useCallback((): void => {
    if (narrow) {
      selectNarrowView(narrowView === "compose" ? "pipeline" : "compose", true);
      return;
    }
    toggleAuthoring();
  }, [narrow, narrowView, selectNarrowView, toggleAuthoring]);

  useLayoutEffect(() => {
    window.addEventListener(TOGGLE_AUTHORING_EVENT, handleToggleIntent);
    return () =>
      window.removeEventListener(TOGGLE_AUTHORING_EVENT, handleToggleIntent);
  }, [handleToggleIntent]);

  const toggleState = useMemo<AuthoringToggleState | null>(
    () =>
      narrow
        ? null
        : {
            collapsed: authoringCollapsed,
            title: authoringTitle,
            status,
            toggle: toggleAuthoring,
            focusDropped: toggleFocusDropped,
          },
    [
      narrow,
      authoringCollapsed,
      authoringTitle,
      status,
      toggleAuthoring,
      toggleFocusDropped,
    ],
  );

  // Last layout effect of the commit: the dropped-focus signal is per commit.
  useLayoutEffect(() => {
    toggleFocusDroppedRef.current = false;
  });

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
    <WorkspacePaneProvider
      paneState={paneState}
      authoringCollapsed={authoringCollapsed}
      artifactVisible={!artifactViewHidden}
      authoringVisible={!authoringHidden}
      showPipeline={showPipeline}
      showCompose={showCompose}
    >
      <AuthoringToggleProvider value={toggleState}>
      <div
        ref={rootRef}
        className="composer-workspace"
        data-testid="composer-workspace"
        data-layout-mode={mode}
        data-authoring-collapsed={authoringCollapsed || undefined}
        style={rootStyle}
      >
        <div
          className="workspace-view-tabs"
          data-workspace-part="view-tabs"
          role={narrow ? "tablist" : undefined}
          aria-label={narrow ? "Workspace view" : undefined}
          hidden={!narrow}
        >
          <Button
            ref={composeTabRef}
            variant="bare"
            className="artifact-tab"
            role={narrow ? "tab" : undefined}
            id={narrow ? "workspace-compose-tab" : undefined}
            aria-controls={narrow ? "workspace-authoring-panel" : undefined}
            aria-selected={narrow ? narrowView === "compose" : undefined}
            tabIndex={narrow && narrowView === "compose" ? 0 : -1}
            onClick={() => selectNarrowView("compose", true)}
            onKeyDown={handleViewTabKeyDown}
          >
            Compose
          </Button>
          <Button
            ref={pipelineTabRef}
            variant="bare"
            className="artifact-tab"
            role={narrow ? "tab" : undefined}
            id={narrow ? "workspace-pipeline-tab" : undefined}
            aria-controls={narrow ? "workspace-artifact-panel" : undefined}
            aria-selected={narrow ? narrowView === "pipeline" : undefined}
            tabIndex={narrow && narrowView === "pipeline" ? 0 : -1}
            onClick={() => selectNarrowView("pipeline", true)}
            onKeyDown={handleViewTabKeyDown}
          >
            Pipeline
          </Button>
        </div>

        <div
          ref={authoringSlotRef}
          className="workspace-authoring-slot"
          data-workspace-part="authoring"
          hidden={authoringViewHidden}
        >
          <section
            ref={authoringPaneRef}
            id="workspace-authoring-pane"
            className="workspace-authoring-pane"
            role="region"
            aria-label="Authoring pane"
            aria-hidden={authoringHidden || undefined}
            {...(authoringHidden ? { inert: "" } : {})}
            hidden={authoringHidden}
            tabIndex={-1}
          >
            {/* The title bar opens with the Hide control; while collapsed the
                Show control takes the same spot in the pipeline's title bar
                (ArtifactWorkspace). */}
            <header className="workspace-pane-header">
              <AuthoringPaneToggle placement="authoring" />
              <h2 className="workspace-pane-title">{authoringTitle}</h2>
            </header>
            <div
              id={narrow ? "workspace-authoring-panel" : undefined}
              className="workspace-authoring-body"
              role={narrow ? "tabpanel" : undefined}
              aria-labelledby={narrow ? "workspace-compose-tab" : undefined}
            >
              <ErrorBoundary label="Authoring pane">{authoring}</ErrorBoundary>
            </div>
          </section>
        </div>

        {/* Workflow actions only. Grid placement puts this content-sized row
            under the pipeline, while the conversation card spans both
            desktop rows; narrow Compose has no row at all. */}
        <div
          className="workspace-action-bar-slot"
          data-workspace-part="action-bar"
          hidden={artifactViewHidden}
        >
          <div className="workspace-action-bar-main">{actionBar}</div>
        </div>

        <div
          ref={separatorSlotRef}
          className="workspace-separator-slot"
          data-workspace-part="separator"
          hidden={narrow || authoringCollapsed}
        >
          <WorkspaceSeparator
            value={paneState.effectiveAuthoringWidth}
            min={paneState.paneBounds.min}
            max={paneState.paneBounds.max}
            disabled={
              narrow ||
              authoringCollapsed ||
              !paneState.paneBounds.resizable
            }
            onResize={paneState.resizeTransient}
            onResizeEnd={paneState.commitResize}
          />
        </div>

        <div
          ref={artifactSlotRef}
          className="workspace-artifact-slot"
          data-workspace-part="artifact"
          hidden={artifactViewHidden}
        >
          <section
            ref={artifactPaneRef}
            className="workspace-artifact-pane"
            role="region"
            aria-label="Pipeline artifact"
            aria-hidden={artifactViewHidden || undefined}
            {...(artifactViewHidden ? { inert: "" } : {})}
            hidden={artifactViewHidden}
            tabIndex={-1}
          >
            <div
              id={narrow ? "workspace-artifact-panel" : undefined}
              className="workspace-artifact-body"
              role={narrow ? "tabpanel" : undefined}
              aria-labelledby={narrow ? "workspace-pipeline-tab" : undefined}
            >
              {artifact}
            </div>
          </section>
        </div>

      </div>
      </AuthoringToggleProvider>
    </WorkspacePaneProvider>
  );
}
