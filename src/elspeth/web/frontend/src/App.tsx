import {
  useEffect,
  useState,
  useCallback,
  useMemo,
  useRef,
  type MouseEvent as ReactMouseEvent,
} from "react";
import "./styles/index.css";
import * as api from "./api/client";
import {
  STALE_BUILD_POLLS_REQUIRED,
  nextStaleBuildStreak,
  ownFrontendBuild,
} from "./utils/deployBeacon";
import { AuthGuard } from "./components/common/AuthGuard";
import { AppHeader } from "./components/common/AppHeader";
import { GraphModal } from "./components/sidebar/GraphModal";
import { ImportYamlModalHost } from "./components/sidebar/ImportYamlModal";
import { CommandPalette } from "./components/common/CommandPalette";
import { ConfirmDialog } from "./components/common/ConfirmDialog";
import {
  AppNoticeCenter,
  type AppNotice,
} from "./components/common/AppNoticeCenter";
import { ShortcutsHelp } from "./components/common/ShortcutsHelp";
import { DefaultModeChangedBanner } from "./components/common/DefaultModeChangedBanner";
import { ChatPanel } from "./components/chat/ChatPanel";
import { CatalogDrawer } from "./components/catalog/CatalogDrawer";
import { RecoveryPanel } from "./components/recovery/RecoveryPanel";
import { SecretsPanel } from "./components/settings/SecretsPanel";
import { ComposerPreferencesPanel } from "./components/settings/ComposerPreferencesPanel";
import { HelloWorldTutorial } from "./components/tutorial";
import {
  REQUEST_RUN_EVENT,
  dispatchAuthoringFocusIntent,
  dispatchArtifactViewIntent,
} from "./lib/composer-events";
import { useAuthStore } from "./stores/authStore";
import { initStoreSubscriptions, requestValidate } from "./stores/subscriptions";
import { useSessionStore } from "./stores/sessionStore";
import { isGuidedBuildActive } from "./components/chat/guided/guidedBuildActive";
import { useExecutionStore } from "./stores/executionStore";
import {
  selectTutorialCompleted,
  usePreferencesStore,
} from "./stores/preferencesStore";
import { useHashRouter } from "./hooks/useHashRouter";
import { useSharedToken } from "./hooks/useSharedToken";
import { useAuth } from "./hooks/useAuth";
import { useAutoResumeSession } from "./hooks/useAutoResumeSession";
import {
  formatDocumentTitle,
  useDocumentTitle,
} from "./hooks/useDocumentTitle";
import { hasCompositionContent } from "./utils/compositionState";
import { SharedInspectView } from "./components/shared/SharedInspectView";
import { SaveForReviewDialog } from "./components/composer/SaveForReviewDialog";
import { ComposerWorkspace } from "./components/workspace/ComposerWorkspace";
import { ArtifactWorkspace } from "./components/workspace/ArtifactWorkspace";
import { WorkspaceInspector } from "./components/workspace/WorkspaceInspector";
import { WorkspaceActionBar } from "./components/workspace/WorkspaceActionBar";
import { useWorkspacePaneController } from "./components/workspace/WorkspacePaneContext";
import { useCollapsedAuthoringStatus } from "./components/workspace/useCollapsedAuthoringStatus";
import { useSessionLifecycle } from "./hooks/useSession";
import {
  OPEN_CATALOG_EVENT,
} from "./lib/composer-events";
import type { SystemStatus } from "./types/index";
import { applyServerComposerTimeout } from "./config/composer";

// Health check interval in milliseconds (30 seconds)
const HEALTH_CHECK_INTERVAL = 30_000;
// Wire up cross-store subscriptions once at module load time.
// This must run before any component renders so that version-change
// auto-clear is active from the first render.
initStoreSubscriptions();

/**
 * Top-level application component.
 *
 * Single composition root: AuthGuard gates the entire app behind authentication,
 * then AppHeader and ComposerWorkspace render the authoring and artifact
 * surfaces. No router in v1 -- the entire application is a single page.
 */
