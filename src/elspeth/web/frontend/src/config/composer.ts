export const COMPOSE_CLIENT_GRACE_MS = 25_000;
export const DEFAULT_COMPOSE_TIMEOUT_MS = 270_000 + COMPOSE_CLIENT_GRACE_MS;

/** Transient boot window: the backend wall clock has not landed yet. */
export const COMPOSE_CONNECTING_MESSAGE = "Connecting to the composer…";
/** Stuck state: backend reachable but it reported no usable compose timeout. */
export const COMPOSE_UNAVAILABLE_MESSAGE =
  "Composer unavailable — the server did not report a compose timeout.";

let composeTimeoutMs = DEFAULT_COMPOSE_TIMEOUT_MS;

/** Current compose abort ceiling. Read at CALL time (useComposer), never
 * cached at module load, so the boot-applied server value governs every
 * send that starts after it lands. */
export function getComposeTimeoutMs(): number {
  return composeTimeoutMs;
}

/**
 * Derive the abort ceiling from the backend's configured compose wall
 * clock (seconds, from GET /api/system/status). Non-finite or non-positive
 * values are ignored — the current ceiling (default 295s) is a safe floor,
 * and a garbage value must not shrink the guard below the backend wall.
 *
 * Returns whether a valid value was applied. App.checkHealth uses the return to
 * latch sessionStore.composeTimeoutReady — the single reactive source of truth
 * for "a known-good ceiling exists" — so a garbage value leaves BOTH the ceiling
 * and readiness untouched.
 */
export function applyServerComposerTimeout(
  backendTimeoutSeconds: number,
): boolean {
  if (
    !Number.isFinite(backendTimeoutSeconds) ||
    backendTimeoutSeconds <= 0
  ) {
    return false;
  }
  composeTimeoutMs =
    Math.round(backendTimeoutSeconds * 1000) + COMPOSE_CLIENT_GRACE_MS;
  return true;
}

/** Test-only: restore the ceiling to its boot default. */
export function resetComposeTimeoutForTests(): void {
  composeTimeoutMs = DEFAULT_COMPOSE_TIMEOUT_MS;
}

export async function runComposeWithTimeout(
  controllerRef: { current: AbortController | null },
  ready: boolean,
  runner: (signal: AbortSignal) => Promise<void>,
): Promise<void> {
  if (!ready) {
    return;
  }
  const controller = new AbortController();
  controllerRef.current = controller;
  const timer = setTimeout(
    () => controller.abort(COMPOSE_TIMEOUT_ABORT_REASON),
    // Read at call time: the ceiling is derived from the backend's configured
    // wall clock once /api/system/status lands at boot, and readiness above
    // guarantees that has happened before we reach here.
    getComposeTimeoutMs(),
  );
  try {
    await runner(controller.signal);
  } finally {
    clearTimeout(timer);
    if (controllerRef.current === controller) {
      controllerRef.current = null;
    }
  }
}

// Abort reasons are internal frontend control-plane values. They let
// sessionStore distinguish a user-requested stop from the timeout guard while
// still using the browser's native AbortController path.
export const COMPOSE_TIMEOUT_ABORT_REASON = "compose_timeout";
export const COMPOSE_USER_CANCEL_ABORT_REASON = "compose_user_cancel";
