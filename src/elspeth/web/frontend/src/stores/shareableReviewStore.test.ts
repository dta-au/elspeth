import { describe, it, expect, vi, beforeEach } from "vitest";
import { useShareableReviewStore } from "./shareableReviewStore";
import * as api from "../api/shareableReviews";

const SESSION_A = "00000000-0000-0000-0000-00000000000a";
const SESSION_B = "00000000-0000-0000-0000-00000000000b";

const validResponseFor = (suffix: string) => ({
  token: `tk-${suffix}`,
  share_url: `/#/shared/tk-${suffix}`,
  expires_at: "2026-06-19T00:00:00+00:00",
  payload_digest: "sha256:" + "ab".repeat(32),
});

const FILES = {
  files: [{ filename: "a.csv", size_bytes: 3, used_by: ["source"] }],
  importable: true,
  not_importable_reason: null,
};
const OPTIONS = { allowImport: true, lifetimeSeconds: 86400 } as const;

/** Open the configure step and confirm it: the two-step flow every mint takes. */
async function openAndConfirm(sessionId: string): Promise<void> {
  await useShareableReviewStore.getState().openConfigure(sessionId);
  await useShareableReviewStore.getState().confirmMark(OPTIONS);
}

describe("shareableReviewStore", () => {
  beforeEach(() => {
    useShareableReviewStore.getState().reset();
    vi.restoreAllMocks();
    vi.spyOn(api, "fetchShareableFiles").mockResolvedValue(FILES);
  });

  it("confirmMark sets latestResponse + dialogOpen on success", async () => {
    vi.spyOn(api, "markReadyForReview").mockResolvedValueOnce(validResponseFor("1"));
    await openAndConfirm(SESSION_A);
    const state = useShareableReviewStore.getState();
    expect(state.dialogOpen).toBe(true);
    expect(state.inFlight).toBe(false);
    expect(state.error).toBeNull();
    expect(state.latestResponse?.token).toBe("tk-1");
    expect(state.sessionIdForResponse).toBe(SESSION_A);
  });

  it("confirmMark marks inFlight=true during the POST", async () => {
    let resolveFn: (v: ReturnType<typeof validResponseFor>) => void = () => {};
    const pending = new Promise<ReturnType<typeof validResponseFor>>((resolve) => {
      resolveFn = resolve;
    });
    vi.spyOn(api, "markReadyForReview").mockReturnValueOnce(pending);
    await useShareableReviewStore.getState().openConfigure(SESSION_A);
    const callPromise = useShareableReviewStore.getState().confirmMark(OPTIONS);
    expect(useShareableReviewStore.getState().inFlight).toBe(true);
    expect(useShareableReviewStore.getState().dialogOpen).toBe(true);
    resolveFn(validResponseFor("1"));
    await callPromise;
    expect(useShareableReviewStore.getState().inFlight).toBe(false);
  });

  it("confirmMark sets error and clears latestResponse on 409", async () => {
    vi.spyOn(api, "markReadyForReview").mockRejectedValueOnce({
      status: 409,
      detail: "composition validation failed; fix errors before sharing",
    });
    await openAndConfirm(SESSION_A);
    const state = useShareableReviewStore.getState();
    expect(state.dialogOpen).toBe(true); // dialog stays open to show the error
    expect(state.inFlight).toBe(false);
    expect(state.error).toContain("composition validation failed");
    expect(state.latestResponse).toBeNull();
  });

  it("confirmMark retains the failed session id for first-attempt retry", async () => {
    vi.spyOn(api, "markReadyForReview").mockRejectedValueOnce({
      status: 409,
      detail: "composition validation failed; fix errors before sharing",
    });

    await openAndConfirm(SESSION_A);

    const state = useShareableReviewStore.getState();
    expect(state.request).toMatchObject({ kind: "error", sessionId: SESSION_A });
    expect(state.latestResponse).toBeNull();
    expect(state.sessionIdForResponse).toBe(SESSION_A);
  });

  it("confirmMark falls back to a generic 401 message when detail absent", async () => {
    vi.spyOn(api, "markReadyForReview").mockRejectedValueOnce({ status: 401 });
    await openAndConfirm(SESSION_A);
    const state = useShareableReviewStore.getState();
    expect(state.error).toBe("Authentication required.");
  });

  it("confirmMark clears stale response when switching sessions", async () => {
    vi.spyOn(api, "markReadyForReview").mockResolvedValueOnce(validResponseFor("A"));
    await openAndConfirm(SESSION_A);
    expect(useShareableReviewStore.getState().sessionIdForResponse).toBe(SESSION_A);
    // Now switch to a different session. Latest response from A must NOT leak.
    vi.spyOn(api, "markReadyForReview").mockResolvedValueOnce(validResponseFor("B"));
    await openAndConfirm(SESSION_B);
    const state = useShareableReviewStore.getState();
    expect(state.sessionIdForResponse).toBe(SESSION_B);
    expect(state.latestResponse?.token).toBe("tk-B");
  });

  it("close() keeps latestResponse so reopening shows the same URL", async () => {
    vi.spyOn(api, "markReadyForReview").mockResolvedValueOnce(validResponseFor("1"));
    await openAndConfirm(SESSION_A);
    useShareableReviewStore.getState().close();
    const state = useShareableReviewStore.getState();
    expect(state.dialogOpen).toBe(false);
    expect(state.latestResponse?.token).toBe("tk-1"); // preserved
  });

  it("reset() clears everything", async () => {
    vi.spyOn(api, "markReadyForReview").mockResolvedValueOnce(validResponseFor("1"));
    await openAndConfirm(SESSION_A);
    useShareableReviewStore.getState().reset();
    const state = useShareableReviewStore.getState();
    expect(state.dialogOpen).toBe(false);
    expect(state.latestResponse).toBeNull();
    expect(state.sessionIdForResponse).toBeNull();
    expect(state.error).toBeNull();
  });

  it("test_stale_response_from_prior_session_does_not_clobber_current", async () => {
    // DC-7 gap 1: stale-response race window.
    // confirmMark(A) is awaiting POST; user switches sessions and confirms
    // B before A resolves. A's resolve must NOT clobber B.
    let resolveA: (v: ReturnType<typeof validResponseFor>) => void = () => {};
    const pendingA = new Promise<ReturnType<typeof validResponseFor>>((resolve) => {
      resolveA = resolve;
    });
    const responseB = validResponseFor("B");
    const spy = vi.spyOn(api, "markReadyForReview");
    spy.mockReturnValueOnce(pendingA);
    spy.mockResolvedValueOnce(responseB);

    // Kick off A but do not await — POST is in-flight.
    await useShareableReviewStore.getState().openConfigure(SESSION_A);
    const promiseA = useShareableReviewStore.getState().confirmMark(OPTIONS);
    // Session switches; B's flow completes first.
    await openAndConfirm(SESSION_B);
    expect(useShareableReviewStore.getState().sessionIdForResponse).toBe(SESSION_B);
    expect(useShareableReviewStore.getState().latestResponse?.token).toBe("tk-B");

    // Now A finally resolves — its response must be dropped.
    resolveA(validResponseFor("A"));
    await promiseA;

    const state = useShareableReviewStore.getState();
    expect(state.sessionIdForResponse).toBe(SESSION_B);
    expect(state.latestResponse?.token).toBe("tk-B");
    expect(state.latestResponse?.token).not.toBe("tk-A");
  });

  it("test_concurrent_confirmMark_does_not_kick_off_second_post", async () => {
    // DC-7 gap 2: double-click race. Programmatic re-entry while inFlight
    // must not mint a second token (audit rows are append-only).
    const neverResolves = new Promise<ReturnType<typeof validResponseFor>>(() => {});
    const spy = vi.spyOn(api, "markReadyForReview").mockReturnValue(neverResolves);

    // Fire twice synchronously; the second call must be a no-op.
    await useShareableReviewStore.getState().openConfigure(SESSION_A);
    void useShareableReviewStore.getState().confirmMark(OPTIONS);
    void useShareableReviewStore.getState().confirmMark(OPTIONS);

    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("test_clearForSession_wipes_dialog_state_and_response", async () => {
    // DC-7 gap 3: clearForSession must wipe dialogOpen, latestResponse,
    // error, and inFlight when the sessionId matches.
    vi.spyOn(api, "markReadyForReview").mockResolvedValueOnce(validResponseFor("A"));
    await openAndConfirm(SESSION_A);
    // Confirm precondition: state populated for session A.
    expect(useShareableReviewStore.getState().latestResponse?.token).toBe("tk-A");

    useShareableReviewStore.getState().clearForSession(SESSION_A);

    const state = useShareableReviewStore.getState();
    expect(state.dialogOpen).toBe(false);
    expect(state.latestResponse).toBeNull();
    expect(state.error).toBeNull();
    expect(state.inFlight).toBe(false);
    expect(state.sessionIdForResponse).toBeNull();
  });

  it("test_clearForSession_other_session_no_op", async () => {
    // DC-7 gap 3 (negative case): clearForSession with a non-matching
    // sessionId must NOT touch the store — the cleared id refers to a
    // session whose state we don't own.
    vi.spyOn(api, "markReadyForReview").mockResolvedValueOnce(validResponseFor("A"));
    await openAndConfirm(SESSION_A);
    const before = useShareableReviewStore.getState();
    const snapshot = {
      dialogOpen: before.dialogOpen,
      latestResponse: before.latestResponse,
      error: before.error,
      inFlight: before.inFlight,
      sessionIdForResponse: before.sessionIdForResponse,
    };

    useShareableReviewStore.getState().clearForSession(SESSION_B);

    const state = useShareableReviewStore.getState();
    expect(state.dialogOpen).toBe(snapshot.dialogOpen);
    expect(state.latestResponse).toEqual(snapshot.latestResponse);
    expect(state.error).toBe(snapshot.error);
    expect(state.inFlight).toBe(snapshot.inFlight);
    expect(state.sessionIdForResponse).toBe(snapshot.sessionIdForResponse);
  });

  // ── configure phase ─────────────────────────────────────────────────

  it("openConfigure loads files and opens the dialog without minting", async () => {
    const mint = vi.spyOn(api, "markReadyForReview");
    vi.spyOn(api, "fetchShareableFiles").mockResolvedValueOnce(FILES);
    await useShareableReviewStore.getState().openConfigure(SESSION_A);
    const state = useShareableReviewStore.getState();
    expect(mint).not.toHaveBeenCalled();
    expect(state.dialogOpen).toBe(true);
    expect(state.request).toMatchObject({ kind: "configure", sessionId: SESSION_A, files: FILES });
    expect(state.inFlight).toBe(false);
  });

  it("openConfigure records a files-load failure in the configure phase", async () => {
    vi.spyOn(api, "fetchShareableFiles").mockRejectedValueOnce({ status: 404 });
    await useShareableReviewStore.getState().openConfigure(SESSION_A);
    expect(useShareableReviewStore.getState().request).toMatchObject({
      kind: "configure",
      files: null,
      filesError: "Session not found.",
    });
  });

  it("confirmMark mints with the options from the configure phase", async () => {
    vi.spyOn(api, "fetchShareableFiles").mockResolvedValueOnce(FILES);
    const mint = vi.spyOn(api, "markReadyForReview").mockResolvedValueOnce(validResponseFor("1"));
    await useShareableReviewStore.getState().openConfigure(SESSION_A);
    await useShareableReviewStore.getState().confirmMark(OPTIONS);
    expect(mint).toHaveBeenCalledWith(SESSION_A, OPTIONS);
    const state = useShareableReviewStore.getState();
    expect(state.request.kind).toBe("resolved");
    expect(state.latestResponse?.token).toBe("tk-1");
  });

  it("confirmMark surfaces a 409 file-naming detail as the error", async () => {
    vi.spyOn(api, "fetchShareableFiles").mockResolvedValueOnce(FILES);
    vi.spyOn(api, "markReadyForReview").mockRejectedValueOnce({ status: 409, detail: "a.csv cannot be shared" });
    await useShareableReviewStore.getState().openConfigure(SESSION_A);
    await useShareableReviewStore.getState().confirmMark(OPTIONS);
    expect(useShareableReviewStore.getState().error).toBe("a.csv cannot be shared");
  });

  it("confirmMark is a no-op outside the configure phase", async () => {
    const mint = vi.spyOn(api, "markReadyForReview");
    await useShareableReviewStore.getState().confirmMark(OPTIONS);
    expect(mint).not.toHaveBeenCalled();
    expect(useShareableReviewStore.getState().request.kind).toBe("idle");
  });

  it("a late files response for a superseded configure is dropped", async () => {
    let resolveA: (v: typeof FILES) => void = () => {};
    vi.spyOn(api, "fetchShareableFiles")
      .mockReturnValueOnce(new Promise((resolve) => { resolveA = resolve; }))
      .mockResolvedValueOnce({ ...FILES, importable: false });
    const first = useShareableReviewStore.getState().openConfigure(SESSION_A);
    await useShareableReviewStore.getState().openConfigure(SESSION_B);
    resolveA(FILES);
    await first;
    expect(useShareableReviewStore.getState().request).toMatchObject({
      kind: "configure",
      sessionId: SESSION_B,
    });
  });
  it("openConfigure reopens a closed dialog while a mint for the same session is pending", async () => {
    const neverResolves = new Promise<ReturnType<typeof validResponseFor>>(() => {});
    vi.spyOn(api, "markReadyForReview").mockReturnValue(neverResolves);
    await useShareableReviewStore.getState().openConfigure(SESSION_A);
    void useShareableReviewStore.getState().confirmMark(OPTIONS);
    useShareableReviewStore.getState().close();
    expect(useShareableReviewStore.getState().dialogOpen).toBe(false);

    await useShareableReviewStore.getState().openConfigure(SESSION_A);

    const state = useShareableReviewStore.getState();
    expect(state.dialogOpen).toBe(true);
    expect(state.inFlight).toBe(true);
  });

  it("confirmMark from the error phase retries the mint with the same options", async () => {
    const mint = vi
      .spyOn(api, "markReadyForReview")
      .mockRejectedValueOnce({ status: 409, detail: "not ready" })
      .mockResolvedValueOnce(validResponseFor("2"));
    await openAndConfirm(SESSION_A);
    const failed = useShareableReviewStore.getState().request;
    expect(failed).toMatchObject({ kind: "error", sessionId: SESSION_A, options: OPTIONS });
    if (failed.kind !== "error") throw new Error("unreachable");

    await useShareableReviewStore.getState().confirmMark(failed.options);

    expect(mint).toHaveBeenCalledTimes(2);
    expect(mint).toHaveBeenLastCalledWith(SESSION_A, OPTIONS);
    expect(useShareableReviewStore.getState().latestResponse?.token).toBe("tk-2");
  });
});
