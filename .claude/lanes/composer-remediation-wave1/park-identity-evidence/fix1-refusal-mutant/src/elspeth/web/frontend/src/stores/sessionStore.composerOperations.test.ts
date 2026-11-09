import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSessionStore } from "./sessionStore";
import { useAuthStore } from "./authStore";
import { COMPOSER_STOP_KEY, COMPOSER_CUSTODY_KEY, COMPOSER_CUSTODY_LIFETIME_MS, attachComposerObserver, settleComposerCustody, acquireComposerOperationCustody, authenticateComposerCustody, findComposerOperationCustody, purgeComposerCustody } from "./composerOperationCustody";
import { detachComposerObservers } from "@/api/composerOperationObserver";
import { advanceAuthGeneration } from "@/api/authSession";
import { compositionStateAuthorityFields } from "@/test/composerFixtures";
import type { ComposerOperationSnapshot, ComposerOperationFailureOutcome, ComposerOperationFailurePage } from "@/types/composerOperations";
import type { ChatMessage, CompositionState, CompositionProposal } from "@/types/index";
vi.mock("@/api/client", () => ({ submitComposerOperation: vi.fn(), fetchComposerOperation: vi.fn(), fetchComposerOperationFailures: vi.fn(), cancelComposerOperation: vi.fn(), fetchComposerOperationStream: vi.fn(), apiErrorFromBody: vi.fn(), composerActiveOperation: vi.fn(), isDefinitiveComposerRefusal: vi.fn(), fetchMessages: vi.fn(), fetchCompositionState: vi.fn(), fetchCompositionProposals: vi.fn(), fetchComposerPreferences: vi.fn(), revertToVersion: vi.fn(), fetchStateVersions: vi.fn(), fetchSessions: vi.fn(), fetchCurrentUser: vi.fn(), fetchAuthConfig: vi.fn(), listBlobs: vi.fn(), listInterpretationEvents: vi.fn() }));
vi.mock("./executionStore", () => ({ useExecutionStore: { getState: () => ({ clearValidation: vi.fn() }) } }));
import * as api from "@/api/client";
const sid = "11111111-1111-4111-8111-111111111111", userId = "22222222-2222-4222-8222-222222222222", assistantId = "33333333-3333-4333-8333-333333333333", stateId = "44444444-4444-4444-8444-444444444444";
const scope = { principalId: "principal", authProvider: "local" };
const assistant: ChatMessage = { id: assistantId, session_id: sid, role: "assistant", content: "done", tool_calls: null, created_at: new Date().toISOString() };
function composition(version = 1): CompositionState { return { ...compositionStateAuthorityFields, id: stateId, session_id: sid, version, sources: {}, nodes: [], edges: [], outputs: [], metadata: { name: null, description: null } }; }
function snapshot(operationId: string, kind: "compose_message" | "compose_recompose" = "compose_message"): ComposerOperationSnapshot { return { operation_id: operationId, kind, status: "completed", cancel_requested: false, poll_after_ms: 1000, deadline_at: new Date().toISOString(), deadline_remaining_ms: 0, result: { message: assistant, state: composition(), proposals: [] }, error: null }; }
beforeEach(() => {
  vi.resetAllMocks(); useSessionStore.getState().reset(); purgeComposerCustody(); sessionStorage.clear(); authenticateComposerCustody(scope);
  useSessionStore.setState({ activeSessionId: sid, compositionStateLoaded: true, compositionState: composition() });
  vi.mocked(api.submitComposerOperation).mockImplementation(async (descriptor) => ({ operation_id: descriptor.operationId, kind: descriptor.kind, status: "queued", poll_after_ms: 1000 }));
  vi.mocked(api.fetchComposerOperation).mockImplementation(async (_session, operationId) => snapshot(operationId));
  vi.mocked(api.fetchComposerOperationStream).mockRejectedValue(new Error("stream unavailable"));
  vi.mocked(api.fetchComposerOperationFailures).mockImplementation(async (sessionId) => ({ session_id: sessionId, items: [], next_cursor: null }));
  vi.mocked(api.composerActiveOperation).mockReturnValue(null); vi.mocked(api.isDefinitiveComposerRefusal).mockReturnValue(false);
  vi.mocked(api.fetchMessages).mockResolvedValue([]); vi.mocked(api.fetchCompositionState).mockResolvedValue(composition()); vi.mocked(api.fetchCompositionProposals).mockResolvedValue([]); vi.mocked(api.fetchSessions).mockResolvedValue([]); vi.mocked(api.listBlobs).mockResolvedValue([]); vi.mocked(api.listInterpretationEvents).mockResolvedValue([]);
  vi.mocked(api.fetchComposerPreferences).mockImplementation(async (sessionId) => ({ session_id: sessionId, trust_mode: "explicit_approve", density_default: "medium", interpretation_review_disabled: false, updated_at: "2026-10-06T00:00:00Z" }));
  vi.mocked(api.fetchCurrentUser).mockResolvedValue({ user_id: scope.principalId, username: "principal", display_name: null, email: null, groups: [], dev_admin: false }); vi.mocked(api.fetchAuthConfig).mockResolvedValue({ provider: "local", registration_mode: "closed", sso_start_url: null });
});
afterEach(() => { detachComposerObservers(); vi.useRealTimers(); purgeComposerCustody(); });

