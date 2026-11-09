import { SSO_CALLBACK_HASH_PREFIX } from "@/components/auth/ssoCallback";

export const POST_LOGIN_REDIRECT_KEY = "elspeth_post_login_redirect";
const SHARED_HASH_PREFIX = "#/shared/";

export type SharedRoute =
  | { kind: "none" }
  | { kind: "invalid" }
  | { kind: "valid"; token: string };

export interface SessionHashState {
  sessionId: string | null;
  verb: string | null;
}

export function isAuthCallbackHash(hash: string): boolean {
  return hash === SSO_CALLBACK_HASH_PREFIX || hash.startsWith(`${SSO_CALLBACK_HASH_PREFIX}?`);
}

export function isSharedRouteHash(hash: string): boolean {
  return hash === "#/shared" || hash.startsWith(SHARED_HASH_PREFIX);
}

export function parseSharedRoute(hash: string): SharedRoute {
  if (!isSharedRouteHash(hash)) return { kind: "none" };
  const raw = hash.slice(SHARED_HASH_PREFIX.length);
  if (raw === "") return { kind: "invalid" };
  try {
    return { kind: "valid", token: decodeURIComponent(raw) };
  } catch {
    return { kind: "invalid" };
  }
}

export function parseSessionHash(hash: string): SessionHashState {
  if (hash === "#composer-main" || isSharedRouteHash(hash) || isAuthCallbackHash(hash)) {
    return { sessionId: null, verb: null };
  }
  const match = hash.match(/^#\/([^/]+?)(?:\/([a-z]+))?$/);
  return match
    ? { sessionId: match[1], verb: match[2] ?? null }
    : { sessionId: null, verb: null };
}

export function isNavigationDestination(hash: string): boolean {
  return isSharedRouteHash(hash) || parseSessionHash(hash).sessionId !== null;
}
