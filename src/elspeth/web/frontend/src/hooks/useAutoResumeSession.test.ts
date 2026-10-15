import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useSessionStore } from "@/stores/sessionStore";
import { resetStore } from "@/test/store-helpers";
import { useAutoResumeSession } from "./useAutoResumeSession";

function seedSessions() {
  useSessionStore.setState({
    sessionsLoaded: true,
    activeSessionId: null,
    sessions: [
      { id: "old", title: "Older", updated_at: "2026-06-01T00:00:00Z" },
      { id: "recent", title: "Newest", updated_at: "2026-07-01T00:00:00Z" },
      {
        id: "archived-newest",
        title: "Archived but newest",
        updated_at: "2026-07-02T00:00:00Z",
        archived: true,
      },
    ],
  } as never);
}

describe("useAutoResumeSession", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
    window.history.replaceState(null, "", window.location.pathname);
    window.sessionStorage.clear();
  });

  it("selects the most recently active non-archived session once sessions load", () => {
    const selectSession = vi.fn();
    seedSessions();
    useSessionStore.setState({ selectSession } as never);

    renderHook(() => useAutoResumeSession(true));

    expect(selectSession).toHaveBeenCalledTimes(1);
    expect(selectSession).toHaveBeenCalledWith("recent");
  });

  it("does nothing while disabled (tutorial / prefs unsettled / shared route)", () => {
    const selectSession = vi.fn();
    seedSessions();
    useSessionStore.setState({ selectSession } as never);

    renderHook(() => useAutoResumeSession(false));

    expect(selectSession).not.toHaveBeenCalled();
  });

  it("resumes when the hook becomes enabled after preferences settle", () => {
    const selectSession = vi.fn();
    seedSessions();
    useSessionStore.setState({ selectSession } as never);

    const { rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useAutoResumeSession(enabled),
      { initialProps: { enabled: false } },
    );
    expect(selectSession).not.toHaveBeenCalled();

    rerender({ enabled: true });
    expect(selectSession).toHaveBeenCalledWith("recent");
  });

  it("does nothing before the session list has loaded", () => {
    const selectSession = vi.fn();
    seedSessions();
    useSessionStore.setState({ selectSession, sessionsLoaded: false } as never);

    renderHook(() => useAutoResumeSession(true));

    expect(selectSession).not.toHaveBeenCalled();
  });

  it("stands down when the URL hash names a session (router jurisdiction)", () => {
    const selectSession = vi.fn();
    seedSessions();
    useSessionStore.setState({ selectSession } as never);
    window.history.replaceState(null, "", "#/deep-linked-session");

    renderHook(() => useAutoResumeSession(true));

    expect(selectSession).not.toHaveBeenCalled();
  });

  it("remembers an arrival deep link rejected before bootstrap enables resume", () => {
    const selectSession = vi.fn();
    seedSessions();
    useSessionStore.setState({ selectSession } as never);
    window.history.replaceState(null, "", "#/unavailable-session");
    const { rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useAutoResumeSession(enabled),
      { initialProps: { enabled: false } },
    );
    // A 404 or the session-list router can clear the fragment while auth or
    // preferences are still loading. That must not turn arrival into resume.
    window.history.replaceState(null, "", "/");
    rerender({ enabled: true });
    expect(selectSession).not.toHaveBeenCalled();
  });

  it("still auto-resumes after an ordinary root SSO callback is scrubbed", () => {
    const selectSession = vi.fn();
    seedSessions();
    useSessionStore.setState({ selectSession } as never);
    window.history.replaceState(null, "", "#/auth/callback?code=synthetic");
    const { rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useAutoResumeSession(enabled),
      { initialProps: { enabled: false } },
    );
    window.history.replaceState(null, "", "/");
    rerender({ enabled: true });
    expect(selectSession).toHaveBeenCalledWith("recent");
  });

  it("does not auto-resume before SSO restores a pending private session destination", () => {
    const selectSession = vi.fn();
    seedSessions();
    useSessionStore.setState({ selectSession } as never);
    window.history.replaceState(null, "", "#/auth/callback?code=synthetic");
    window.sessionStorage.setItem("elspeth_post_login_redirect", "#/11111111-1111-4111-8111-111111111111");
    const { rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useAutoResumeSession(enabled),
      { initialProps: { enabled: false } },
    );
    window.history.replaceState(null, "", "/");
    rerender({ enabled: true });
    expect(selectSession).not.toHaveBeenCalled();
  });

  it("remembers explicit navigation erased before any bootstrap dependency changes", () => {
    const selectSession = vi.fn();
    seedSessions();
    useSessionStore.setState({ selectSession, sessionsLoaded: false } as never);
    const { rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useAutoResumeSession(enabled),
      { initialProps: { enabled: false } },
    );
    act(() => {
      window.history.replaceState(null, "", "#/unavailable-session");
      window.dispatchEvent(new HashChangeEvent("hashchange"));
      window.history.replaceState(null, "", "/");
    });
    act(() => useSessionStore.setState({ sessionsLoaded: true }));
    rerender({ enabled: true });
    expect(selectSession).not.toHaveBeenCalled();
  });

  it("stands down when a session is already active", () => {
    const selectSession = vi.fn();
    seedSessions();
    useSessionStore.setState({
      selectSession,
      activeSessionId: "recent",
    } as never);

    renderHook(() => useAutoResumeSession(true));

    expect(selectSession).not.toHaveBeenCalled();
  });

  it("does nothing when there are no live sessions (empty landing instead)", () => {
    const selectSession = vi.fn();
    useSessionStore.setState({
      selectSession,
      sessionsLoaded: true,
      activeSessionId: null,
      sessions: [
        {
          id: "archived-only",
          title: "Archived",
          updated_at: "2026-07-01T00:00:00Z",
          archived: true,
        },
      ],
    } as never);

    renderHook(() => useAutoResumeSession(true));

    expect(selectSession).not.toHaveBeenCalled();
  });

  it("attempts at most once per mount (no re-resume after the user closes a session)", () => {
    const selectSession = vi.fn();
    seedSessions();
    useSessionStore.setState({ selectSession } as never);

    renderHook(() => useAutoResumeSession(true));
    expect(selectSession).toHaveBeenCalledTimes(1);

    // User later ends up with no active session again (e.g. archives it) —
    // the hook must not yank them into another session.
    useSessionStore.setState({ activeSessionId: "recent" } as never);
    useSessionStore.setState({ activeSessionId: null } as never);

    expect(selectSession).toHaveBeenCalledTimes(1);
  });
});