describe("authenticated retained operation outcomes", () => {
  const outcome: ComposerOperationFailureOutcome = { session_id: sid, operation_id: userId, kind: "compose_message", user_message_id: userId, settled_at: "2026-10-14T08:00:00Z", failure_code: "worker_lost", error_type: "composer_operation_worker_lost", diagnostic_id: null, request_id: "request-reference", saved_revisions: { attribution: "fenced", count: 2, latest_state_id: stateId, latest_version: 7 } };

  it("restores history with no browser custody, merges older pages, and retains a failure after a successful turn", async () => {
    vi.mocked(api.fetchComposerOperationFailures)
      .mockResolvedValueOnce({ session_id: sid, items: [outcome], next_cursor: "opaque-page" })
      .mockResolvedValueOnce({ session_id: sid, items: [outcome, { ...outcome, operation_id: stateId, user_message_id: null, settled_at: "2026-10-13T08:00:00Z" }], next_cursor: null });
    await useSessionStore.getState().resumeComposerOperation(sid);
    await vi.waitFor(() => expect(useSessionStore.getState().composerFailureOutcomes).toEqual([outcome]));
    expect(api.submitComposerOperation).not.toHaveBeenCalled();
    await useSessionStore.getState().loadComposerFailureOutcomes(true);
    expect(api.fetchComposerOperationFailures).toHaveBeenLastCalledWith(sid, { cursor: "opaque-page", signal: expect.any(AbortSignal) });
    expect(useSessionStore.getState().composerFailureOutcomes.map((row) => row.operation_id)).toEqual([userId, stateId]);
    await useSessionStore.getState().sendMessage("A later request");
    expect(useSessionStore.getState().composerFailureOutcomes.map((row) => row.operation_id)).toEqual([userId, stateId]);
    expect(useSessionStore.getState().messages.every((row) => row.role !== "system")).toBe(true);
  });

  it("reloads outcomes through actual same-principal login and clears them on a different principal", async () => {
    vi.mocked(api.fetchComposerOperationFailures).mockResolvedValue({ session_id: sid, items: [outcome], next_cursor: null });
    await useAuthStore.getState().loginWithToken("synthetic-same-principal-token");
    await vi.waitFor(() => expect(useSessionStore.getState().composerFailureOutcomes).toEqual([outcome]));
    vi.mocked(api.fetchCurrentUser).mockResolvedValue({ user_id: "other-principal", username: "other", display_name: null, email: null, groups: [], dev_admin: false });
    await useAuthStore.getState().loginWithToken("synthetic-other-principal-token");
    expect(useSessionStore.getState().activeSessionId).toBeNull();
    expect(useSessionStore.getState().composerFailureOutcomes).toEqual([]);
  });

  it("enriches the known terminal by exact identity without duplicating the notice", async () => {
    let resolve!: (page: ComposerOperationFailurePage) => void;
    vi.mocked(api.apiErrorFromBody).mockReturnValue({ status: 503, detail: "Server stopped", request_id: "request-reference" });
    vi.mocked(api.fetchComposerOperation).mockImplementation(async (_sid, operationId) => ({ ...snapshot(operationId), status: "failed", result: null, error: { http_status: 503, failure_code: "worker_lost", error_type: "composer_operation_worker_lost", diagnostic_id: null, body: {} } }));
    vi.mocked(api.fetchComposerOperationFailures).mockImplementationOnce(async () => await new Promise((done) => { resolve = done; }));
    const sending = useSessionStore.getState().sendMessage("Synthetic request");
    await vi.waitFor(() => expect(useSessionStore.getState().composerFailureOutcomes).toHaveLength(1));
    const immediate = useSessionStore.getState().composerFailureOutcomes[0];
    expect(immediate.saved_revisions.attribution).toBe("unavailable");
    expect(api.fetchComposerOperationFailures).toHaveBeenCalledWith(sid, { operationId: immediate.operation_id, signal: expect.any(AbortSignal) });
    const retained = { ...outcome, operation_id: immediate.operation_id };
    resolve({ session_id: sid, items: [retained], next_cursor: null });
    await sending;
    expect(useSessionStore.getState().composerFailureOutcomes).toEqual([retained]);
  });

  it.each(["navigation", "auth"])("rejects a late history response after %s generation changes", async (change) => {
    let resolve!: (page: ComposerOperationFailurePage) => void;
    let signal!: AbortSignal;
    vi.mocked(api.fetchComposerOperationFailures).mockImplementationOnce(async (_sid, options) => {
      signal = options!.signal!;
      return await new Promise((done) => { resolve = done; });
    });
    const loading = useSessionStore.getState().loadComposerFailureOutcomes();
    if (change === "navigation") {
      await useSessionStore.getState().selectSession(stateId);
      await useSessionStore.getState().selectSession(sid); // Same-session ABA must still reject the old page.
      expect(signal.aborted).toBe(true);
    } else advanceAuthGeneration();
    resolve({ session_id: sid, items: [outcome], next_cursor: null });
    await loading;
    expect(useSessionStore.getState().composerFailureOutcomes).toEqual([]);
  });

  it("bounds an uncooperative history read and preserves already known outcomes", async () => {
    vi.useFakeTimers();
    useSessionStore.setState({ composerFailureOutcomes: [outcome] });
    let signal!: AbortSignal;
    vi.mocked(api.fetchComposerOperationFailures).mockImplementationOnce(async (_sid, options) => {
      signal = options!.signal!;
      return await new Promise(() => {});
    });
    const loading = useSessionStore.getState().loadComposerFailureOutcomes();
    await vi.advanceTimersByTimeAsync(15000);
    await loading;
    expect(signal.aborted).toBe(true);
    expect(useSessionStore.getState().composerFailureHistoryLoading).toBe(false);
    expect(useSessionStore.getState().composerFailureHistoryError).toContain("Could not load");
    expect(useSessionStore.getState().composerFailureOutcomes).toEqual([outcome]);
  });
});

