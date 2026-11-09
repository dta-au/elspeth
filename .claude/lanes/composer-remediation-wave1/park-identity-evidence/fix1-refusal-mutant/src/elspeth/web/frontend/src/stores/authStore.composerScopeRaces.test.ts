import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAuthStore } from "./authStore";
import { purgeComposerCustody } from "./composerOperationCustody";
vi.mock("./sessionStore", () => ({ useSessionStore: { getState: () => ({ activeSessionId: null, reset: vi.fn(), resumeComposerOperation: vi.fn(), reconcileInactiveComposerCustody: vi.fn() }) } }));
const profile = { user_id: "alice", username: "alice", display_name: null, email: null, groups: [], dev_admin: false };
beforeEach(() => { localStorage.clear(); purgeComposerCustody(); useAuthStore.setState(useAuthStore.getInitialState(), true); });
afterEach(() => { vi.unstubAllGlobals(); purgeComposerCustody(); });
it.each(["token", "password", "storage"])("superseded %s profile does not dispatch a fresh configuration read", async (kind) => {
  let releaseOld!: (response: Response) => void;
  let profiles = 0, configurations = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (url === "/api/auth/login") return new Response(JSON.stringify({ access_token: "old-token" }));
    if (url === "/api/auth/me") {
      if (++profiles === 1) return new Promise<Response>((resolve) => { releaseOld = resolve; });
      return new Response(JSON.stringify(profile));
    }
    if (url === "/api/auth/config") { configurations++; return new Response(JSON.stringify({ provider: "local", registration_mode: "closed", sso_start_url: null })); }
    throw new Error(`Unexpected request: ${url}`);
  }));
  localStorage.setItem("auth_token", "old-token");
  const oldLogin = kind === "password" ? useAuthStore.getState().login("alice", "password") : kind === "storage" ? useAuthStore.getState().loadFromStorage() : useAuthStore.getState().loginWithToken("old-token");
  await vi.waitFor(() => expect(profiles).toBe(1));
  await useAuthStore.getState().loginWithToken("new-token");
  expect(configurations).toBe(1);
  releaseOld(new Response(JSON.stringify({ ...profile, username: "old-user" }))); await oldLogin;
  expect(configurations).toBe(1);
  expect(useAuthStore.getState().token).toBe("new-token");
  expect(useAuthStore.getState().user).toEqual(profile);
});
