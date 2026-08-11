import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  REQUEST_ARTIFACT_VIEW_EVENT,
  type RequestArtifactViewDetail,
} from "@/lib/composer-events";
import { resetStore } from "@/test/store-helpers";
import * as apiClient from "@/api/client";
import { useBlobStore } from "@/stores/blobStore";
import { useSessionStore } from "@/stores/sessionStore";
import { useHashRouter } from "./useHashRouter";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return {
    ...actual,
    uploadBlob: vi.fn(),
    fetchComposerProgress: vi.fn(),
    fetchMessages: vi.fn(),
  };
});

/** Minimal composition with content (one source) — export is meaningful. */
function nonEmptyCompositionState() {
  return {
    id: "state-1",
    version: 1,
    sources: { source: { plugin: "csv", options: {} } },
    nodes: [],
    edges: [],
    outputs: [],
    metadata: { name: null, description: null },
  };
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

describe("useHashRouter Phase 3B fragment migration", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
    resetStore(useBlobStore);
    vi.mocked(apiClient.uploadBlob).mockReset();
    window.history.replaceState(null, "", window.location.pathname);
    useSessionStore.setState({
      sessions: [{ id: "sess-1", title: "Session 1" } as never],
      activeSessionId: null,
      selectSession: vi.fn(),
    } as never);
  });

  it("rewrites #/{id}/spec to #/{id}", () => {
    window.history.replaceState(null, "", "#/sess-1/spec");

    renderHook(() => useHashRouter());

    expect(window.location.hash).toBe("#/sess-1");
  });

  it("rewrites #/{id}/runs to #/{id}", () => {
    window.history.replaceState(null, "", "#/sess-1/runs");

    renderHook(() => useHashRouter());

    expect(window.location.hash).toBe("#/sess-1");
  });

  it("requests Graph and rewrites #/{id}/graph", async () => {
    const requests: RequestArtifactViewDetail[] = [];
    const handler = (event: Event) => {
      requests.push((event as CustomEvent<RequestArtifactViewDetail>).detail);
    };
    window.addEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handler);
    window.history.replaceState(null, "", "#/sess-1/graph");

    renderHook(() => useHashRouter());
    await act(async () => {});

    expect(requests).toEqual([
      { tab: "graph", focusMode: false, sessionId: "sess-1" },
    ]);
    expect(window.location.hash).toBe("#/sess-1");
    window.removeEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handler);
  });

  it("requests YAML and rewrites #/{id}/yaml when the pipeline has content", async () => {
    const requests: RequestArtifactViewDetail[] = [];
    const handler = (event: Event) => {
      requests.push((event as CustomEvent<RequestArtifactViewDetail>).detail);
    };
    window.addEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handler);
    window.history.replaceState(null, "", "#/sess-1/yaml");
    // Session already active with a KNOWN, non-empty composition — the
    // yaml verb is content-gated (elspeth-bff8043d33 residual).
    useSessionStore.setState({
      activeSessionId: "sess-1",
      compositionStateLoaded: true,
      compositionState: nonEmptyCompositionState(),
    } as never);

    renderHook(() => useHashRouter());
    await act(async () => {});

    expect(requests).toEqual([
      { tab: "yaml", focusMode: false, sessionId: "sess-1" },
    ]);
    expect(window.location.hash).toBe("#/sess-1");
    window.removeEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handler);
  });

  it("does NOT request YAML for #/{id}/yaml on an empty pipeline", async () => {
    const handler = vi.fn();
    window.addEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handler);
    window.history.replaceState(null, "", "#/sess-1/yaml");
    useSessionStore.setState({
      activeSessionId: "sess-1",
      compositionStateLoaded: true,
      compositionState: null,
    } as never);

    renderHook(() => useHashRouter());
    await act(async () => {});

    expect(handler).not.toHaveBeenCalled();
    // The hash is still canonicalised — only artifact selection is withheld.
    expect(window.location.hash).toBe("#/sess-1");
    window.removeEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handler);
  });

  it("strips any unrecognized verb", () => {
    window.history.replaceState(null, "", "#/sess-1/nonsense");

    renderHook(() => useHashRouter());

    expect(window.location.hash).toBe("#/sess-1");
  });

  it("treats the Composer skip target as non-routing and preserves the active session", () => {
    useSessionStore.setState({
      activeSessionId: "sess-1",
    } as never);
    window.history.replaceState(null, "", "#/sess-1");
    renderHook(() => useHashRouter());

    window.history.replaceState(null, "", "#composer-main");
    act(() => window.dispatchEvent(new HashChangeEvent("hashchange")));

    expect(window.location.hash).toBe("#composer-main");
    expect(useSessionStore.getState().activeSessionId).toBe("sess-1");
  });

  it("defers cold-load graph actions until enabled", async () => {
    const handler = vi.fn();
    window.addEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handler);
    window.history.replaceState(null, "", "#/sess-1/graph");

    const { rerender } = renderHook(
      ({ enabled }) => useHashRouter({ enabled }),
      { initialProps: { enabled: false } },
    );
    await act(async () => {});

    expect(handler).not.toHaveBeenCalled();
    expect(window.location.hash).toBe("#/sess-1/graph");

    rerender({ enabled: true });
    await act(async () => {});

    expect(handler).toHaveBeenCalled();
    expect(window.location.hash).toBe("#/sess-1");
    window.removeEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handler);
  });
});

