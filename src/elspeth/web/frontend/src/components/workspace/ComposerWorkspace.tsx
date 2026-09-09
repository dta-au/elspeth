import {
  type CSSProperties,
  type KeyboardEvent,
  type ReactNode,
  useCallback,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import { ErrorBoundary } from "@/components/common/ErrorBoundary";
import { Button, Icon } from "@/components/ui";
import { useSessionStore } from "@/stores/sessionStore";
import { hasCompositionContent } from "@/utils/compositionState";
import {
  FOCUS_AUTHORING_EVENT,
  claimWorkspaceViewIntent,
} from "@/lib/composer-events";
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
type FocusIntent = "collapse" | "restore" | null;

export interface ComposerWorkspaceProps {
  authoring: ReactNode;
  artifact: ReactNode;
  inspector: ReactNode;
  actionBar: ReactNode;
  authoringStatus?: ReactNode;
  collapsedStatus?: ReactNode | {
    text: string;
    tone: "neutral" | "busy" | "error";
  };
}

function isCollapsedStatusProjection(
  value: ComposerWorkspaceProps["collapsedStatus"],
): value is { text: string; tone: "neutral" | "busy" | "error" } {
  if (typeof value !== "object" || value === null) return false;
  const projection = value as { text?: unknown; tone?: unknown };
  return (
    typeof projection.text === "string" &&
    (projection.tone === "neutral" ||
      projection.tone === "busy" ||
      projection.tone === "error")
  );
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
  const rootRef = useRef<HTMLDivElement | null>(null);
  const composeTabRef = useRef<HTMLButtonElement | null>(null);
  const pipelineTabRef = useRef<HTMLButtonElement | null>(null);
  const authoringSlotRef = useRef<HTMLDivElement | null>(null);
  const separatorSlotRef = useRef<HTMLDivElement | null>(null);
  const artifactSlotRef = useRef<HTMLDivElement | null>(null);
  const authoringPaneRef = useRef<HTMLElement | null>(null);
  const artifactPaneRef = useRef<HTMLElement | null>(null);
  const collapseControlRef = useRef<HTMLButtonElement | null>(null);
  const restoreControlRef = useRef<HTMLButtonElement | null>(null);
  const focusIntentRef = useRef<FocusIntent>(null);
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
  const showPipeline = useCallback(() => setNarrowView("pipeline"), []);
  const showCompose = useCallback(() => setNarrowView("compose"), []);
  const previousNarrowRef = useRef(narrow);
  const projectedCollapsedStatus = isCollapsedStatusProjection(collapsedStatus)
    ? collapsedStatus
    : null;
  const collapsedStatusContent: ReactNode = projectedCollapsedStatus
    ? projectedCollapsedStatus.text
    : (collapsedStatus as ReactNode);

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
    const focusIntent = focusIntentRef.current;
    const intentTarget =
      focusIntent === "restore"
        ? restoreControlRef.current
        : focusIntent === "collapse"
          ? collapseControlRef.current
          : null;
    if (intentTarget !== null) {
      intentTarget.focus({ preventScroll: true });
      focusIntentRef.current = null;
    }

    const wasNarrow = previousNarrowRef.current;
    previousNarrowRef.current = narrow;
    if (wasNarrow === narrow) return;

    const activeElement = document.activeElement;
    if (narrow) {
      const hiddenSlot =
        narrowView === "compose"
          ? artifactSlotRef.current
          : authoringSlotRef.current;
      if (
        !hiddenSlot?.contains(activeElement) &&
        !separatorSlotRef.current?.contains(activeElement)
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
      narrowView === "compose" && !paneState.authoringCollapsed
        ? authoringPaneRef
        : artifactPaneRef;
    target.current?.focus({ preventScroll: true });
  }, [narrow, narrowView, paneState.authoringCollapsed]);

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

  const collapseAuthoring = (): void => {
    claimWorkspaceViewIntent();
    focusIntentRef.current = "restore";
    paneState.setAuthoringCollapsed(true);
  };

  const restoreAuthoring = (): void => {
    claimWorkspaceViewIntent();
    focusIntentRef.current = "collapse";
    paneState.setAuthoringCollapsed(false);
  };

  const selectNarrowView = (view: NarrowView, moveFocus: boolean): void => {
    claimWorkspaceViewIntent();
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
    <WorkspacePaneProvider
      paneState={paneState}
      artifactVisible={!artifactViewHidden}
      authoringVisible={!authoringHidden}
      showPipeline={showPipeline}
      showCompose={showCompose}
    >
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
            <div
              id={narrow ? "workspace-authoring-panel" : undefined}
              className="workspace-authoring-body"
              role={narrow ? "tabpanel" : undefined}
              aria-labelledby={narrow ? "workspace-compose-tab" : undefined}
            >
              <ErrorBoundary label="Authoring pane">{authoring}</ErrorBoundary>
            </div>
            <div className="workspace-authoring-status">{authoringStatus}</div>
          </section>
          {paneState.authoringCollapsed && (
            <div
              className="workspace-collapsed-affordance"
              data-tone={projectedCollapsedStatus?.tone}
            >
              <div id="workspace-collapsed-status" role="status">
                {collapsedStatusContent}
              </div>
              <Button
                ref={restoreControlRef}
                variant="bare"
                className="workspace-pane-toggle workspace-restore-control"
                title="Restore authoring pane"
                aria-label="Restore authoring pane"
                aria-expanded={false}
                aria-controls="workspace-authoring-pane"
                aria-describedby="workspace-collapsed-status"
                onClick={restoreAuthoring}
              >
                <Icon
                  name="chevrons-right"
                  className="workspace-pane-toggle-icon"
                />
              </Button>
            </div>
          )}
        </div>

        <div
          ref={separatorSlotRef}
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

        {/* The application's bottom bar: ONE grid item spanning both columns
            (elspeth-9c94a58500), carrying the rule and the fill once. Its two
            cells are the parent grid's own columns (subgrid), so the collapse
            control sits in the authoring column and the action bar in the
            artifact column at every pane width, including the collapsed 0px
            one — and the row is exactly as tall as its content, whichever
            child is showing. Before this the bar was the artifact pane's last
            flex child and the authoring pane painted a MIRRORED band to meet
            it across the seam: first from a token formula, then from a
            measured height a ResizeObserver published (elspeth-215c989bed,
            elspeth-97db9c22e5). Two elements agreeing about one height is the
            seam that produced both bugs; a single element has no height to
            keep in sync.

            The collapse control moved here from the authoring pane's tail
            for the same reason: in narrow Compose view the action bar is
            hidden, so the row's height has to come from the control alone,
            and it can only do that as the row's own child. Its aria-controls
            still names the pane; the disclosure button now sits OUTSIDE the
            region it discloses, which is the same relationship the restore
            button already has. Each child hides on the same fact that used
            to hide its former parent — the control with the authoring pane,
            the bar with the artifact view — so what is visible in each
            state is unchanged, and the row itself hides when neither is. */}
        <div
          className="workspace-action-bar-slot"
          data-workspace-part="action-bar"
          hidden={authoringHidden && artifactViewHidden}
        >
          {/* Icon-form pane toggles (2026-08-15 UX review): the earlier
              full-text secondary-chrome button shared height, border,
              background, and spacing rhythm with the action-bar buttons one
              gutter away, so Gestalt grouping read it as a sixth workflow
              action. Both toggles now follow the pane's own icon+tooltip
              convention (ChatInput's Upload/More buttons): icon-only, full
              name kept as aria-label + title, chevrons pointing the way the
              pane moves. aria-expanded/aria-controls make the disclosure
              state machine-readable; the pair is two buttons (one per
              state), each reporting the state it acts from. */}
          <Button
            ref={collapseControlRef}
            variant="bare"
            className="workspace-pane-toggle workspace-collapse-control"
            title="Collapse authoring pane"
            aria-label="Collapse authoring pane"
            aria-expanded={!paneState.authoringCollapsed}
            aria-controls="workspace-authoring-pane"
            hidden={authoringHidden}
            onClick={collapseAuthoring}
          >
            <Icon name="chevrons-left" className="workspace-pane-toggle-icon" />
          </Button>
          <div className="workspace-action-bar-main" hidden={artifactViewHidden}>
            {actionBar}
          </div>
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