describe("terminal publication before optional reads", () => {
  it.each(["failed", "completed"] as const)("rejects a stale attached winner's %s terminal after navigation", async (status) => {
    const winnerId = "66666666-6666-4666-8666-666666666666";
    const refusal = { status: 409, error_type: "composer_operation_active", operation_id: winnerId, kind: "compose_message" };
    vi.mocked(api.submitComposerOperation).mockRejectedValueOnce(refusal);
    vi.mocked(api.composerActiveOperation).mockImplementation((error) => error === refusal ? { operation_id: winnerId, kind: "compose_message" } : null);
    let resolve!: (snapshot: ComposerOperationSnapshot) => void;
    vi.mocked(api.fetchComposerOperation).mockImplementationOnce(async () => await new Promise((done) => { resolve = done; }));
    const sending = useSessionStore.getState().sendMessage("my unsent draft");
    await vi.waitFor(() => expect(api.fetchComposerOperation).toHaveBeenCalledWith(sid, winnerId));
    await useSessionStore.getState().selectSession(stateId);
    const winner = snapshot(winnerId);
    resolve(status === "completed" ? winner : { ...winner, status: "failed", result: null, error: { http_status: 503, failure_code: "worker_lost", error_type: "composer_operation_worker_lost", diagnostic_id: null, body: {} } });
    await sending;
    expect(useSessionStore.getState().activeSessionId).toBe(stateId);
    expect(useSessionStore.getState().messages).toEqual([]);
    expect(useSessionStore.getState().composerFailureOutcomes).toEqual([]);
    expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
  });
  it.each(["failed", "completed"] as const)("publishes the attached winner's %s terminal while preserving the unsent loser", async (status) => {
    const winnerId = "66666666-6666-4666-8666-666666666666";
    const refusal = { status: 409, error_type: "composer_operation_active", operation_id: winnerId, kind: "compose_message" };
    vi.mocked(api.submitComposerOperation).mockRejectedValueOnce(refusal);
    vi.mocked(api.composerActiveOperation).mockImplementation((error) => error === refusal ? { operation_id: winnerId, kind: "compose_message" } : null);
    vi.mocked(api.apiErrorFromBody).mockReturnValue({ status: 503, detail: "Server stopped" });
    const completed = snapshot(winnerId);
    vi.mocked(api.fetchComposerOperation).mockResolvedValue(status === "completed" ? completed : { ...completed, status: "failed", result: null, error: { http_status: 503, failure_code: "worker_lost", error_type: "composer_operation_worker_lost", diagnostic_id: null, body: {} } });
    const release: (() => void)[] = [];
    vi.mocked(api.fetchMessages).mockImplementationOnce(async () => await new Promise((resolve) => { release.push(() => resolve([])); }));
    vi.mocked(api.fetchCompositionState).mockImplementationOnce(async () => await new Promise((resolve) => { release.push(() => resolve(composition())); }));
    vi.mocked(api.fetchCompositionProposals).mockImplementationOnce(async () => await new Promise((resolve) => { release.push(() => resolve([])); }));
    vi.mocked(api.fetchComposerOperationFailures).mockImplementationOnce(async () => await new Promise((resolve) => { release.push(() => resolve({ session_id: sid, items: [], next_cursor: null })); }));
    const sending = useSessionStore.getState().sendMessage("my unsent draft");
    try {
      await vi.waitFor(() => expect(useSessionStore.getState().isComposing).toBe(false));
      const state = useSessionStore.getState();
      const loser = state.messages.find((row) => row.content === "my unsent draft")!;
      expect(loser.operation_id).not.toBe(winnerId);
      expect(loser.local_status).toBe("failed");
      expect(state.error).toContain("unsent content remains a draft");
      expect(state.error).not.toContain("is active");
      if (status === "failed") {
        expect(state.composerFailureOutcomes).toEqual([expect.objectContaining({ operation_id: winnerId, failure_code: "worker_lost" })]);
        expect(api.fetchComposerOperationFailures).toHaveBeenCalledWith(sid, { operationId: winnerId, signal: expect.any(AbortSignal) });
      } else expect(state.messages).toContainEqual(completed.result!.message);
      expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
      expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
    } finally { for (const finish of release) finish(); await sending; }
  });

  it.each([false, true])("keeps fresh same-ID tool projections; genuine intervening update=%s", async (intervening) => {
    const tool = { id: userId, type: "function", function: { name: "set_metadata", arguments: "{}" } };
    const old = { ...assistant, content: "older assistant", tool_calls: [tool] };
    useSessionStore.setState({ messages: [old] });
    const terminal = { ...assistant, id: stateId, content: "new terminal" };
    vi.mocked(api.fetchComposerOperation).mockImplementation(async (_sid, operationId) => ({ ...snapshot(operationId), result: { message: terminal, state: composition(), proposals: [] } }));
    let resolve!: (messages: ChatMessage[]) => void;
    vi.mocked(api.fetchMessages).mockImplementationOnce(async () => await new Promise((done) => { resolve = done; }));
    const sending = useSessionStore.getState().sendMessage("Synthetic request");
    await vi.waitFor(() => expect(useSessionStore.getState().messages.some((row) => row.id === terminal.id)).toBe(true));
    const fresh = { ...old, tool_calls: [{ ...tool, outcome: "applied" as const, applied_state_version: 7 }] };
    const newer = { ...old, tool_calls: [{ ...tool, outcome: "rejected" as const, rejection: { error_code: "required_choice", guidance: ["Synthetic public guidance"] } }] };
    if (intervening) useSessionStore.setState((state) => ({ messages: state.messages.map((row) => row.id === old.id ? newer : row) }));
    resolve([fresh, terminal]);
    await sending;
    expect(useSessionStore.getState().messages.find((row) => row.id === old.id)).toEqual(intervening ? newer : fresh);
  });

  it("keeps fresh projections of the terminal reply and recovery notice", async () => {
    const recovery: ChatMessage = { ...assistant, id: userId, operation_id: userId, role: "system", content: "Restored saved draft" };
    vi.mocked(api.fetchComposerOperation).mockImplementation(async (_sid, operationId) => ({ ...snapshot(operationId), result: { message: assistant, state: composition(), proposals: [], recovery_notice: recovery } }));
    const projectedReply = { ...assistant, composition_state_id: stateId };
    const projectedRecovery = { ...recovery, composition_state_id: stateId };
    vi.mocked(api.fetchMessages).mockResolvedValueOnce([projectedReply, projectedRecovery]);
    await useSessionStore.getState().sendMessage("Synthetic request");
    expect(useSessionStore.getState().messages.find((row) => row.id === assistant.id)).toEqual(projectedReply);
    expect(useSessionStore.getState().messages.find((row) => row.id === recovery.id)).toEqual(projectedRecovery);
  });
  it("bounds all optional reads while preserving the completed reply", async () => {
    vi.useFakeTimers();
    const signals: AbortSignal[] = [];
    vi.mocked(api.fetchMessages).mockImplementationOnce(async (_sid, signal) => { signals.push(signal!); return await new Promise(() => {}); });
    vi.mocked(api.fetchCompositionState).mockImplementationOnce(async (_sid, signal) => { signals.push(signal!); return await new Promise(() => {}); });
    vi.mocked(api.fetchCompositionProposals).mockImplementationOnce(async (_sid, signal) => { signals.push(signal!); return await new Promise(() => {}); });
    const sending = useSessionStore.getState().sendMessage("Synthetic request");
    await vi.waitFor(() => expect(useSessionStore.getState().isComposing).toBe(false));
    await vi.advanceTimersByTimeAsync(15000);
    await sending;
    expect(signals).toHaveLength(3);
    expect(signals.every((signal) => signal.aborted)).toBe(true);
    expect(useSessionStore.getState().messages.some((row) => row.id === assistantId)).toBe(true);
  });
  it.each(["messages", "graph", "proposals"])("publishes known failure while %s refresh is stalled", async (phase) => {
    let release!: () => void;
    if (phase === "messages") vi.mocked(api.fetchMessages).mockReturnValueOnce(new Promise((resolve) => { release = () => resolve([]); }));
    if (phase === "graph") vi.mocked(api.fetchCompositionState).mockReturnValueOnce(new Promise((resolve) => { release = () => resolve(composition()); }));
    if (phase === "proposals") vi.mocked(api.fetchCompositionProposals).mockReturnValueOnce(new Promise((resolve) => { release = () => resolve([]); }));
    vi.mocked(api.apiErrorFromBody).mockReturnValue({ status: 500, detail: "Composer action failed.", request_id: "request-reference" });
    vi.mocked(api.fetchComposerOperation).mockImplementation(async (_session, operationId) => ({ ...snapshot(operationId), status: "failed", result: null, error: { http_status: 500, failure_code: "operation_failed", error_type: "operation_failed", diagnostic_id: assistantId, body: { detail: "Composer action failed.", request_id: "request-reference" } } }));
    const sending = useSessionStore.getState().sendMessage("Synthetic request");
    try {
      await vi.waitFor(() => expect(useSessionStore.getState().isComposing).toBe(false));
      const state = useSessionStore.getState();
      expect(state.error).toMatch(/failed/i);
      expect(state.composerFailureOutcomes).toHaveLength(1);
      expect(state.composerFailureOutcomes[0].diagnostic_id).toBe(assistantId);
      expect(state.composerFailureOutcomes[0].saved_revisions.count).toBeNull();
      expect(state.composerFailureOutcomes[0].saved_revisions.attribution).toBe("unavailable");
    } finally { release(); await sending; }
  });

  it("publishes the completed reply before a stalled proposal refresh", async () => {
    let release!: () => void;
    vi.mocked(api.fetchCompositionProposals).mockReturnValueOnce(new Promise((resolve) => { release = () => resolve([]); }));
    const sending = useSessionStore.getState().sendMessage("Synthetic request");
    try {
      await vi.waitFor(() => expect(useSessionStore.getState().messages.some((row) => row.id === assistantId)).toBe(true));
      expect(useSessionStore.getState().isComposing).toBe(false);
      expect(useSessionStore.getState().compositionState?.version).toBe(1);
    } finally { release(); await sending; }
  });
});