describe("useHashRouter persistent artifact intents", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
    window.history.replaceState(null, "", window.location.pathname);
    useSessionStore.setState({
      sessions: [
        { id: "sess-1", title: "Session 1" } as never,
        { id: "sess-2", title: "Session 2" } as never,
      ],
      activeSessionId: "sess-1",
      compositionStateLoaded: true,
      compositionState: nonEmptyCompositionState(),
      selectSession: vi.fn(),
    } as never);
  });

  it.each([
    ["graph", "graph"],
    ["spec", "spec"],
    ["yaml", "yaml"],
    ["runs", "run"],
  ] as const)("routes /%s to the matching artifact tab", async (verb, tab) => {
    const requests: RequestArtifactViewDetail[] = [];
    const handler = (event: Event) => {
      requests.push((event as CustomEvent<RequestArtifactViewDetail>).detail);
    };
    window.addEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handler);
    window.history.replaceState(null, "", `#/sess-1/${verb}`);

    renderHook(() => useHashRouter());
    await act(async () => {});

    expect(requests).toEqual([
      { tab, focusMode: false, sessionId: "sess-1" },
    ]);
    expect(window.location.hash).toBe("#/sess-1");
    window.removeEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handler);
  });

  it.each(["spec", "yaml"] as const)(
    "retains one /%s intent until the matching session content loads",
    async (tab) => {
      const requests: RequestArtifactViewDetail[] = [];
      const handler = (event: Event) => {
        requests.push((event as CustomEvent<RequestArtifactViewDetail>).detail);
      };
      window.addEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handler);
      useSessionStore.setState({
        compositionStateLoaded: false,
        compositionState: null,
      } as never);
      window.history.replaceState(null, "", `#/sess-1/${tab}`);

      renderHook(() => useHashRouter());
      await act(async () => {});
      expect(requests).toEqual([]);
      expect(window.location.hash).toBe("#/sess-1");

      await act(async () => {
        useSessionStore.setState({
          compositionStateLoaded: true,
          compositionState: nonEmptyCompositionState(),
        } as never);
      });
      expect(requests).toEqual([
        { tab, focusMode: false, sessionId: "sess-1" },
      ]);
      window.removeEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handler);
    },
  );

  it("fulfills a loaded-content intent after the store notification completes", async () => {
    const deliveryPhases: string[] = [];
    let phase = "idle";
    const handler = () => deliveryPhases.push(phase);
    window.addEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handler);
    useSessionStore.setState({
      compositionStateLoaded: false,
      compositionState: null,
    } as never);
    window.history.replaceState(null, "", "#/sess-1/yaml");
    renderHook(() => useHashRouter());

    phase = "notifying";
    act(() => {
      useSessionStore.setState({
        compositionStateLoaded: true,
        compositionState: nonEmptyCompositionState(),
      } as never);
    });
    phase = "committed";

    expect(deliveryPhases).toEqual([]);
    await act(async () => Promise.resolve());
    expect(deliveryPhases).toEqual(["committed"]);
    window.removeEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handler);
  });

  it("supersedes a pending Spec intent with a newer Graph intent", async () => {
    const requests: RequestArtifactViewDetail[] = [];
    const handler = (event: Event) => {
      requests.push((event as CustomEvent<RequestArtifactViewDetail>).detail);
    };
    window.addEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handler);
    useSessionStore.setState({
      compositionStateLoaded: false,
      compositionState: null,
    } as never);
    window.history.replaceState(null, "", "#/sess-1/spec");
    renderHook(() => useHashRouter());

    await act(async () => {
      window.history.replaceState(null, "", "#/sess-1/graph");
      window.dispatchEvent(new HashChangeEvent("hashchange"));
      await Promise.resolve();
    });
    await act(async () => {
      useSessionStore.setState({
        compositionStateLoaded: true,
        compositionState: nonEmptyCompositionState(),
      } as never);
    });

    expect(requests).toEqual([
      { tab: "graph", focusMode: false, sessionId: "sess-1" },
    ]);
    window.removeEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handler);
  });

  it("discards a pending intent when navigation selects another session", async () => {
    const requests: RequestArtifactViewDetail[] = [];
    const handler = (event: Event) => {
      requests.push((event as CustomEvent<RequestArtifactViewDetail>).detail);
    };
    window.addEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handler);
    useSessionStore.setState({
      compositionStateLoaded: false,
      compositionState: null,
      selectSession: vi.fn((sessionId: string) => {
        useSessionStore.setState({ activeSessionId: sessionId });
        return Promise.resolve();
      }),
    } as never);
    window.history.replaceState(null, "", "#/sess-1/spec");
    renderHook(() => useHashRouter());

    await act(async () => {
      window.history.replaceState(null, "", "#/sess-2/graph");
      window.dispatchEvent(new HashChangeEvent("hashchange"));
      await Promise.resolve();
    });
    await act(async () => {
      useSessionStore.setState({
        activeSessionId: "sess-1",
        compositionStateLoaded: true,
        compositionState: nonEmptyCompositionState(),
      } as never);
    });

    expect(requests).toEqual([
      { tab: "graph", focusMode: false, sessionId: "sess-2" },
    ]);
    window.removeEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handler);
  });
});

