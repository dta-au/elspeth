/**
 * The SPA's half of the SSO handoff: the `#/auth/callback` hash route.
 *
 * The backend's callback never hands the browser a bearer token. It hands it a
 * single-use HANDOFF CODE in the URL FRAGMENT — browsers do not send the
 * fragment, so neither the load balancer nor uvicorn logs it — and the SPA
 * exchanges that code for the session token with POST /api/auth/sso/complete.
 * A refused login arrives on the same route as `?error=<category>`, category
 * only: the backend never reflects IdP-supplied text, and neither does this.
 *
 * Spec: docs/specs/2026-09-02-pluggable-sso-design.md §"Hash route
 * `#/auth/callback`". Two rules from it are load-bearing here:
 *
 * 1. `history.replaceState` runs BEFORE any network call, so the code is out
 *    of the address bar (and out of a later back/forward, bookmark or share)
 *    before it is spent.
 * 2. Exactly one route, exactly one parser. Success and failure differ only
 *    in which parameter is present; anything else is malformed and refused.
 *
 * This is a leaf module — no React, no store — so the parser is the single
 * authority both the page and its tests reach for, and the Python parity
 * test can pin the category table against `SSO_FAILURE_CATEGORIES` in
 * `web/auth/sso.py` by reading this file.
 */

export const SSO_CALLBACK_HASH_PREFIX = "#/auth/callback";

/** The backend's `SsoCompleteRequest` bound: `Field(min_length=1, max_length=128)`. */
const HANDOFF_CODE_MAX_LENGTH = 128;
/** `secrets.token_urlsafe` alphabet. A code from anywhere else is not ours. */
const HANDOFF_CODE = /^[A-Za-z0-9_-]{1,128}$/;
/** Nothing legitimate on this route is anywhere near this large. */
const CALLBACK_MAX_BYTES = 64 * 1024;

/**
 * The closed failure vocabulary, mirrored from `SSO_FAILURE_CATEGORIES` in
 * `web/auth/sso.py` and pinned there by a parity test. Each maps to the
 * sentence the person sees. The wording is deliberately generic for the
 * mechanical refusals — a login page must not teach an attacker which check
 * their forgery failed — and specific only where the person can act on it.
 */
export const SSO_FAILURE_MESSAGES = {
  sso_cookie_missing: "Single sign-on failed. Please try again.",
  sso_cookie_invalid: "Single sign-on failed. Please try again.",
  sso_state_mismatch: "Single sign-on failed. Please try again.",
  sso_idp_error: "The identity provider did not complete sign-in. Please try again.",
  sso_token_exchange_failed: "Single sign-on failed. Please try again.",
  sso_id_token_invalid: "Single sign-on failed. Please try again.",
  sso_claim_check_failed: "Single sign-on failed. Please try again.",
  sso_userinfo_invalid: "Single sign-on failed. Please try again.",
  sso_identity_disabled: "This account has been disabled. Contact an administrator.",
  sso_access_pending: "Your access is awaiting approval by an administrator.",
  sso_handoff_invalid: "Single sign-on failed. Please try again.",
  provider_unavailable: "The identity provider is unavailable. Please try again later.",
} as const;

export type SsoFailureCategory = keyof typeof SSO_FAILURE_MESSAGES;

/** The generic sentence, for a callback that is on the route but not of either shape. */
export const SSO_GENERIC_FAILURE = "Single sign-on failed. Please try again.";

export type SsoCallbackOutcome =
  | { kind: "code"; code: string }
  | { kind: "error"; category: SsoFailureCategory }
  | { kind: "malformed" };

function isFailureCategory(value: string): value is SsoFailureCategory {
  return Object.prototype.hasOwnProperty.call(SSO_FAILURE_MESSAGES, value);
}

/**
 * Parse a location hash. `null` means "not the callback route at all" — the
 * page then behaves as if no callback happened. Anything ON the route that is
 * not exactly one well-formed `code` or exactly one known `error` is
 * `malformed`: refused, never partially adopted.
 */
export function parseSsoCallbackHash(hash: string): SsoCallbackOutcome | null {
  if (hash !== SSO_CALLBACK_HASH_PREFIX && !hash.startsWith(`${SSO_CALLBACK_HASH_PREFIX}?`)) {
    return null;
  }
  if (new TextEncoder().encode(hash).byteLength > CALLBACK_MAX_BYTES) {
    return { kind: "malformed" };
  }
  const params = new URLSearchParams(hash.slice(SSO_CALLBACK_HASH_PREFIX.length + 1));
  const codes = params.getAll("code");
  const errors = params.getAll("error");
  const otherKeys = [...params.keys()].filter((key) => key !== "code" && key !== "error");

  if (otherKeys.length > 0) return { kind: "malformed" };
  if (codes.length === 1 && errors.length === 0) {
    const code = codes[0];
    if (code.length > HANDOFF_CODE_MAX_LENGTH || !HANDOFF_CODE.test(code)) {
      return { kind: "malformed" };
    }
    return { kind: "code", code };
  }
  if (errors.length === 1 && codes.length === 0 && isFailureCategory(errors[0])) {
    return { kind: "error", category: errors[0] };
  }
  return { kind: "malformed" };
}

/**
 * Read the callback out of the address bar and scrub it, synchronously, in
 * one step. The scrub happens whether or not the callback parses: a
 * malformed callback is still something that should not survive a reload.
 */
export function captureSsoCallback(): SsoCallbackOutcome | null {
  const outcome = parseSsoCallbackHash(window.location.hash);
  if (outcome === null) return null;
  window.history.replaceState(null, "", window.location.pathname + window.location.search);
  return outcome;
}

/** The sentence for a refused callback. Total over the outcomes that are not a code. */
export function ssoFailureMessage(outcome: Exclude<SsoCallbackOutcome, { kind: "code" }>): string {
  return outcome.kind === "error" ? SSO_FAILURE_MESSAGES[outcome.category] : SSO_GENERIC_FAILURE;
}