describe("unavailable graph recovery admission", () => {
  const unavailable = { status: 409, error_type: "composition_state_unavailable", detail: "Saved graph unavailable", unavailable_state_head: { state_id: stateId, version: 7 } };

  it("allows a fresh browser to request recovery against the exact unreadable head", async () => {
    vi.mocked(api.fetchCompositionState).mockRejectedValueOnce(unavailable);
    await useSessionStore.getState().selectSession(sid);
    expect(useSessionStore.getState().compositionState).toBeNull();
    expect(useSessionStore.getState().compositionStateLoaded).toBe(true);
    expect(useSessionStore.getState().error).toContain("saved graph is unavailable");
    expect(useSessionStore.getState().unavailableCompositionHead).toEqual({ sessionId: sid, ...unavailable.unavailable_state_head });
    vi.mocked(api.fetchComposerOperation).mockImplementation(async (_session, operationId) => ({ ...snapshot(operationId), result: { message: assistant, state: composition(8), proposals: [] } }));
    vi.mocked(api.fetchCompositionState).mockResolvedValue(composition(8));
    await useSessionStore.getState().sendMessage("Restore a safe draft and explain what happened");
    expect(api.submitComposerOperation).toHaveBeenCalledWith(expect.objectContaining({ body: expect.objectContaining({ state_id: stateId }) }));
    expect(useSessionStore.getState().compositionState?.version).toBe(8);
    expect(useSessionStore.getState().unavailableCompositionHead).toBeNull();
    expect(useSessionStore.getState().isComposing).toBe(false);
  });

  it.each([{ status: 403 }, { status: 500 }, { status: 409, error_type: "stale_compose_state" }, { status: 409, error_type: "composition_state_unavailable" }])("keeps ordinary or malformed failures gated: %j", async (error) => {
    vi.mocked(api.fetchCompositionState).mockRejectedValueOnce(error);
    await useSessionStore.getState().selectSession(sid);
    expect(useSessionStore.getState().compositionStateLoaded).toBe(false);
    expect(useSessionStore.getState().unavailableCompositionHead).toBeNull();
    await useSessionStore.getState().sendMessage("Recovery");
    expect(api.submitComposerOperation).not.toHaveBeenCalled();
  });

  it("does not publish a late unreadable-head response into another session", async () => {
    let rejectOld!: (error: unknown) => void;
    vi.mocked(api.fetchCompositionState).mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectOld = reject; }));
    const selectingOld = useSessionStore.getState().selectSession(sid);
    const other = "55555555-5555-4555-8555-555555555555";
    vi.mocked(api.fetchCompositionState).mockResolvedValueOnce(null);
    await useSessionStore.getState().selectSession(other);
    rejectOld(unavailable);
    await selectingOld;
    expect(useSessionStore.getState().activeSessionId).toBe(other);
    expect(useSessionStore.getState().compositionState).toBeNull();
    expect(useSessionStore.getState().unavailableCompositionHead).toBeNull();
    expect(useSessionStore.getState().error).toBeNull();
  });

  it("retains system receipts when the independent graph refresh fails", async () => {
    const receipt: ChatMessage = { ...assistant, id: "66666666-6666-4666-8666-666666666666", role: "system", content: "Restored a saved draft. The broken candidate was not retained." };
    vi.mocked(api.fetchMessages).mockResolvedValue([receipt, assistant]);
    vi.mocked(api.fetchCompositionState).mockRejectedValue(new Error("refresh failed"));
    await useSessionStore.getState().sendMessage("Recovery");
    expect(useSessionStore.getState().messages).toEqual(expect.arrayContaining([receipt, assistant]));
    expect(useSessionStore.getState().compositionStateLoaded).toBe(false);
    expect(useSessionStore.getState().isComposing).toBe(false);
    expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
  });

  it("publishes the actual terminal receipt during total refresh failure and permits fenced continuation", async () => {
    const receipt: ChatMessage = { ...assistant, id: "66666666-6666-4666-8666-666666666666", role: "system", content: "Restored a saved draft. The broken candidate was not retained." };
    vi.mocked(api.fetchComposerOperation).mockImplementation(async (_session, operationId) => ({ ...snapshot(operationId), result: { message: assistant, state: composition(8), proposals: [], recovery_notice: receipt } }));
    vi.mocked(api.fetchMessages).mockRejectedValue(new Error("refresh failed"));
    vi.mocked(api.fetchCompositionState).mockRejectedValue(new Error("refresh failed"));
    vi.mocked(api.fetchCompositionProposals).mockRejectedValue(new Error("refresh failed"));
    await useSessionStore.getState().sendMessage("Recovery");
    expect(useSessionStore.getState().messages).toEqual(expect.arrayContaining([receipt, assistant]));
    expect(useSessionStore.getState().compositionStateLoaded).toBe(true);
    expect(useSessionStore.getState().compositionState?.version).toBe(8);
    expect(useSessionStore.getState().isComposing).toBe(false);
    expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
    await useSessionStore.getState().sendMessage("Continue editing");
    expect(api.submitComposerOperation).toHaveBeenCalledTimes(2);
    expect(useSessionStore.getState().messages.filter((row) => row.id === receipt.id)).toHaveLength(1);
  });

  it("does not resurrect an older decoded graph over a newer unavailable head", async () => {
    vi.mocked(api.fetchCompositionState).mockRejectedValue(unavailable);
    await useSessionStore.getState().sendMessage("Recovery");
    expect(useSessionStore.getState().compositionState).toBeNull();
    expect(useSessionStore.getState().unavailableCompositionHead?.version).toBe(7);
    expect(useSessionStore.getState().error).toContain("saved graph is unavailable");
    expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
  });
});
describe("expired live composer observation", () => {
  function holdLiveStream(): void {
    vi.mocked(api.fetchComposerOperationStream).mockImplementation(async (sessionId, operationId, signal) => {
      let heartbeat: ReturnType<typeof setInterval>;
      let sequence = 0;
      const body = new ReadableStream<Uint8Array>({ start(controller) {
        const emit = (event: "status" | "heartbeat", payload?: unknown) => controller.enqueue(new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify({ schema_version: "composer-operation-stream.v1", session_id: sessionId, operation_id: operationId, sequence: sequence++, event, ...(payload === undefined ? {} : { payload }) })}\n\n`));
        emit("status", { status: "running", cancel_requested: false, deadline_remaining_ms: 0 });
        heartbeat = setInterval(() => emit("heartbeat"), 5000);
        signal.addEventListener("abort", () => { clearInterval(heartbeat); controller.error(new DOMException("aborted", "AbortError")); }, { once: true });
      }, cancel() { clearInterval(heartbeat); } });
      return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
    });
  }
  it("surfaces recovery when a live stream expires without replaying or claiming failure", async () => {
    vi.useFakeTimers(); holdLiveStream();
    const sending = useSessionStore.getState().composeRequest("send", "hello");
    await vi.advanceTimersByTimeAsync(24999);
    expect(useSessionStore.getState().error).toBeNull();
    await vi.advanceTimersByTimeAsync(1); await sending;
    const held = findComposerOperationCustody(scope, sid).foreground!;
    expect(useSessionStore.getState().error).not.toBeNull();
    expect(useSessionStore.getState().error).toMatch(/Reload this session/);
    expect(useSessionStore.getState().error).toMatch(/Stop/);
    expect(useSessionStore.getState().isComposing).toBe(true);
    expect(useSessionStore.getState().composeRequests.size).toBe(0);
    expect(useSessionStore.getState().messages.find((row) => row.operation_id === held.operationId)?.local_status).toBe("pending");
    expect(api.fetchMessages).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(60000);
    await useSessionStore.getState().composeRequest("send", "must remain gated");
    expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
    expect(api.fetchComposerOperationStream).toHaveBeenCalledTimes(1);
    expect(api.fetchComposerOperation).not.toHaveBeenCalled();
    expect(api.cancelComposerOperation).not.toHaveBeenCalled();
  });
  it("Stop after expiration reconciles the held ID and publishes its terminal", async () => {
    vi.useFakeTimers(); holdLiveStream();
    const sending = useSessionStore.getState().composeRequest("send", "hello");
    await vi.advanceTimersByTimeAsync(25000); await sending;
    expect(useSessionStore.getState().error).not.toBeNull();
    expect(useSessionStore.getState().error).toMatch(/Reload this session/);
    const held = findComposerOperationCustody(scope, sid).foreground!;
    vi.mocked(api.fetchComposerOperationStream).mockRejectedValue(new Error("stream unavailable"));
    vi.mocked(api.cancelComposerOperation).mockResolvedValue({ ...snapshot(held.operationId), status: "running", cancel_requested: true, result: null });
    useSessionStore.getState().cancelComposition();
    await vi.advanceTimersByTimeAsync(0);
    expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
    expect(useSessionStore.getState().isComposing).toBe(false);
    expect(useSessionStore.getState().error).toBeNull();
    expect(useSessionStore.getState().messages.some((row) => row.id === assistantId)).toBe(true);
    expect(vi.mocked(api.cancelComposerOperation).mock.calls.every(([sessionId, operationId]) => sessionId === sid && operationId === held.operationId)).toBe(true);
    expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
  });
  it("surfaces recovery when durable polling reaches the same deadline", async () => {
    vi.useFakeTimers();
    vi.mocked(api.fetchComposerOperation).mockImplementation(async (_session, operationId) => ({ ...snapshot(operationId), status: "running", result: null, deadline_remaining_ms: 0, poll_after_ms: 60000 }));
    const sending = useSessionStore.getState().composeRequest("send", "hello");
    await vi.advanceTimersByTimeAsync(25000); await sending;
    expect(useSessionStore.getState().error).not.toBeNull();
    expect(useSessionStore.getState().error).toMatch(/Reload this session/);
    expect(useSessionStore.getState().isComposing).toBe(true);
    expect(findComposerOperationCustody(scope, sid).foreground).not.toBeNull();
    expect(api.fetchComposerOperation).toHaveBeenCalledTimes(1);
    expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
    expect(api.cancelComposerOperation).not.toHaveBeenCalled();
  });
  it("does not publish an old deadline after explicit same-session observation replacement", async () => {
    vi.useFakeTimers(); holdLiveStream();
    const sending = useSessionStore.getState().composeRequest("send", "hello");
    await vi.advanceTimersByTimeAsync(24000);
    vi.mocked(api.fetchComposerOperation).mockImplementation(async (_session, operationId) => ({ ...snapshot(operationId), status: "running", result: null, deadline_remaining_ms: 300000 }));
    await useSessionStore.getState().resumeComposerOperation(sid);
    await vi.advanceTimersByTimeAsync(2000); await sending;
    expect(useSessionStore.getState().error).toBeNull();
    expect(useSessionStore.getState().isComposing).toBe(true);
    expect(findComposerOperationCustody(scope, sid).foreground).not.toBeNull();
    expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
  });
  it("does not publish timeout recovery after authentication generation changes", async () => {
    vi.useFakeTimers(); holdLiveStream();
    const sending = useSessionStore.getState().composeRequest("send", "hello");
    await vi.advanceTimersByTimeAsync(24000); advanceAuthGeneration();
    useSessionStore.setState({ error: "new authentication context" });
    await vi.advanceTimersByTimeAsync(1000); await sending;
    expect(useSessionStore.getState().error).toBe("new authentication context");
    expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
    expect(api.fetchMessages).not.toHaveBeenCalled();
  });
  it("does not publish timeout recovery after a session switch", async () => {
    vi.useFakeTimers(); holdLiveStream();
    const sending = useSessionStore.getState().composeRequest("send", "hello");
    await vi.advanceTimersByTimeAsync(24000);
    await useSessionStore.getState().selectSession("77777777-7777-4777-8777-777777777777");
    await vi.advanceTimersByTimeAsync(1000); await sending;
    expect(useSessionStore.getState().activeSessionId).toBe("77777777-7777-4777-8777-777777777777");
    expect(useSessionStore.getState().error).toBeNull();
    expect(findComposerOperationCustody(scope, sid).foreground).not.toBeNull();
    expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
  });
});
describe("durable composer store integration", () => {
  it("holds Send until the authoritative session state loaded", async () => {
    useSessionStore.setState({ compositionStateLoaded: false }); await useSessionStore.getState().sendMessage("hello"); expect(api.submitComposerOperation).not.toHaveBeenCalled();
    useSessionStore.setState({ compositionStateLoaded: true }); await useSessionStore.getState().sendMessage("hello"); expect(api.submitComposerOperation).toHaveBeenCalledTimes(1); expect(useSessionStore.getState().messages.some((row) => row.id === assistantId)).toBe(true);
  });
  it("deduplicates canonical transcript and final completed answer", async () => {
    vi.mocked(api.fetchMessages).mockResolvedValue([assistant]); await useSessionStore.getState().sendMessage("hello");
    expect(useSessionStore.getState().messages.filter((row) => row.id === assistantId)).toHaveLength(1); expect(useSessionStore.getState().isComposing).toBe(false);
  });
  it("Retry of stale terminal mints a new ID with the current head", async () => {
    const failure = { status: 409, error_type: "stale_compose_state", detail: "Session changed" };
    vi.mocked(api.apiErrorFromBody).mockResolvedValue(failure);
    vi.mocked(api.fetchComposerOperation).mockImplementationOnce(async (_session, operationId) => ({ ...snapshot(operationId), status: "failed", result: null, error: { http_status: 409, failure_code: "http_error", error_type: "stale_compose_state", diagnostic_id: null, body: { detail: "Session changed" } } }));
    await useSessionStore.getState().sendMessage("hello"); const row = useSessionStore.getState().messages.find((item) => item.role === "user")!; expect(row.local_failure_code).toBe("stale_compose_state");
    const first = vi.mocked(api.submitComposerOperation).mock.calls[0][0]; await useSessionStore.getState().retryMessage(row.id); const second = vi.mocked(api.submitComposerOperation).mock.calls[1][0];
    expect(second.operationId).not.toBe(first.operationId); expect(second.body.state_id).toBe(stateId);
  });
  it("a delayed terminal reload cannot overwrite a newer state or proposal decisions", async () => {
    let release!: (messages: ChatMessage[]) => void;
    vi.mocked(api.fetchMessages).mockReturnValue(new Promise((resolve) => { release = resolve; }));
    const proposal: CompositionProposal = { id: "66666666-6666-4666-8666-666666666666", session_id: sid, tool_call_id: "call", tool_name: "set_pipeline", status: "pending", summary: "change", rationale: "requested", affects: [], arguments_redacted_json: {}, base_state_id: stateId, committed_state_id: null, audit_event_id: null, pipeline_metadata: null, created_at: new Date().toISOString(), updated_at: new Date().toISOString() };
    vi.mocked(api.fetchCompositionProposals).mockResolvedValue([proposal]);
    const sending = useSessionStore.getState().sendMessage("hello");
    await vi.waitFor(() => expect(api.fetchMessages).toHaveBeenCalled());
    const newer = { ...proposal, id: "77777777-7777-4777-8777-777777777777" };
    const laterRow = { ...assistant, id: "88888888-8888-4888-8888-888888888888", content: "newer durable arrival", sequence_no: 2 };
    useSessionStore.setState({ messages: [...useSessionStore.getState().messages, laterRow], compositionState: composition(2), compositionProposals: [{ ...proposal, status: "rejected" }, newer] });
    release([]); await sending;
    expect(useSessionStore.getState().compositionState?.version).toBe(2);
    expect(useSessionStore.getState().messages.find((row) => row.id === laterRow.id)?.content).toBe("newer durable arrival");
    expect(useSessionStore.getState().compositionProposals.find((item) => item.id === proposal.id)?.status).toBe("rejected");
    expect(useSessionStore.getState().compositionProposals.some((item) => item.id === newer.id)).toBe(true);
  });
  it("recompose binds each fresh action even when the expected user is identical", async () => {
    const user: ChatMessage = { ...assistant, id: userId, role: "user", local_status: "failed" };
    useSessionStore.setState({ messages: [user] }); vi.mocked(api.fetchComposerOperation).mockImplementation(async (_session, operationId) => snapshot(operationId, "compose_recompose"));
    await useSessionStore.getState().retryMessage(userId); const first = vi.mocked(api.submitComposerOperation).mock.calls[0][0];
    useSessionStore.setState({ messages: [user] }); await useSessionStore.getState().retryMessage(userId); const second = vi.mocked(api.submitComposerOperation).mock.calls[1][0];
    expect(first.kind).toBe("compose_recompose"); expect(second.operationId).not.toBe(first.operationId); expect(second.kind === "compose_recompose" && second.body.expected_user_message_id).toBe(userId);
  });
  it("restores pending custody only after fresh authenticated scope reads", async () => {
    const operationId = "55555555-5555-4555-8555-555555555555";
    acquireComposerOperationCustody({ mode: "submitted", scope, sessionId: sid, operationId, createdAt: Date.now(), kind: "compose_message", body: { operation_id: operationId, content: "hello", state_id: stateId } });
    await useSessionStore.getState().resumeComposerOperation(sid); await vi.waitFor(() => expect(findComposerOperationCustody(scope, sid).foreground).toBeNull());
    expect(api.fetchCurrentUser).toHaveBeenCalled(); expect(api.fetchAuthConfig).toHaveBeenCalled(); expect(api.submitComposerOperation).not.toHaveBeenCalled();
  });
  it("an active-action loser retains its own draft without relabelling it", async () => {
    const winnerId = "66666666-6666-4666-8666-666666666666";
    const refusal = { status: 409, error_type: "composer_operation_active", operation_id: winnerId, kind: "compose_message" };
    vi.mocked(api.submitComposerOperation).mockRejectedValue(refusal);
    vi.mocked(api.composerActiveOperation).mockReturnValue({ operation_id: winnerId, kind: "compose_message" });
    vi.mocked(api.fetchComposerOperation).mockResolvedValue(snapshot(winnerId));
    const winnerUser: ChatMessage = { ...assistant, id: userId, role: "user", operation_id: winnerId, content: "the other tab's request" };
    vi.mocked(api.fetchMessages).mockResolvedValue([winnerUser, assistant]);
    await useSessionStore.getState().sendMessage("my unsent draft");
    const losing = useSessionStore.getState().messages.find((row) => row.content === "my unsent draft")!;
    expect(losing.operation_id).not.toBe(winnerId); expect(losing.local_status).toBe("failed");
    expect(api.submitComposerOperation).toHaveBeenCalledTimes(1); expect(api.fetchComposerOperation).toHaveBeenCalledWith(sid, winnerId);
    expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
  });
  it("refuses retry when authoritative history already contains a genuine reply", async () => {
    useSessionStore.setState({ messages: [{ ...assistant, id: userId, role: "user", local_status: "failed" }, assistant] });
    await useSessionStore.getState().retryMessage(userId); expect(api.submitComposerOperation).not.toHaveBeenCalled();
  });
});

describe("terminal proposal snapshot ordering", () => {
  const proposal: CompositionProposal = { id: "66666666-6666-4666-8666-666666666666", session_id: sid, tool_call_id: "call", tool_name: "set_pipeline", status: "pending", summary: "change", rationale: "requested", affects: [], arguments_redacted_json: {}, base_state_id: stateId, committed_state_id: null, audit_event_id: null, pipeline_metadata: null, created_at: "2026-10-06T00:00:00Z", updated_at: "2026-10-06T00:00:00Z" };
  it("does not resurrect a proposal retired by a newer authoritative read during terminal hydration", async () => {
    useSessionStore.setState({ compositionProposals: [proposal] });
    let release!: (rows: CompositionProposal[]) => void;
    vi.mocked(api.fetchCompositionProposals).mockReturnValueOnce(new Promise((resolve) => { release = resolve; })).mockResolvedValue([]);
    vi.mocked(api.fetchComposerOperation).mockImplementation(async (_sid, id) => ({ ...snapshot(id), result: { message: assistant, state: composition(), proposals: [proposal] } }));
    const sending = useSessionStore.getState().sendMessage("hello");
    await vi.waitFor(() => expect(api.fetchCompositionProposals).toHaveBeenCalledTimes(1));
    await useSessionStore.getState().loadCompositionProposals(sid);
    expect(useSessionStore.getState().compositionProposals).toEqual([]);
    release([proposal]); await sending;
    expect(useSessionStore.getState().compositionProposals).toEqual([]);
    expect(useSessionStore.getState().messages.some((row) => row.id === assistantId)).toBe(true);
  });
  it("allows a newer authoritative read to retire a proposal hydrated by the completed operation", async () => {
    vi.mocked(api.fetchCompositionProposals).mockResolvedValueOnce([proposal]).mockResolvedValue([]);
    vi.mocked(api.fetchComposerOperation).mockImplementation(async (_sid, id) => ({ ...snapshot(id), result: { message: assistant, state: composition(), proposals: [proposal] } }));
    await useSessionStore.getState().sendMessage("hello");
    expect(useSessionStore.getState().compositionProposals).toEqual([proposal]);
    await useSessionStore.getState().loadCompositionProposals(sid);
    expect(useSessionStore.getState().compositionProposals).toEqual([]);
  });
});

it("publishes an older failed terminal without waiting for transcript refresh", async () => {
  const old = acquireComposerOperationCustody({ mode: "submitted", scope, sessionId: sid, operationId: userId, createdAt: Date.now(), kind: "compose_message", body: { operation_id: userId, content: "older", state_id: stateId } });
  const winner = attachComposerObserver(old, assistantId, "compose_message", true); settleComposerCustody(winner);
  useSessionStore.setState({ isComposing: true });
  let release!: (rows: ChatMessage[]) => void;
  vi.mocked(api.fetchMessages).mockReturnValue(new Promise((resolve) => { release = resolve; }));
  vi.mocked(api.fetchCompositionState).mockResolvedValue(composition(2));
  vi.mocked(api.apiErrorFromBody).mockReturnValue({ status: 499, error_type: "request_cancelled", detail: "stopped" });
  vi.mocked(api.fetchComposerOperation).mockResolvedValue({ ...snapshot(userId), status: "failed", result: null, error: { http_status: 499, failure_code: "request_cancelled", error_type: "request_cancelled", diagnostic_id: null, body: { detail: "stopped" } } });
  await useSessionStore.getState().resumeComposerOperation(sid);
  await vi.waitFor(() => expect(api.fetchMessages).toHaveBeenCalled());
  expect(useSessionStore.getState().isComposing).toBe(false);
  release([assistant]); await vi.waitFor(() => expect(useSessionStore.getState().isComposing).toBe(false));
  expect(useSessionStore.getState().compositionState?.version).toBe(2);
  expect(api.fetchCompositionProposals).toHaveBeenCalledWith(sid, expect.any(AbortSignal)); expect(api.submitComposerOperation).not.toHaveBeenCalled();
});
it("failed graph refresh leaves authoring gated until authoritative refresh succeeds", async () => {
  vi.mocked(api.fetchCompositionState).mockRejectedValue(new Error("head unavailable"));
  vi.mocked(api.fetchMessages).mockRejectedValue(new Error("partial traversal"));
  await useSessionStore.getState().sendMessage("hello");
  expect(useSessionStore.getState().compositionStateLoaded).toBe(false);
  await useSessionStore.getState().sendMessage("blocked"); expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
});

it("message refresh failure does not discard an independently refreshed head", async () => {
  vi.mocked(api.fetchMessages).mockRejectedValue(new Error("partial traversal"));
  await useSessionStore.getState().sendMessage("hello");
  expect(useSessionStore.getState().compositionStateLoaded).toBe(true);
  expect(useSessionStore.getState().messages).toContainEqual(assistant);
  await useSessionStore.getState().sendMessage("continue");
  expect(api.submitComposerOperation).toHaveBeenCalledTimes(2);
});

it("scope recovery reaches an inactive deleted expired session without discarding its valid sibling", async () => {
  const sibling = "77777777-7777-4777-8777-777777777777";
  purgeComposerCustody();
  const pending = { mode: "submitted", scope, sessionId: sid, operationId: userId, createdAt: Date.now() - COMPOSER_CUSTODY_LIFETIME_MS - 1, kind: "compose_message", body: { operation_id: userId, content: "held", state_id: null } };
  const fresh = { ...pending, sessionId: sibling, createdAt: Date.now() };
  const raw = JSON.stringify({ schema: "composer-operations.v1", entries: [pending, fresh].map((foreground) => ({ foreground, unresolvedSubmissions: [] })) });
  sessionStorage.setItem(COMPOSER_CUSTODY_KEY, raw); authenticateComposerCustody(scope);
  useSessionStore.setState({ activeSessionId: sibling, compositionState: { ...composition(), session_id: sibling } });
  vi.mocked(api.fetchComposerOperation).mockImplementation(async (sessionId, operationId) => sessionId === sid ? { kind: "session_missing" } : { ...snapshot(operationId), result: { message: { ...assistant, session_id: sibling }, state: { ...composition(2), session_id: sibling }, proposals: [] } });
  await useSessionStore.getState().reconcileInactiveComposerCustody();
  await vi.waitFor(() => expect(findComposerOperationCustody(scope, sid).foreground).toBeNull());
  expect(findComposerOperationCustody(scope, sibling).foreground?.operationId).toBe(userId);
  expect(api.fetchComposerOperation).toHaveBeenCalledWith(sid, userId); expect(api.submitComposerOperation).not.toHaveBeenCalled();
  expect(sessionStorage.getItem(COMPOSER_CUSTODY_KEY)).toBe(raw);
  await useSessionStore.getState().resumeComposerOperation(sibling);
  await vi.waitFor(() => expect(findComposerOperationCustody(scope, sibling).foreground).toBeNull());
  expect(useSessionStore.getState().activeSessionId).toBe(sibling);
});

it("actual Stop persistence denial preserves the draft and tells the user to keep the tab open", async () => {
  const held = acquireComposerOperationCustody({ mode: "submitted", scope, sessionId: sid, operationId: userId, createdAt: Date.now(), kind: "compose_message", body: { operation_id: userId, content: "held draft", state_id: stateId } });
  const setItem = Storage.prototype.setItem;
  const denied = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key: string, value: string) { if (this === sessionStorage && key === COMPOSER_STOP_KEY) throw new DOMException("denied", "QuotaExceededError"); setItem.call(this, key, value); });
  let release!: (value: ComposerOperationSnapshot) => void;
  vi.mocked(api.fetchComposerOperation).mockReturnValue(new Promise((resolve) => { release = resolve; }));
  vi.mocked(api.cancelComposerOperation).mockResolvedValue({ kind: "operation_missing" });
  useSessionStore.getState().cancelComposition();
  expect(useSessionStore.getState().error).toMatch(/Keep this tab open/);
  expect(findComposerOperationCustody(scope, sid).foreground).toEqual(held);
  expect(held.kind === "compose_message" && held.body.content).toBe("held draft");
  await vi.waitFor(() => expect(api.fetchComposerOperation).toHaveBeenCalled());
  denied.mockRestore(); release(snapshot(userId));
  await vi.waitFor(() => expect(findComposerOperationCustody(scope, sid).foreground).toBeNull());
});

describe("authoritative load and revert publication", () => {
  it("a failed initial state read cannot authorize a null-base POST", async () => {
    vi.mocked(api.fetchCompositionState).mockRejectedValueOnce({ status: 503 });
    await useSessionStore.getState().selectSession(sid);
    expect(useSessionStore.getState().compositionStateLoaded).toBe(false);
    await useSessionStore.getState().sendMessage("must wait"); expect(api.submitComposerOperation).not.toHaveBeenCalled();
    await useSessionStore.getState().selectSession(sid);
    expect(useSessionStore.getState().compositionStateLoaded).toBe(true);
    await useSessionStore.getState().sendMessage("explicit gesture");
    expect(vi.mocked(api.submitComposerOperation).mock.calls[0][0].body.state_id).toBe(stateId);
  });
  it("A-B-A cannot revive a held reversion from the prior session epoch", async () => {
    const other = "77777777-7777-4777-8777-777777777777";
    let release!: (state: CompositionState) => void;
    vi.mocked(api.revertToVersion).mockReturnValue(new Promise((resolve) => { release = resolve; }));
    const reverting = useSessionStore.getState().revertToVersion(stateId);
    await vi.waitFor(() => expect(api.revertToVersion).toHaveBeenCalled());
    vi.mocked(api.fetchCompositionState).mockImplementation(async (sessionId) => ({ ...composition(2), session_id: sessionId }));
    await useSessionStore.getState().selectSession(other); await useSessionStore.getState().selectSession(sid);
    release(composition(1)); await reverting;
    expect(useSessionStore.getState().compositionState?.version).toBe(2);
  });
  it("a later same-session head wins over an older held reversion", async () => {
    let release!: (state: CompositionState) => void;
    vi.mocked(api.revertToVersion).mockReturnValue(new Promise((resolve) => { release = resolve; }));
    const reverting = useSessionStore.getState().revertToVersion(stateId);
    await vi.waitFor(() => expect(api.revertToVersion).toHaveBeenCalled());
    useSessionStore.setState({ compositionState: composition(3) });
    release(composition(2)); await reverting;
    expect(useSessionStore.getState().compositionState?.version).toBe(3);
  });
});

it("late version history cannot cross A-B-A or mask the current session's history", async () => {
  let release!: (rows: Awaited<ReturnType<typeof api.fetchStateVersions>>) => void;
  vi.mocked(api.fetchStateVersions).mockReturnValue(new Promise((resolve) => { release = resolve; }));
  const loading = useSessionStore.getState().loadStateVersions();
  const other = "77777777-7777-4777-8777-777777777777";
  await useSessionStore.getState().selectSession(other); await useSessionStore.getState().selectSession(sid);
  release([{ id: stateId, version: 1, created_at: "2026-10-06T00:00:00Z", node_count: 1 }]); await loading;
  expect(useSessionStore.getState().stateVersions).toEqual([]);
  vi.mocked(api.fetchStateVersions).mockResolvedValue([{ id: stateId, version: 2, created_at: "2026-10-06T00:00:00Z", node_count: 2 }]);
  await useSessionStore.getState().loadStateVersions(); expect(useSessionStore.getState().stateVersions[0].version).toBe(2);
});

it("newer same-session version history wins over an older late response", async () => {
  type Rows = Awaited<ReturnType<typeof api.fetchStateVersions>>;
  let releaseOld!: (rows: Rows) => void;
  const oldRows: Rows = [{ id: stateId, version: 1, created_at: "2026-10-06T00:00:00Z", node_count: 1 }];
  const newRows: Rows = [{ ...oldRows[0], id: "88888888-8888-4888-8888-888888888888", version: 2 }, ...oldRows];
  vi.mocked(api.fetchStateVersions).mockReturnValueOnce(new Promise((resolve) => { releaseOld = resolve; })).mockResolvedValueOnce(newRows);
  const oldRead = useSessionStore.getState().loadStateVersions();
  await useSessionStore.getState().loadStateVersions();
  expect(useSessionStore.getState().stateVersions).toEqual(newRows);
  releaseOld(oldRows); await oldRead;
  expect(useSessionStore.getState().stateVersions).toEqual(newRows);
  expect(useSessionStore.getState().isLoadingVersions).toBe(false);
});

it("an older failed history read cannot clear a newer read's loading state", async () => {
  type Rows = Awaited<ReturnType<typeof api.fetchStateVersions>>;
  let rejectOld!: (error: Error) => void;
  let releaseNew!: (rows: Rows) => void;
  vi.mocked(api.fetchStateVersions)
    .mockReturnValueOnce(new Promise((_resolve, reject) => { rejectOld = reject; }))
    .mockReturnValueOnce(new Promise((resolve) => { releaseNew = resolve; }));
  const oldRead = useSessionStore.getState().loadStateVersions();
  const newRead = useSessionStore.getState().loadStateVersions();
  rejectOld(new Error("old read failed")); await oldRead;
  expect(useSessionStore.getState().isLoadingVersions).toBe(true);
  const rows: Rows = [{ id: stateId, version: 2, created_at: "2026-10-06T00:00:00Z", node_count: 2 }];
  releaseNew(rows); await newRead;
  expect(useSessionStore.getState().stateVersions).toEqual(rows);
  expect(useSessionStore.getState().isLoadingVersions).toBe(false);
});
