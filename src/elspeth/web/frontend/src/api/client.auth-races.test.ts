import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fetchAuthConfig, fetchCurrentUser } from "./client";
import { fetchMailboxSummary } from "./workflow";
import { useAuthStore } from "@/stores/authStore";
import { useBlobStore } from "@/stores/blobStore";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { resetStore } from "@/test/store-helpers";

const profile = { user_id: "alice", username: "alice", display_name: null, email: null, groups: [], dev_admin: false };
function response(status: number) {
  return new Response(JSON.stringify(status === 200 ? profile : { detail: "Unauthorized" }), { status });
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe("credential ownership of delayed responses", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });
  beforeEach(() => {
    resetStore(useAuthStore);
    localStorage.clear();
    vi.stubGlobal("fetch", vi.fn());
  });

  it("logs out the credential actually rejected by the server", async () => {
    localStorage.setItem("auth_token", "old-token");
    useAuthStore.setState({ token: "old-token" });
    vi.mocked(fetch).mockResolvedValueOnce(response(401));
    await expect(fetchCurrentUser()).rejects.toMatchObject({ status: 401 });
    expect(useAuthStore.getState().token).toBeNull();
    expect(localStorage.getItem("auth_token")).toBeNull();
  });

  it.each([
    ["profile", fetchCurrentUser],
    ["workflow", fetchMailboxSummary],
    ["unauthenticated", fetchAuthConfig],
  ])("preserves replacement login after a delayed %s 401", async (_name, request) => {
    const old = deferred<Response>();
    localStorage.setItem("auth_token", "old-token");
    useAuthStore.setState({ token: "old-token" });
    vi.mocked(fetch).mockReturnValueOnce(old.promise).mockResolvedValueOnce(response(200));
    const pending = request();
    await useAuthStore.getState().loginWithToken("new-token");
    old.resolve(response(401));
    await expect(pending).rejects.toMatchObject({ status: 401 });
    expect(useAuthStore.getState().token).toBe("new-token");
    expect(localStorage.getItem("auth_token")).toBe("new-token");
  });

  it("fences a new login even if it receives the same token", async () => {
    const old = deferred<Response>();
    localStorage.setItem("auth_token", "same-token");
    useAuthStore.setState({ token: "same-token" });
    vi.mocked(fetch).mockReturnValueOnce(old.promise).mockResolvedValueOnce(response(200));
    const pending = fetchCurrentUser();
    await useAuthStore.getState().loginWithToken("same-token");
    old.resolve(response(401));
    await expect(pending).rejects.toMatchObject({ status: 401 });
    expect(useAuthStore.getState().token).toBe("same-token");
  });

  it.each([200, 401])("ignores a superseded login profile response (%s)", async (status) => {
    const old = deferred<Response>();
    vi.mocked(fetch).mockReturnValueOnce(old.promise).mockResolvedValueOnce(response(200));
    const pending = useAuthStore.getState().loginWithToken("old-token");
    await useAuthStore.getState().loginWithToken("new-token");
    old.resolve(status === 200 ? new Response(JSON.stringify({ ...profile, username: "old-user" })) : response(status));
    await pending;
    expect(useAuthStore.getState().token).toBe("new-token");
    expect(useAuthStore.getState().user?.username).toBe("alice");
  });

  it("clears old caches before a replacement login publishes its credential", async () => {
    useBlobStore.setState({ activeSessionId: "old-session", error: "old failure" });
    useInterpretationEventsStore.setState({ terminalIdsBySession: { previous: new Set(["old event"]) } });
    const observedSessions: (string | null)[] = [];
    const unsubscribe = useAuthStore.subscribe((state) => {
      if (state.token === "new-token") observedSessions.push(useBlobStore.getState().activeSessionId);
    });
    vi.mocked(fetch).mockResolvedValueOnce(response(200));
    const logout = useAuthStore.getState().logout();
    const duplicateLogout = useAuthStore.getState().logout();
    await useAuthStore.getState().loginWithToken("new-token");
    await Promise.all([logout, duplicateLogout]);
    unsubscribe();
    expect(observedSessions.length).toBeGreaterThan(0);
    expect(observedSessions.every((session) => session === null)).toBe(true);
    expect(useBlobStore.getState().error).toBeNull();
    expect(useInterpretationEventsStore.getState().terminalIdsBySession).toEqual({});
    expect(useAuthStore.getState().token).toBe("new-token");
    expect(useAuthStore.getState().user).toEqual(profile);
  });

  it("clears interpretation snapshots and pending request custody on logout", async () => {
    useInterpretationEventsStore.setState({
      refreshRequestBySession: { previous: Symbol("old request") },
      terminalIdsBySession: { previous: new Set(["old event"]) },
    });
    await useAuthStore.getState().logout();
    expect(useInterpretationEventsStore.getState()).toEqual(useInterpretationEventsStore.getInitialState());
  });

  it.each(["password", "storage"])("ignores superseded %s login failure", async (kind) => {
    const old = deferred<Response>();
    localStorage.setItem("auth_token", "old-token");
    vi.mocked(fetch).mockReturnValueOnce(old.promise).mockResolvedValueOnce(response(200));
    const pending = kind === "password"
      ? useAuthStore.getState().login("alice", "wrong")
      : useAuthStore.getState().loadFromStorage();
    await useAuthStore.getState().loginWithToken("new-token");
    old.resolve(response(401));
    await pending;
    expect(useAuthStore.getState().token).toBe("new-token");
    expect(useAuthStore.getState().loginError).toBeNull();
  });
});