describe("useHashRouter — Batch 2 fixes", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
    vi.mocked(apiClient.fetchComposerProgress).mockReset();
    vi.mocked(apiClient.fetchMessages).mockReset();
    window.history.replaceState(null, "", window.location.pathname);
    useSessionStore.setState({
      sessions: [{ id: "sess-1", title: "Session 1" } as never],
      activeSessionId: null,
      selectSession: vi.fn(),
    } as never);
  });

  // ── Fix A: prototype-walk guard ─────────────────────────────────────────
  //
  // The old code used `verb in ACTION_VERBS` which walks the prototype chain.
  // For example, `"constructor" in ACTION_VERBS` returns true even though
  // "constructor" is not an own property of the ACTION_VERBS object —
  // `ACTION_VERBS["constructor"]` returns the Object constructor function, and
  // `new CustomEvent(fn)` coerces it to a string and dispatches a garbage event.
  //
  // The fix uses Object.hasOwn(ACTION_VERBS, verb) which only checks own
  // properties.  We use "constructor" as the test verb because it:
  //   1. Is all-lowercase (matches the regex [a-z]+)
  //   2. Is a prototype-inherited property of plain objects
  //   3. Was exploitable under the old `in` check

  it("prototype-walk guard: #/{id}/constructor does not dispatch any CustomEvent", async () => {
    window.history.replaceState(null, "", "#/sess-1/constructor");

    // Spy on dispatchEvent BEFORE rendering so we capture all calls
    const spy = vi.spyOn(window, "dispatchEvent");

    renderHook(() => useHashRouter());
    // Flush any queued microtasks
    await act(async () => {});

    // No CustomEvent should be dispatched; native events (e.g. popstate) are
    // not CustomEvent instances so we filter to CustomEvent only.
    const customEvents = spy.mock.calls
      .map(([e]) => e)
      .filter((e) => e instanceof CustomEvent);
    expect(customEvents).toHaveLength(0);

    spy.mockRestore();
  });

  it("prototype-walk guard: #/{id}/constructor rewrites to #/{id} (silent strip)", () => {
    window.history.replaceState(null, "", "#/sess-1/constructor");

    renderHook(() => useHashRouter());

    // Silently stripped — no event, no toast, just the canonical hash
    expect(window.location.hash).toBe("#/sess-1");
  });

  it.each(["runs", "spec"])(
    "does not show a retired-view toast for restored /%s navigation",
    (verb) => {
      window.history.replaceState(null, "", `#/sess-1/${verb}`);

      const { result } = renderHook(() => useHashRouter());

      expect(result.current.redirectToast).toBeNull();
    },
  );

  it("unrecognized non-retired verb does NOT show a toast", () => {
    // "nonsense" is not a retired verb — it is silently stripped with no toast
    window.history.replaceState(null, "", "#/sess-1/nonsense");

    const { result } = renderHook(() => useHashRouter());

    expect(result.current.redirectToast).toBeNull();
  });

  // ── Fix B: try/finally guard on applying.current ────────────────────────

  it("applying.current is cleared so URL-echo subscription works after selectSession throws", async () => {
    // The try/finally in applyHash guarantees applying.current = false even
    // when selectSession throws.  We verify this by:
    //   1. Mounting the hook with a normal selectSession.
    //   2. Triggering a hashchange to a new session where selectSession throws.
    //      JSDOM propagates the uncaught exception as a window "error" event;
    //      we intercept it to prevent Vitest from treating it as an unhandled error.
    //   3. After the throw, triggering the URL-echo subscription (useEffect #3)
    //      directly via a store mutation and verifying the URL updates.
    //      If applying.current had stayed true, the subscription would early-return
    //      and the URL would not change.

    useSessionStore.setState({
      sessions: [
        { id: "sess-1", title: "Session 1" } as never,
        { id: "sess-2", title: "Session 2" } as never,
      ],
      activeSessionId: "sess-1",
      selectSession: vi.fn(),
    } as never);

    window.history.replaceState(null, "", "#/sess-1");
    renderHook(() => useHashRouter());

    // Replace selectSession with a throwing variant
    useSessionStore.setState({
      selectSession: vi.fn(() => {
        throw new Error("selectSession boom");
      }) as never,
    } as never);

    // Intercept the uncaught exception that JSDOM propagates so Vitest
    // doesn't flag it as an unhandled error.  We assert it was thrown so
    // the test fails if the exception is unexpectedly swallowed.
    let caught: Event | null = null;
    function onError(e: Event) {
      e.preventDefault(); // Suppress "Uncaught Exception" in Vitest
      caught = e;
    }
    window.addEventListener("error", onError);

    await act(async () => {
      window.history.replaceState(null, "", "#/sess-2");
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });

    window.removeEventListener("error", onError);

    // Confirm the error was thrown (proving the throwing code path ran)
    expect(caught).not.toBeNull();

    // Now verify applying.current was reset by try/finally.
    // The URL-echo subscription (useEffect #3) fires whenever activeSessionId
    // changes — but only if applying.current is false.  Drive it directly.
    //
    // IMPORTANT: we use "sess-3" here, not "sess-2".  After the failed
    // hashchange, lastWrittenHash.current === "#/sess-2" (set by
    // handleHashChange before calling applyHash).  If we then set
    // activeSessionId="sess-2", the subscription early-returns because
    // hash === lastWrittenHash.current — that's a false pass whether or not
    // applying.current was fixed.  Using "sess-3" forces the subscription to
    // actually pushState, which only happens if applying.current is false.
    //
    // Under the bug (no try/finally):
    //   applying.current stays true → subscription early-returns → hash stays "#/sess-2"
    // Under the fix (try/finally):
    //   applying.current resets to false → subscription pushes "#/sess-3"
    await act(async () => {
      useSessionStore.setState({ selectSession: vi.fn() } as never);
      useSessionStore.setState({
        sessions: [
          { id: "sess-1", title: "Session 1" } as never,
          { id: "sess-2", title: "Session 2" } as never,
          { id: "sess-3", title: "Session 3" } as never,
        ],
        activeSessionId: "sess-3",
      } as never);
    });

    expect(window.location.hash).toBe("#/sess-3");
  });

  // ── popstate triggers reapply ────────────────────────────────────────────

  it("popstate requests Graph when hash is #/{id}/graph", async () => {
    window.history.replaceState(null, "", "#/sess-1");
    renderHook(() => useHashRouter());

    const handler = vi.fn();
    window.addEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handler);

    await act(async () => {
      // Simulate navigating back to a graph URL via popstate
      window.history.pushState(null, "", "#/sess-1/graph");
      window.dispatchEvent(new PopStateEvent("popstate"));
      // Flush queueMicrotask
      await Promise.resolve();
    });

    expect(handler).toHaveBeenCalled();
    expect(
      (handler.mock.calls[0]?.[0] as CustomEvent<RequestArtifactViewDetail>)
        .detail,
    ).toEqual({ tab: "graph", focusMode: false, sessionId: "sess-1" });
    window.removeEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handler);
  });

  it("popstate to an empty hash clears the active session", async () => {
    useSessionStore.setState({
      sessions: [{ id: "sess-1", title: "Session 1" } as never],
      activeSessionId: "sess-1",
      selectSession: vi.fn(),
    } as never);
    window.history.replaceState(null, "", "#/sess-1");
    renderHook(() => useHashRouter());

    await act(async () => {
      window.history.replaceState(null, "", window.location.pathname);
      window.dispatchEvent(new PopStateEvent("popstate"));
    });

    expect(window.location.hash).toBe("");
    expect(useSessionStore.getState().activeSessionId).toBeNull();
  });

  it("popstate to an empty hash stops both old-session pollers", async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(apiClient.fetchComposerProgress).mockResolvedValue({
        session_id: "sess-1",
        request_id: "request-1",
        phase: "using_tools",
        headline: "Working",
        evidence: [],
        likely_next: null,
        reason: null,
        updated_at: "2026-07-26T10:00:00Z",
      });
      vi.mocked(apiClient.fetchMessages).mockResolvedValue([]);
      useSessionStore.setState({
        sessions: [{ id: "sess-1", title: "Session 1" } as never],
        activeSessionId: "sess-1",
        selectSession: vi.fn(),
      } as never);
      useSessionStore.getState().startComposerProgressPolling("sess-1");
      useSessionStore.getState().startInflightMessagesPolling("sess-1");
      await vi.advanceTimersByTimeAsync(1500);
      expect(apiClient.fetchComposerProgress).toHaveBeenCalledTimes(2);
      expect(apiClient.fetchMessages).toHaveBeenCalledTimes(1);

      window.history.replaceState(null, "", "#/sess-1");
      renderHook(() => useHashRouter());
      await act(async () => {
        window.history.replaceState(null, "", window.location.pathname);
        window.dispatchEvent(new PopStateEvent("popstate"));
      });
      const progressCallsAtUnbind = vi.mocked(apiClient.fetchComposerProgress)
        .mock.calls.length;
      const messageCallsAtUnbind = vi.mocked(apiClient.fetchMessages).mock.calls
        .length;

      await vi.advanceTimersByTimeAsync(4500);
      expect(apiClient.fetchComposerProgress).toHaveBeenCalledTimes(
        progressCallsAtUnbind,
      );
      expect(apiClient.fetchMessages).toHaveBeenCalledTimes(messageCallsAtUnbind);
    } finally {
      resetStore(useSessionStore);
      vi.useRealTimers();
    }
  });

  it("missing-session hydration stops both old-session pollers", async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(apiClient.fetchComposerProgress).mockResolvedValue({
        session_id: "missing-session",
        request_id: "request-missing",
        phase: "using_tools",
        headline: "Working",
        evidence: [],
        likely_next: null,
        reason: null,
        updated_at: "2026-07-26T10:00:00Z",
      });
      vi.mocked(apiClient.fetchMessages).mockResolvedValue([]);
      useSessionStore.setState({
        sessions: [],
        activeSessionId: "missing-session",
        selectSession: vi.fn(),
      } as never);
      useSessionStore
        .getState()
        .startComposerProgressPolling("missing-session");
      useSessionStore.getState().startInflightMessagesPolling("missing-session");
      await vi.advanceTimersByTimeAsync(1500);
      expect(apiClient.fetchComposerProgress).toHaveBeenCalledTimes(2);
      expect(apiClient.fetchMessages).toHaveBeenCalledTimes(1);

      window.history.replaceState(null, "", "#/missing-session");
      renderHook(() => useHashRouter());
      await act(async () => {
        useSessionStore.setState({
          sessions: [{ id: "sess-1", title: "Session 1" } as never],
        });
      });
      expect(useSessionStore.getState().activeSessionId).toBeNull();
      const progressCallsAtUnbind = vi.mocked(apiClient.fetchComposerProgress)
        .mock.calls.length;
      const messageCallsAtUnbind = vi.mocked(apiClient.fetchMessages).mock.calls
        .length;

      await vi.advanceTimersByTimeAsync(4500);
      expect(apiClient.fetchComposerProgress).toHaveBeenCalledTimes(
        progressCallsAtUnbind,
      );
      expect(apiClient.fetchMessages).toHaveBeenCalledTimes(messageCallsAtUnbind);
    } finally {
      resetStore(useSessionStore);
      vi.useRealTimers();
    }
  });

  it("fences an A upload completion across router A-to-null-to-A navigation", async () => {
    const upload = deferred<Awaited<ReturnType<typeof apiClient.uploadBlob>>>();
    const uploaded = {
      id: "00000000-0000-4000-8000-000000000911",
      session_id: "sess-1",
      filename: "stale.csv",
      mime_type: "text/csv",
      size_bytes: 12,
      content_hash: "f".repeat(64),
      created_at: "2026-07-26T09:00:00Z",
      created_by: "user" as const,
      source_description: null,
      status: "ready" as const,
      creation_modality: "verbatim" as const,
      created_from_message_id: null,
      creating_model_identifier: null,
      creating_model_version: null,
      creating_provider: null,
      creating_composer_skill_hash: null,
      creating_arguments_hash: null,
    };
    vi.mocked(apiClient.uploadBlob).mockReturnValueOnce(upload.promise);
    useBlobStore.getState().activateSession("sess-1");
    useSessionStore.setState({
      sessions: [{ id: "sess-1", title: "Session 1" } as never],
      activeSessionId: "sess-1",
      selectSession: vi.fn((sessionId: string) => {
        useBlobStore.getState().activateSession(sessionId);
        useSessionStore.setState({ activeSessionId: sessionId });
        return Promise.resolve();
      }),
    } as never);
    window.history.replaceState(null, "", "#/sess-1");
    renderHook(() => useHashRouter());
    const pendingUpload = useBlobStore
      .getState()
      .uploadBlob("sess-1", new File(["stale"], "stale.csv"));

    await act(async () => {
      window.history.replaceState(null, "", window.location.pathname);
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await act(async () => {
      window.history.replaceState(null, "", "#/sess-1");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(useSessionStore.getState().activeSessionId).toBe("sess-1");

    await act(async () => {
      upload.resolve(uploaded);
      await pendingUpload;
    });

    expect(useBlobStore.getState().blobs).toEqual([]);
  });

  // ── Two rapid hashchanges ────────────────────────────────────────────────

  it("a newer loaded YAML hash globally supersedes a queued Graph hash", async () => {
    window.history.replaceState(null, "", "#/sess-1");
    // The yaml verb is content-gated: give the active session a KNOWN,
    // non-empty composition so its dispatch fires (elspeth-bff8043d33).
    useSessionStore.setState({
      activeSessionId: "sess-1",
      compositionStateLoaded: true,
      compositionState: nonEmptyCompositionState(),
    } as never);
    renderHook(() => useHashRouter());

    const requests: RequestArtifactViewDetail[] = [];
    const handler = (event: Event) => {
      requests.push((event as CustomEvent<RequestArtifactViewDetail>).detail);
    };
    window.addEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handler);

    await act(async () => {
      // Fire two hashchanges synchronously; both handlers are queued as microtasks
      window.history.replaceState(null, "", "#/sess-1/graph");
      window.dispatchEvent(new HashChangeEvent("hashchange"));
      window.history.replaceState(null, "", "#/sess-1/yaml");
      window.dispatchEvent(new HashChangeEvent("hashchange"));
      // Flush all queued microtasks
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(requests).toEqual([
      { tab: "yaml", focusMode: false, sessionId: "sess-1" },
    ]);

    window.removeEventListener(REQUEST_ARTIFACT_VIEW_EVENT, handler);
  });
});
