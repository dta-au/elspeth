/**
 * Hash-based router for session deep linking.
 *
 * Format: #/{sessionId}                 -> canonical
 *         #/{sessionId}/graph           -> select Graph artifact, then rewrite
 *         #/{sessionId}/spec            -> select Spec artifact, then rewrite
 *         #/{sessionId}/yaml            -> select YAML artifact, then rewrite
 *         #/{sessionId}/runs            -> select Run artifact, then rewrite
 *         #/{sessionId}/{anything-else} -> silently strip the verb
 *         #composer-main                -> IGNORED — owned by App's skip link
 *         #/shared/{token}              -> IGNORED — owned by useSharedToken
 *                                          (Phase 6B Task 8); the hook below
 *                                          short-circuits without touching the
 *                                          hash so SharedInspectView keeps
 *                                          control of the URL.
 *
 * Phase 3B uses action fragments rather than steady-state view fragments.
 * The fragment is an arrival action, not steady-state URL state.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  REQUEST_ARTIFACT_VIEW_EVENT,
  type RequestArtifactViewDetail,
} from "@/lib/composer-events";
import { useSessionStore } from "@/stores/sessionStore";
import { hasCompositionContent } from "@/utils/compositionState";
import type { ArtifactTab } from "@/components/workspace/workspaceTypes";

interface HashState {
  sessionId: string | null;
  verb: string | null;
}

interface RedirectToast {
  message: string;
  dismiss: () => void;
}

const ACTION_VERBS: Record<string, ArtifactTab> = {
  graph: "graph",
  spec: "spec",
  yaml: "yaml",
  runs: "run",
};

/**
 * Verbs that were valid tabs in earlier versions but have since been removed.
 * When detected, a one-time dismissible toast is shown to the user.
 * All other unrecognized verbs are silently stripped (backward compat).
 */
const RETIRED_VERBS: Record<string, string> = {};

const TOAST_DISMISSED_KEY = "elspeth_redirect_toast_dismissed";

const SHARED_HASH_PREFIX = "#/shared/";
const COMPOSER_MAIN_HASH = "#composer-main";

/** True when the current hash is a Phase 6B shared-inspect route. The
 *  session router short-circuits on this so SharedInspectView keeps
 *  control of the URL and the session store is not mutated. */
function _isSharedRoute(hash: string): boolean {
  return hash.startsWith(SHARED_HASH_PREFIX);
}

function _isNonRoutingHash(hash: string): boolean {
  return hash === COMPOSER_MAIN_HASH || _isSharedRoute(hash);
}