function CollapsedAuthoringStatus(): JSX.Element {
  const activeSessionId = useSessionStore((state) => state.activeSessionId);
  const { state } = useWorkspacePaneController();
  const status = useCollapsedAuthoringStatus({
    activeSessionId,
    authoringCollapsed: state.authoringCollapsed,
  });
  return (
    <span className="workspace-collapsed-status" data-tone={status.tone}>
      {status.text}
    </span>
  );
}

function App() {
  const [systemStatus, setSystemStatus] = useState<SystemStatus | null>(null);
  // Deploy-cache coherence beacon: this tab's own bundle identity (null in
  // dev disarms the feature) plus the stable-mismatch latch. Latched once
  // the polled frontend_build differs across STALE_BUILD_POLLS_REQUIRED
  // consecutive health checks; only a refresh clears it — never auto-reload
  // (an in-flight guided operation must not be yanked).
  const ownBuild = useMemo(() => ownFrontendBuild(), []);
  const staleBuildStreakRef = useRef(0);
  const [staleBuildDetected, setStaleBuildDetected] = useState(false);
  const [backendAvailable, setBackendAvailable] = useState<boolean | null>(null);
  const [showSecrets, setShowSecrets] = useState(false);
  const [showPalette, setShowPalette] = useState(false);
  const [showShortcuts, setShowShortcuts] = useState(false);
  const [showComposerSettings, setShowComposerSettings] = useState(false);
  const [tutorialResetEpoch, setTutorialResetEpoch] = useState(0);
  const [catalogOpen, setCatalogOpen] = useState(false);
  const logout = useAuthStore((s) => s.logout);
  const openComposerSettings = useCallback(
    () => setShowComposerSettings(true),
    [],
  );
  const closeComposerSettings = useCallback(
    () => setShowComposerSettings(false),
    [],
  );
  const handleResetTutorialComplete = useCallback(() => {
    setShowComposerSettings(false);
    setTutorialResetEpoch((epoch) => epoch + 1);
  }, []);
  const healthCheckRef = useRef<number | null>(null);

  // Phase 6B Task 8: shared-inspect route detection. When the URL hash is
  // `#/shared/{token}`, render the read-only inspect view and short-circuit
  // the regular composer UI. The session router's URL-writes are dormant
  // in this mode (see useHashRouter._isSharedRoute), so the hash is
  // preserved across the entire shared-view lifecycle.
  const sharedToken = useSharedToken();
  const { isAuthenticated } = useAuth();

  // Sync URL hash ↔ session/tab state for deep linking & back/forward
  const { redirectToast } = useHashRouter({
    enabled: isAuthenticated && sharedToken === null,
  });
  useSessionLifecycle();

  const createSession = useSessionStore((s) => s.createSession);
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  // Guided build on screen → ChatPanel renders the two-column workspace and
  // this shell must drop the freeform SideRail (the workspace rail replaces
  // it). Selector returns a primitive so zustand only re-renders on flips.
  const guidedBuildActive = useSessionStore((s) =>
    isGuidedBuildActive(s.guidedSession, s.guidedNextTurn),
  );
  const compositionState = useSessionStore((s) => s.compositionState);
  const sessionsLoaded = useSessionStore((s) => s.sessionsLoaded);
  const hasLiveSessions = useSessionStore((s) =>
    s.sessions.some((session) => !session.archived),
  );
  const activeSessionTitle = useSessionStore((s) => {
    const active = s.sessions.find((session) => session.id === s.activeSessionId);
    return active?.title ?? null;
  });
  const recoveryError = useSessionStore((s) => s.recoveryError);
  const applyRecoveredState = useSessionStore((s) => s.applyRecoveredState);
  const discardRecovery = useSessionStore((s) => s.discardRecovery);
  const pendingFanoutGuard = useExecutionStore((s) => s.pendingFanoutGuard);
  const bootstrapPrefs = usePreferencesStore((s) => s.bootstrap);
  const preferencesLoaded = usePreferencesStore((s) => s.loaded);
  const tutorialCompleted = usePreferencesStore(selectTutorialCompleted);
  const preferencesWriteError = usePreferencesStore((s) => s.writeError);
  // I5: when bootstrap failed (writeError is set), tutorialCompleted is at
  // its initial-state default of false — but that's "we don't know," not
  // "definitively not completed." Showing the tutorial on the failure
  // branch would re-prompt a returning user who has already completed it
  // (and who is already seeing a corrupt-preferences alert). Treat the
  // unknown state as "don't surface tutorial," consistent with the
  // no-fabrication contract in the store.
  const showTutorial =
    preferencesLoaded && !tutorialCompleted && preferencesWriteError === null;

  // Returning-user auto-resume (elspeth-e69642fede): once sessions have
  // loaded, select the most recently active one instead of landing on an
  // empty shell. Gated so it never fights the flows that own their own
  // session choice: the first-run tutorial (tutorial resume wins — wait for
  // preferences to settle before deciding), the shared-inspect route, and
  // hash deep links (checked inside the hook).
  const preferencesSettled =
    preferencesLoaded || preferencesWriteError !== null;
  useAutoResumeSession(
    isAuthenticated &&
      sharedToken === null &&
      preferencesSettled &&
      !showTutorial,
  );

  // Browser-tab title tracks the active session (elspeth-42f63fa312).
  // Session rename and the first-message auto-title both update the
  // sessions list, so the tab follows without extra plumbing.
  useDocumentTitle(
    formatDocumentTitle(sharedToken === null ? activeSessionTitle : null),
  );

  // Real empty state (elspeth-e69642fede): the account has no live sessions,
  // so there is nothing to auto-resume — the main area carries the primary
  // actions directly rather than pointing at a header menu.
  const showEmptyLanding =
    sessionsLoaded && !hasLiveSessions && activeSessionId === null;

  // REQUEST_RUN_EVENT has exactly one policy owner: ExecuteButton inside
  // CompletionBar. Keep every emitter aligned with the same availability
  // fact used to mount that owner, so shortcuts and the command palette can
  // never dispatch into a zero-listener surface.
  const runAdmissionAvailable =
    sharedToken === null &&
    !showTutorial &&
    !showEmptyLanding &&
    !guidedBuildActive;
  const catalogAvailable = !guidedBuildActive;
  const workspaceActionCapabilities = useMemo(
    () => ({
      completion: runAdmissionAvailable,
      importYaml: !guidedBuildActive,
      catalog: catalogAvailable,
    }),
    [catalogAvailable, guidedBuildActive, runAdmissionAvailable],
  );

  useEffect(() => {
    if (!catalogAvailable) {
      setCatalogOpen(false);
      return;
    }

    function handleOpenCatalog() {
      setCatalogOpen(true);
    }

    window.addEventListener(OPEN_CATALOG_EVENT, handleOpenCatalog);
    return () => window.removeEventListener(OPEN_CATALOG_EVENT, handleOpenCatalog);
  }, [catalogAvailable]);

  // Phase 1B + I5: load account-level composer preferences once authenticated.
  // bootstrapPrefs() is contracted to NEVER reject — it catches failures
  // internally, degrades to the guided default, and surfaces the failure
  // via the store's writeError (rendered by the role="alert" region wired
  // by Phase 1B-round-2). The earlier .catch(console.error) was silently
  // swallowing CorruptPreferencesError, the named backend integrity
  // exception, with no user-visible signal at all.
  useEffect(() => {
    if (!isAuthenticated) return;
    void bootstrapPrefs();
  }, [isAuthenticated, bootstrapPrefs]);

  const openSecrets = useCallback(() => setShowSecrets(true), []);
  const closeSecrets = useCallback(() => setShowSecrets(false), []);
  const closePalette = useCallback(() => setShowPalette(false), []);
  const focusMainContent = useCallback(
    (event: ReactMouseEvent<HTMLAnchorElement>) => {
      event.preventDefault();
      document.getElementById("composer-main")?.focus({ preventScroll: true });
    },
    [],
  );
  const confirmFanoutExecution = useCallback(async () => {
    await useExecutionStore.getState().confirmFanoutExecution();
  }, []);
  const dismissFanoutGuard = useCallback(() => {
    useExecutionStore.getState().dismissFanoutGuard();
  }, []);

  // Check backend health. healthChecking/lastHealthCheckAt exist because a
  // Retry that changes NOTHING visible on failure reads as a dead button
  // (operator-observed during a network drop): the button now shows a
  // checking state while in flight, and a failed attempt stamps the banner
  // with the attempt time — the role=alert content change doubles as the
  // "still unreachable" announcement for AT.
  const [healthChecking, setHealthChecking] = useState(false);
  const [lastHealthCheckAt, setLastHealthCheckAt] = useState<string | null>(
    null,
  );
  const checkHealth = useCallback(async () => {
    setHealthChecking(true);
    try {
      const status = await api.fetchSystemStatus();
      setSystemStatus(status);
      // Publish the deployment's composer-model identity for the AppHeader
      // chip. One derivation per surface: this poll is the app's single
      // /api/system/status consumer — the chip must never fetch on its own
      // (a second consumer raced sequenced test doubles and double-fetched
      // in production). Set on success only; a later failed poll keeps the
      // last known value (the backend banner owns unreachability).
      if (
        typeof status.composer_model === "string" &&
        status.composer_model.length > 0
      ) {
        useSessionStore.getState().setComposerModel(status.composer_model);
      }
      // Derive the compose abort ceiling from the deployment's configured
      // wall clock — a hard-coded client cap only satisfies the
      // client-outlives-server invariant for the checked-in defaults.
      // Latch the store readiness gate (the single source of truth) true once
      // a known-good ceiling is applied: the Send affordances (freeform,
      // guided, side-rail Apply) ungate only then, closing the bootstrap race
      // where a send started before this fetch would schedule an abort from
      // the stale default. Only ever set true — the backend wall clock does
      // not change mid-session, so a later partial health response must not
      // un-ready a composer that already knows its ceiling.
      if (
        status.composer_timeout_seconds !== undefined &&
        applyServerComposerTimeout(status.composer_timeout_seconds)
      ) {
        // Latch the reactive readiness gate — the single source of truth the
        // Send affordances subscribe to — now that a known-good ceiling is
        // applied. Only ever set true: the backend wall clock does not change
        // mid-session, so a later partial poll must not un-ready a composer that
        // already knows its ceiling (the else-guard below enforces that).
        useSessionStore.getState().setComposeTimeoutReady(true);
        useSessionStore.getState().setComposerTimeoutUnavailable(false);
      } else if (!useSessionStore.getState().composeTimeoutReady) {
        // Backend reachable but no usable composer_timeout_seconds AND no good
        // ceiling was ever latched this session — genuinely stuck. The gate must
        // stay closed (a send would schedule an abort from the stale default
        // ceiling), so latch a distinct diagnostic: the Send affordance stops
        // saying "Connecting…" and the misconfiguration is visible. Log once on
        // the false→true transition, not every poll.
        //
        // The `!composeTimeoutReady` guard is load-bearing: a partial or absent
        // response that arrives AFTER a good ceiling was latched is a transient
        // (readiness holds), so we must not flag unavailable or spam a false "no
        // usable timeout" error on a genuinely healthy composer.
        const sessionState = useSessionStore.getState();
        if (!sessionState.composerTimeoutUnavailable) {
          console.error(
            "[health-check] system status reported no usable " +
              "composer_timeout_seconds:",
            status.composer_timeout_seconds,
          );
        }
        sessionState.setComposerTimeoutUnavailable(true);
      }
      // Deploy beacon: debounce across consecutive polls so a mid-deploy
      // status flap never flashes the banner; latch once stable.
      staleBuildStreakRef.current = nextStaleBuildStreak(
        staleBuildStreakRef.current,
        ownBuild,
        status.frontend_build,
      );
      if (staleBuildStreakRef.current >= STALE_BUILD_POLLS_REQUIRED) {
        setStaleBuildDetected(true);
      }
      setBackendAvailable(true);
      setLastHealthCheckAt(null);
    } catch (err) {
      // Preserve diagnostic detail (network vs CORS vs auth vs 5xx) for
      // operators inspecting DevTools when Retry keeps failing.  The
      // user-visible signal is the role=alert banner; this is the only
      // channel that exposes the underlying cause.
      console.error("[health-check] fetchSystemStatus failed:", err);
      setSystemStatus(null);
      setBackendAvailable(false);
      // Backend unreachable: the "Backend unavailable" banner is the signal
      // now, not the composer-specific diagnostic. Clear it so a later
      // recovery does not surface a stale "composer unavailable".
      useSessionStore.getState().setComposerTimeoutUnavailable(false);
      setLastHealthCheckAt(new Date().toLocaleTimeString());
    } finally {
      setHealthChecking(false);
    }
  }, [ownBuild]);

  // Global keyboard shortcuts
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      // Ctrl+K / Cmd+K: Open command palette
      if (e.key === "k" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        setShowPalette(true);
        return;
      }

      // Ctrl+Shift+P / Cmd+Shift+P: Open plugin catalog
      if (
        e.key.toLowerCase() === "p" &&
        e.shiftKey &&
        (e.ctrlKey || e.metaKey)
      ) {
        e.preventDefault();
        if (catalogAvailable) {
          window.dispatchEvent(new CustomEvent(OPEN_CATALOG_EVENT));
        }
        return;
      }

      // Ctrl+Shift+G / Cmd+Shift+G: Select the persistent Graph artifact.
      if (
        e.key.toLowerCase() === "g" &&
        e.shiftKey &&
        (e.ctrlKey || e.metaKey)
      ) {
        e.preventDefault();
        dispatchArtifactViewIntent({
          tab: "graph",
          focusMode: false,
          sessionId: activeSessionId,
        });
        return;
      }

      // Ctrl+Shift+Y / Cmd+Shift+Y: Select the persistent YAML artifact.
      // Gated on composition content — the same hasCompositionContent
      // predicate ExportYamlButton uses — so the shortcut can't open the
      // near-empty modal on a pipeline with nothing to export
      // (elspeth-bff8043d33 residual).
      if (
        e.key.toLowerCase() === "y" &&
        e.shiftKey &&
        (e.ctrlKey || e.metaKey)
      ) {
        e.preventDefault();
        if (activeSessionId && hasCompositionContent(compositionState)) {
          dispatchArtifactViewIntent({
            tab: "yaml",
            focusMode: false,
            sessionId: activeSessionId,
          });
        }
        return;
      }

      // Ctrl+N / Cmd+N: New session
      if (e.key === "n" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        createSession();
        return;
      }

      // Ctrl+/ / Cmd+/: Focus chat input
      if (e.key === "/" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        dispatchAuthoringFocusIntent();
        return;
      }

      // Ctrl+Shift+V / Cmd+Shift+V: Validate pipeline
      if (
        e.key === "V" &&
        e.shiftKey &&
        (e.ctrlKey || e.metaKey) &&
        activeSessionId &&
        compositionState
      ) {
        e.preventDefault();
        requestValidate(activeSessionId, compositionState.version);
        return;
      }

      // Ctrl+E / Cmd+E: Execute pipeline
      if (
        e.key === "e" &&
        (e.ctrlKey || e.metaKey) &&
        activeSessionId &&
        runAdmissionAvailable
      ) {
        e.preventDefault();
        const executionReady =
          useExecutionStore.getState().validationResult?.readiness
            ?.execution_ready === true;
        if (executionReady) {
          window.dispatchEvent(new CustomEvent(REQUEST_RUN_EVENT));
        }
        return;
      }

      // ?: Show keyboard shortcuts (only when not typing in an input)
      if (e.key === "?" && !e.ctrlKey && !e.metaKey) {
        const tag = (e.target as HTMLElement)?.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA") return;
        e.preventDefault();
        setShowShortcuts(true);
        return;
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [
    activeSessionId,
    catalogAvailable,
    compositionState,
    createSession,
    runAdmissionAvailable,
  ]);

  // Initial health check and periodic polling
  useEffect(() => {
    checkHealth();

    // Set up periodic health checks
    healthCheckRef.current = window.setInterval(checkHealth, HEALTH_CHECK_INTERVAL);

    return () => {
      if (healthCheckRef.current !== null) {
        window.clearInterval(healthCheckRef.current);
      }
    };
  }, [checkHealth]);

  // Re-establish the compose-timeout gate on RE-authentication. App stays
  // mounted across auth changes (AuthGuard gates only its children), so the
  // mount effect above does not re-run on login; meanwhile logout's store reset
  // dropped composeTimeoutReady to false (and reset the module ceiling in
  // lockstep). Without this, a fresh login would sit behind a disabled Send
  // until the next 30s poll re-latched the backend ceiling. Fire only on the
  // false→true transition so the initial mount does not double-fetch.
  const wasAuthenticatedRef = useRef(isAuthenticated);
  useEffect(() => {
    if (isAuthenticated && !wasAuthenticatedRef.current) {
      void checkHealth();
    }
    wasAuthenticatedRef.current = isAuthenticated;
  }, [isAuthenticated, checkHealth]);

  const appNotices = useMemo<AppNotice[]>(() => {
    const notices: AppNotice[] = [];
    if (backendAvailable === false) {
      notices.push({
        kind: "backend-unavailable",
        role: "alert",
        content: (
          <>
            <strong>Backend unavailable</strong> — Cannot connect to the
            ELSPETH server. Check that the backend is running.
            {lastHealthCheckAt !== null ? (
              <> Last attempt: {lastHealthCheckAt}.</>
            ) : null}
          </>
        ),
        action: (
          <button
            type="button"
            onClick={checkHealth}
            disabled={healthChecking}
            aria-busy={healthChecking}
            aria-label="Retry connection"
            title="Retry connection"
            className="alert-banner-action"
          >
            {healthChecking ? "Checking…" : "Retry"}
          </button>
        ),
      });
    }
    if (preferencesWriteError !== null) {
      notices.push({
        kind: "preferences",
        role: "alert",
        content: (
          <>
            <strong>Preferences:</strong> {preferencesWriteError}
          </>
        ),
      });
    }
    if (redirectToast !== null) {
      notices.push({
        kind: "redirect",
        role: "alert",
        tone: "info",
        content: redirectToast.message,
        action: (
          <button
            type="button"
            className="alert-banner-action"
            onClick={redirectToast.dismiss}
            aria-label="Dismiss"
          >
            Dismiss
          </button>
        ),
      });
    }
    if (staleBuildDetected) {
      notices.push({
        kind: "stale-build",
        role: "status",
        content: "A new version of ELSPETH is available — refresh to load it.",
        action: (
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="alert-banner-action"
          >
            Refresh
          </button>
        ),
      });
    }
    if (
      backendAvailable &&
      systemStatus !== null &&
      !systemStatus.composer_available
    ) {
      notices.push({
        kind: "composer-unavailable",
        role: "status",
        content: (
          <>
            Service unavailable:{" "}
            {systemStatus.composer_reason ??
              "The composer cannot reach a usable LLM right now."}
          </>
        ),
        action: (
          <button
            type="button"
            onClick={openSecrets}
            aria-label="Open secrets settings"
            title="Configure API keys"
            className="alert-banner-action"
          >
            ⚙ API Keys
          </button>
        ),
      });
    }
    return notices;
  }, [
    backendAvailable,
    checkHealth,
    healthChecking,
    lastHealthCheckAt,
    openSecrets,
    preferencesWriteError,
    redirectToast,
    staleBuildDetected,
    systemStatus,
  ]);

  // Phase 6B Task 8 short-circuit: if the URL hash is `#/shared/{token}`,
  // render the read-only inspect view inside AuthGuard. The token is a
  // CAPABILITY, not an authenticator — the recipient must still be logged
  // in. AuthGuard preserves the hash through the login redirect, so the
  // reviewer lands back here after authenticating.
  if (sharedToken !== null) {
    return (
      <AuthGuard>
        <div className="app-root app-root--shared-inspect">
          <SharedInspectView token={sharedToken} />
        </div>
      </AuthGuard>
    );
  }

  return (
    <AuthGuard>
      <div className="app-root">
        <a
          href="#composer-main"
          className="skip-to-content"
          onClick={focusMainContent}
        >
          Skip to main content
        </a>
        <h1 className="sr-only">ELSPETH Pipeline Composer</h1>

        <AppNoticeCenter notices={appNotices} />
        <AppHeader
          onOpenSettings={openComposerSettings}
          onSignOut={logout}
        />
        {showTutorial ? (
          <div id="composer-main" className="app-main" tabIndex={-1}>
            <HelloWorldTutorial
              key={tutorialResetEpoch}
              composerAvailable={systemStatus?.composer_available ?? false}
              composerUnavailableReason={systemStatus?.composer_reason ?? null}
              tutorialReady={systemStatus?.tutorial_ready ?? false}
              tutorialUnavailableReason={systemStatus?.tutorial_reason ?? null}
            />
          </div>
        ) : showEmptyLanding ? (
          <div
            id="composer-main"
            className="app-main"
            role="main"
            tabIndex={-1}
          >
            <section
              className="empty-landing"
              aria-labelledby="empty-landing-title"
            >
              <h2 id="empty-landing-title">No sessions yet</h2>
              <p>
                Create a session to start composing a pipeline, or browse the
                plugin catalog to see what ELSPETH can work with.
              </p>
              <div className="empty-landing-actions">
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => void createSession()}
                >
                  + New session
                </button>
                <button
                  type="button"
                  className="btn"
                  onClick={() => setCatalogOpen(true)}
                >
                  Browse the catalog
                </button>
              </div>
            </section>
          </div>
        ) : (
          <div
            id="composer-main"
            className="app-main"
            role="main"
            tabIndex={-1}
          >
            <ComposerWorkspace
              authoring={<ChatPanel onOpenSecrets={openSecrets} />}
              authoringStatus={<DefaultModeChangedBanner />}
              collapsedStatus={<CollapsedAuthoringStatus />}
              artifact={<ArtifactWorkspace />}
              inspector={<WorkspaceInspector />}
              actionBar={
                <WorkspaceActionBar
                  capabilities={workspaceActionCapabilities}
                />
              }
            />
          </div>
        )}

        {showSecrets && <SecretsPanel onClose={closeSecrets} />}
        <GraphModal />
        <ImportYamlModalHost />
        {/* Phase 6B Task 4: mount the SaveForReviewDialog at app-root level so
         *  CompletionBar's Save-for-review verb can open it regardless of
         *  which view is currently focused. The dialog reads its state from
         *  useShareableReviewStore; it renders null when dialogOpen=false. */}
        <SaveForReviewDialog />
        <CatalogDrawer
          isOpen={catalogOpen}
          onClose={() => setCatalogOpen(false)}
        />
        {showComposerSettings && (
          <ComposerPreferencesPanel
            onClose={closeComposerSettings}
            onResetTutorialComplete={handleResetTutorialComplete}
          />
        )}
        <CommandPalette
          isOpen={showPalette}
          onClose={closePalette}
          runAdmissionAvailable={runAdmissionAvailable}
        />
        {showShortcuts && (
          <ShortcutsHelp onClose={() => setShowShortcuts(false)} />
        )}
        <RecoveryPanel
          activeSessionId={activeSessionId}
          currentState={compositionState}
          recoveryError={recoveryError}
          onApply={applyRecoveredState}
          onDiscard={discardRecovery}
        />
        {pendingFanoutGuard && (
          <ConfirmDialog
            title="Review LLM provider calls"
            message={pendingFanoutGuard.summary}
            confirmLabel="Execute"
            cancelLabel="Cancel"
            variant="danger"
            onConfirm={confirmFanoutExecution}
            onCancel={dismissFanoutGuard}
          />
        )}
      </div>
    </AuthGuard>
  );
}

export default App;
