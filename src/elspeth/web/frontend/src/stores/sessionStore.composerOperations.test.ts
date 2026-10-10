import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSessionStore } from "./sessionStore";
import { COMPOSER_STOP_KEY, COMPOSER_CUSTODY_KEY, COMPOSER_CUSTODY_LIFETIME_MS, attachComposerObserver, settleComposerCustody, acquireComposerOperationCustody, authenticateComposerCustody, findComposerOperationCustody, purgeComposerCustody } from "./composerOperationCustody";
import { detachComposerObservers } from "@/api/composerOperationObserver";
import { advanceAuthGeneration } from "@/api/authSession";
import { compositionStateAuthorityFields } from "@/test/composerFixtures";
import type { ComposerOperationSnapshot } from "@/types/composerOperations";
import type { ChatMessage, CompositionState, CompositionProposal } from "@/types/index";
vi.mock("@/api/client", () => ({ submitComposerOperation: vi.fn(), fetchComposerOperation: vi.fn(), cancelComposerOperation: vi.fn(), fetchComposerOperationStream: vi.fn(), apiErrorFromBody: vi.fn(), composerActiveOperation: vi.fn(), isDefinitiveComposerRefusal: vi.fn(), fetchMessages: vi.fn(), fetchCompositionState: vi.fn(), fetchCompositionProposals: vi.fn(), fetchComposerPreferences: vi.fn(), revertToVersion: vi.fn(), fetchStateVersions: vi.fn(), fetchSessions: vi.fn(), fetchCurrentUser: vi.fn(), fetchAuthConfig: vi.fn(), listBlobs: vi.fn(), listInterpretationEvents: vi.fn() }));
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
  vi.mocked(api.composerActiveOperation).mockReturnValue(null); vi.mocked(api.isDefinitiveComposerRefusal).mockReturnValue(false);
  vi.mocked(api.fetchMessages).mockResolvedValue([]); vi.mocked(api.fetchCompositionState).mockResolvedValue(composition()); vi.mocked(api.fetchCompositionProposals).mockResolvedValue([]); vi.mocked(api.fetchSessions).mockResolvedValue([]); vi.mocked(api.listBlobs).mockResolvedValue([]); vi.mocked(api.listInterpretationEvents).mockResolvedValue([]);
  vi.mocked(api.fetchComposerPreferences).mockImplementation(async (sessionId) => ({ session_id: sessionId, trust_mode: "explicit_approve", density_default: "medium", interpretation_review_disabled: false, updated_at: "2026-10-06T00:00:00Z" }));
  vi.mocked(api.fetchCurrentUser).mockResolvedValue({ user_id: scope.principalId, username: "principal", display_name: null, email: null, groups: [], dev_admin: false }); vi.mocked(api.fetchAuthConfig).mockResolvedValue({ provider: "local", registration_mode: "closed", sso_start_url: null });
});
afterEach(() => { detachComposerObservers(); vi.useRealTimers(); purgeComposerCustody(); });
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

it("older unresolved failed terminal hydrates saved state before reopening Send", async () => {
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
  expect(useSessionStore.getState().isComposing).toBe(true);
  release([assistant]); await vi.waitFor(() => expect(useSessionStore.getState().isComposing).toBe(false));
  expect(useSessionStore.getState().compositionState?.version).toBe(2);
  expect(api.fetchCompositionProposals).toHaveBeenCalledWith(sid); expect(api.submitComposerOperation).not.toHaveBeenCalled();
});
it("failed terminal refresh leaves authoring gated until authoritative refresh succeeds", async () => {
  vi.mocked(api.fetchMessages).mockRejectedValue(new Error("partial traversal"));
  await useSessionStore.getState().sendMessage("hello");
  expect(useSessionStore.getState().compositionStateLoaded).toBe(false);
  await useSessionStore.getState().sendMessage("blocked"); expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
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