function parseHash(): HashState {
  const hash = window.location.hash;
  if (_isNonRoutingHash(hash)) return { sessionId: null, verb: null };
  const match = hash.match(/^#\/([^/]+?)(?:\/([a-z]+))?$/);
  if (!match) return { sessionId: null, verb: null };
  return { sessionId: match[1], verb: match[2] ?? null };
}

function buildCanonicalHash(sessionId: string | null): string {
  return sessionId ? `#/${sessionId}` : "";
}

interface UseHashRouterOptions {
  enabled?: boolean;
}

export function useHashRouter(
  options: UseHashRouterOptions = {},
): { redirectToast: RedirectToast | null } {
  const enabled = options.enabled ?? true;
  const lastWrittenHash = useRef<string>("");
  const applying = useRef(false);
  const pendingArtifactIntent = useRef<{
    sessionId: string;
    tab: "spec" | "yaml";
  } | null>(null);
  const pendingIntentUnsubscribe = useRef<(() => void) | null>(null);

  const cancelPendingArtifactIntent = useCallback((): void => {
    pendingArtifactIntent.current = null;
    pendingIntentUnsubscribe.current?.();
    pendingIntentUnsubscribe.current = null;
  }, []);

  const dispatchArtifactIntent = useCallback((
    sessionId: string,
    tab: ArtifactTab,
  ): void => {
    window.dispatchEvent(
      new CustomEvent<RequestArtifactViewDetail>(REQUEST_ARTIFACT_VIEW_EVENT, {
        detail: { tab, focusMode: false, sessionId },
      }),
    );
  }, []);

  const requestArtifact = useCallback((sessionId: string, tab: ArtifactTab): void => {
    cancelPendingArtifactIntent();
    if (tab !== "spec" && tab !== "yaml") {
      queueMicrotask(() => dispatchArtifactIntent(sessionId, tab));
      return;
    }

    const dispatchIfAvailable = (): boolean => {
      const state = useSessionStore.getState();
      if (
        state.activeSessionId !== sessionId ||
        !state.compositionStateLoaded
      ) {
        return false;
      }
      if (hasCompositionContent(state.compositionState)) {
        dispatchArtifactIntent(sessionId, tab);
      }
      return true;
    };
    if (dispatchIfAvailable()) return;

    const intent = { sessionId, tab };
    pendingArtifactIntent.current = intent;
    pendingIntentUnsubscribe.current = useSessionStore.subscribe((state) => {
      if (pendingArtifactIntent.current !== intent) return;
      if (state.activeSessionId !== sessionId) {
        cancelPendingArtifactIntent();
        return;
      }
      if (!state.compositionStateLoaded) return;
      pendingIntentUnsubscribe.current?.();
      pendingIntentUnsubscribe.current = null;
      queueMicrotask(() => {
        if (pendingArtifactIntent.current !== intent) return;
        const committedState = useSessionStore.getState();
        cancelPendingArtifactIntent();
        if (
          committedState.activeSessionId === sessionId &&
          committedState.compositionStateLoaded &&
          hasCompositionContent(committedState.compositionState)
        ) {
          dispatchArtifactIntent(sessionId, tab);
        }
      });
    });
  }, [cancelPendingArtifactIntent, dispatchArtifactIntent]);

  // Read dismissal flag once at mount. Using a ref rather than reading
  // localStorage on every applyHash invocation avoids repeated storage reads.
  const dismissedRef = useRef<boolean>(
    typeof window !== "undefined" &&
      window.localStorage.getItem(TOAST_DISMISSED_KEY) === "1",
  );

  const [redirectToast, setRedirectToast] = useState<RedirectToast | null>(
    null,
  );

  const applyHash = useCallback((state: HashState) => {
    // Shared inspection and the App-owned skip target are non-routing hashes.
    // Their owners keep control of navigation/focus, so applying either is a
    // no-op and cannot unbind the active Composer session.
    if (_isNonRoutingHash(window.location.hash)) {
      return;
    }
    applying.current = true;
    try {
      const { sessionId, verb } = state;
      const store = useSessionStore.getState();

      if (sessionId && sessionId !== store.activeSessionId) {
        store.selectSession(sessionId);
      } else if (!sessionId && store.activeSessionId) {
        store.unbindMissingSession(store.activeSessionId);
      }

      // Fix A: use hasOwnProperty to avoid prototype-chain walk.
      // The `in` operator walks the prototype chain: `"constructor" in ACTION_VERBS`
      // is true even though "constructor" is not an own property of ACTION_VERBS.
      // `ACTION_VERBS["constructor"]` returns the Object constructor function and
      // `new CustomEvent(fn)` would coerce it to a garbage event name.
      // Object.prototype.hasOwnProperty.call() is the ES2020-compatible guard.
      const hasOwn = Object.prototype.hasOwnProperty;
      if (verb && sessionId && hasOwn.call(ACTION_VERBS, verb)) {
        requestArtifact(sessionId, ACTION_VERBS[verb]);
      }

      // Fix C: retired-verb redirect toast. Only "runs" and "spec" trigger
      // the toast; all other unrecognized verbs are silently stripped.
      if (verb && hasOwn.call(RETIRED_VERBS, verb) && !dismissedRef.current) {
        const message = RETIRED_VERBS[verb];
        setRedirectToast({
          message,
          dismiss: () => {
            dismissedRef.current = true;
            window.localStorage.setItem(TOAST_DISMISSED_KEY, "1");
            setRedirectToast(null);
          },
        });
      }

      const canonical = buildCanonicalHash(sessionId);
      if (canonical !== window.location.hash) {
        lastWrittenHash.current = canonical;
        window.history.replaceState(
          null,
          "",
          canonical || window.location.pathname,
        );
      }
    } finally {
      // Fix B: guarantee the flag is cleared even if selectSession throws.
      // Without this, applying.current stays true permanently and the URL-echo
      // subscription (useEffect #3) becomes a no-op until page reload.
      applying.current = false;
    }
  }, [requestArtifact]);

  useEffect(() => {
    if (!enabled) return;
    if (_isNonRoutingHash(window.location.hash)) return;
    const initial = parseHash();
    if (initial.sessionId) {
      lastWrittenHash.current = window.location.hash;
      applyHash(initial);
    } else {
      const { activeSessionId } = useSessionStore.getState();
      if (activeSessionId) {
        const hash = buildCanonicalHash(activeSessionId);
        lastWrittenHash.current = hash;
        window.history.replaceState(
          null,
          "",
          hash || window.location.pathname,
        );
      }
    }
  }, [applyHash, enabled]);

  useEffect(
    () => () => {
      cancelPendingArtifactIntent();
    },
    [cancelPendingArtifactIntent],
  );

  useEffect(() => {
    if (!enabled) return;
    function handleHashChange() {
      const newHash = window.location.hash;
      if (newHash === lastWrittenHash.current) return;
      lastWrittenHash.current = newHash;
      applyHash(parseHash());
    }

    window.addEventListener("popstate", handleHashChange);
    window.addEventListener("hashchange", handleHashChange);
    return () => {
      window.removeEventListener("popstate", handleHashChange);
      window.removeEventListener("hashchange", handleHashChange);
    };
  }, [applyHash, enabled]);

  useEffect(() => {
    if (!enabled) return;
    const unsub = useSessionStore.subscribe((state, prevState) => {
      if (applying.current) return;
      if (state.activeSessionId === prevState.activeSessionId) return;
      // Non-routing hashes remain owned by their view/focus surface. The
      // session store may legitimately change activeSessionId via background
      // bootstrap while one is live; that must not strip the shared token or
      // reinterpret the skip target as session navigation.
      if (_isNonRoutingHash(window.location.hash)) return;

      const hash = buildCanonicalHash(state.activeSessionId);
      if (hash === lastWrittenHash.current) return;
      lastWrittenHash.current = hash;

      if (hash) {
        window.history.pushState(null, "", hash);
      } else {
        window.history.replaceState(null, "", window.location.pathname);
      }
    });
    return unsub;
  }, [enabled]);

  useEffect(() => {
    if (!enabled) return;
    const unsub = useSessionStore.subscribe((state, prevState) => {
      if (prevState.sessions.length > 0 || state.sessions.length === 0) return;

      const { sessionId } = parseHash();
      if (!sessionId) return;

      const exists = state.sessions.some((s) => s.id === sessionId);
      if (!exists && state.activeSessionId === sessionId) {
        lastWrittenHash.current = "";
        window.history.replaceState(null, "", window.location.pathname);
        useSessionStore.getState().unbindMissingSession(sessionId);
      }
    });
    return unsub;
  }, [enabled]);

  return { redirectToast };
}
