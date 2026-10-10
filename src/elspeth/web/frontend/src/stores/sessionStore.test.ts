import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { createInterpretationResolutionHandler, useSessionStore } from "./sessionStore";
import { useBlobStore } from "./blobStore";
import { useInterpretationEventsStore } from "./interpretationEventsStore";
import { resetStore } from "@/test/store-helpers";
import { installComposeTurnDouble } from "@/test/composeTurnDouble";
import recoveryProducerFixtures from "@/test/composerRecoveryProducerFixtures.json";
import { acquireComposerOperationCustody, findComposerOperationCustody, authenticateComposerCustody, purgeComposerCustody } from "./composerOperationCustody";
import type {
  ApiError,
  ChatMessage,
  ComposerPreferences,
  ComposerRecoveryError,
  ComposerProgressSnapshot,
  CompositionState,
  CompositionProposal,
  BlobMetadata,
} from "@/types/api";
import type { InterpretationEvent } from "@/types/interpretation";
import { compositionStateAuthorityFields } from "@/test/composerFixtures";
import { clearAllSessionOperationRetries } from "./sessionOperationRetry";

const clearValidationMock = vi.hoisted(() => vi.fn());
const validateMock = vi.hoisted(() => vi.fn());

// Mock the API client — store tests verify state logic, not HTTP calls
vi.mock("@/api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/api/client")>(),
  submitComposerOperation: vi.fn(),
  fetchComposerOperation: vi.fn(),
  fetchComposerOperationStream: vi.fn(),
  cancelComposerOperation: vi.fn(),
  fetchCurrentUser: vi.fn(),
  fetchAuthConfig: vi.fn(),
  fetchSessions: vi.fn(),
  createSession: vi.fn(),
  fetchMessages: vi.fn(),
  fetchCompositionState: vi.fn(),
  fetchCompositionProposals: vi.fn(),
  fetchComposerPreferences: vi.fn(),
  fetchComposerProgress: vi.fn(),
  sendMessage: vi.fn(),
  recompose: vi.fn(),
  acceptCompositionProposal: vi.fn(),
  rejectCompositionProposal: vi.fn(),
  forkFromMessage: vi.fn(),
  isForkCommittedResponseError: (error: unknown) =>
    typeof error === "object" && error !== null && "committedSuccessResponse" in error,
  revertToVersion: vi.fn(),
  fetchStateVersions: vi.fn(),
  archiveSession: vi.fn(),
  renameSession: vi.fn(),
  // Preference bootstrap is independent of session creation.
  fetchUserComposerPreferences: vi.fn().mockResolvedValue({
    tutorial_completed_at: null,
    tutorial_stage: null,
    tutorial_session_id: null,
    tutorial_run_id: null,
    tutorial_source_data_hash: null,
    updated_at: "2026-05-15T00:00:00Z",
  }),
  updateUserComposerPreferences: vi.fn(),
  // Phase 5b — sessionStore.selectSession fires a fire-and-forget refreshAll
  // on the interpretationEventsStore, which routes through this method.
  // Mocked to resolve with an empty array so the unhandled-rejection path
  // does not trip session-load tests; targeted assertions on the call live
  // in interpretationEventsStore.test.ts.
  listInterpretationEvents: vi.fn().mockResolvedValue([]),
  listBlobs: vi.fn(),
  uploadBlob: vi.fn(),
  deleteBlob: vi.fn(),
  downloadBlobContent: vi.fn(),
}));

// Mock the execution store dependency
vi.mock("./executionStore", () => ({
  useExecutionStore: {
    getState: () => ({
      clearValidation: clearValidationMock,
      validate: validateMock,
    }),
  },
}));

function makeCompositionState(version: number, nodeIds: string[] = []): CompositionState {
  return {
    id: `10000000-0000-4000-8000-${String(version).padStart(12, "0")}`,
    ...compositionStateAuthorityFields,
    session_id: "20000000-0000-4000-8000-000000000001",
    version,
    sources: {},
    nodes: nodeIds.map((id) => ({
      id,
      node_type: "transform",
      plugin: "passthrough",
      input: "source",
      on_success: "out",
      on_error: null,
      options: {},
    })),
    edges: [],
    outputs: [],
    metadata: { name: null, description: null },
  };
}


function makePendingInterpretationEvent(id: string): InterpretationEvent {
  return {
    id,
    session_id: "20000000-0000-4000-8000-000000000001",
    composition_state_id: "10000000-0000-4000-8000-000000000001",
    affected_node_id: "analyze_colors",
    tool_call_id: "call-1",
    user_term: "llm_model_choice:analyze_colors",
    kind: "llm_model_choice",
    llm_draft: "openrouter/openai/gpt-5.4-mini",
    accepted_value: null,
    choice: "pending",
    created_at: "2026-05-29T12:00:00Z",
    resolved_at: null,
    actor: "system:composer",
    interpretation_source: "user_approved",
    model_identifier: null,
    model_version: null,
    provider: null,
    composer_skill_hash: null,
    arguments_hash: null,
    hash_domain_version: null,
    runtime_model_identifier_at_resolve: null,
    runtime_model_version_at_resolve: null,
    approved_prompt_artifact_hash: null,
  };
}

function makeRecoveryError(
  partialState = makeCompositionState(2),
): ComposerRecoveryError {
  return {
    status: 500,
    detail: "compose failed",
    error_type: "composer_plugin_crash",
    partial_state: partialState,
    failed_turn: {
      ...recoveryProducerFixtures.recovery[1],
      tool_calls_attempted: 2,
      tool_responses_persisted: 1,
      transcript_url: null,
    },
  };
}

// ── R2-F9: wall-clock timeout 422 (elspeth-114dd261bc) ─────────────────────
//
// The route handler persists the salvaged partial pipeline as a NEW
// composition-state version and the next turn resumes from it, so the partial
// IS the session's current state. Leaving the pre-request graph on screen —
// under copy that says nothing was kept — is a lie the user acts on.
function makeTimeoutError(overrides: Partial<ApiError> = {}): ApiError {
  return {
    status: 422,
    error_type: "convergence",
    detail: "Composer did not converge within 6 turns (budget exhausted: timeout).",
    reason: "convergence_wall_clock_timeout",
    recovery_text:
      "Retry once the provider responds faster, or ask an operator to raise the composer wall-clock budget.",
    timeout_seconds: 240,
    partial_state: makeCompositionState(6),
    failed_turn: {
      ...recoveryProducerFixtures.recovery[1],
      tool_calls_attempted: 3,
      tool_responses_persisted: 3,
      transcript_url: null,
    },
    ...overrides,
  };
}

function makeCompositionProposal(
  overrides: Partial<CompositionProposal> = {},
): CompositionProposal {
  return {
    id: "proposal-1",
    session_id: "20000000-0000-4000-8000-000000000001",
    tool_call_id: "tool-call-1",
    tool_name: "set_pipeline",
    status: "pending",
    summary: "Replace the current pipeline.",
    rationale: "The user asked for a new pipeline.",
    affects: ["source", "transforms", "outputs"],
    arguments_redacted_json: { source: { plugin: "csv" } },
    base_state_id: "10000000-0000-4000-8000-000000000001",
    committed_state_id: null,
    audit_event_id: null,
    pipeline_metadata: null,
    created_at: "2026-05-14T00:00:00Z",
    updated_at: "2026-05-14T00:00:00Z",
    ...overrides,
  };
}

// Legacy behavioral obligations now use the durable operation boundary. A
// progress counter or closed subscription cannot settle a submitted action.
const durableSession = "20000000-0000-4000-8000-000000000001";
const durableScope = { principalId: "test-user", authProvider: "local" };
const durableUserId = "30000000-0000-4000-8000-000000000001";
const durableReplyId = "40000000-0000-4000-8000-000000000001";
function durableUser(operationId: string): ChatMessage {
  return { id: durableUserId, session_id: durableSession, role: "user", content: "hello", operation_id: operationId, tool_calls: null, created_at: "2026-10-06T00:00:00Z" };
}
function durableReply(): ChatMessage {
  return { id: durableReplyId, session_id: durableSession, role: "assistant", content: "Done", tool_calls: null, created_at: "2026-10-06T00:00:01Z" };
}
function durableCompleted(operationId: string, kind: "compose_message" | "compose_recompose" = "compose_message", version = 2): import("@/types/composerOperations").ComposerOperationSnapshot {
  return { operation_id: operationId, kind, status: "completed", cancel_requested: false, poll_after_ms: 100, deadline_at: "2030-01-01T00:00:00Z", deadline_remaining_ms: 0, result: { message: durableReply(), state: makeCompositionState(version), proposals: [] }, error: null };
}
async function armDurableTransport() {
  const api = await import("@/api/client");
  vi.mocked(api.submitComposerOperation).mockImplementation(async (descriptor) => ({ operation_id: descriptor.operationId, kind: descriptor.kind, status: "running", poll_after_ms: 100 }));
  vi.mocked(api.fetchComposerOperationStream).mockResolvedValue(new Response(null, { status: 503 }));
  vi.mocked(api.fetchMessages).mockImplementation(async () => {
    const descriptor = vi.mocked(api.submitComposerOperation).mock.calls[vi.mocked(api.submitComposerOperation).mock.calls.length - 1]?.[0];
    return descriptor ? [durableUser(descriptor.operationId), durableReply()] : [];
  });
  vi.mocked(api.fetchCompositionState).mockResolvedValue(makeCompositionState(2));
  vi.mocked(api.fetchCompositionProposals).mockResolvedValue([]);
  vi.mocked(api.fetchComposerPreferences).mockResolvedValue({ session_id: durableSession, trust_mode: "auto_commit", density_default: "high", interpretation_review_disabled: false, updated_at: "2026-10-06T00:00:00Z" });
  vi.mocked(api.fetchSessions).mockResolvedValue([]);
  vi.mocked(api.listBlobs).mockResolvedValue([]);
  useSessionStore.setState({ activeSessionId: durableSession, compositionStateLoaded: true, compositionState: makeCompositionState(1), messages: [] });
  return api;
}
async function provePendingOperation(options: { ambiguous?: boolean; rejectedProgress?: boolean; phase?: string; identicalOtherUser?: boolean } = {}) {
  const api = await armDurableTransport();
  const terminal = deferred<import("@/types/composerOperations").ComposerOperationSnapshot>();
  vi.mocked(api.fetchComposerOperation).mockReturnValueOnce(terminal.promise);
  if (options.ambiguous) vi.mocked(api.submitComposerOperation).mockRejectedValueOnce(new TypeError("Admission response lost"));
  if (options.rejectedProgress) vi.mocked(api.fetchComposerProgress).mockRejectedValue(new TypeError("Progress unavailable"));
  else vi.mocked(api.fetchComposerProgress).mockResolvedValue({ phase: options.phase ?? "complete", inflight_requests: 0 } as ComposerProgressSnapshot);
  const pending = useSessionStore.getState().sendMessage("hello");
  await vi.waitFor(() => expect(api.fetchComposerOperation).toHaveBeenCalledTimes(1));
  const descriptor = vi.mocked(api.submitComposerOperation).mock.calls[0][0];
  const local = useSessionStore.getState().messages[0];
  expect(descriptor.body).toEqual({ operation_id: descriptor.operationId, content: "hello", state_id: makeCompositionState(1).id });
  expect(local.operation_id).toBe(descriptor.operationId);
  expect(useSessionStore.getState().isComposing).toBe(true);
  await Promise.all([useSessionStore.getState().retryMessage(local.id), useSessionStore.getState().retryMessage(local.id)]);
  expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
  expect(api.recompose).not.toHaveBeenCalled();
  expect(api.fetchComposerProgress).not.toHaveBeenCalled();
  if (options.identicalOtherUser) {
    const other = { ...durableUser("55555555-5555-4555-8555-555555555555"), id: "other-user" };
    vi.mocked(api.fetchMessages).mockResolvedValue([other, durableUser(descriptor.operationId), durableReply()]);
  }
  terminal.resolve(durableCompleted(descriptor.operationId));
  await pending;
  expect(useSessionStore.getState().isComposing).toBe(false);
  const canonical = useSessionStore.getState().messages.find((row) => row.operation_id === descriptor.operationId)!;
  expect(canonical.id).toBe(durableUserId);
  expect(canonical.local_failure_code).toBeUndefined();
  expect(useSessionStore.getState().messages.some((row) => row.id === durableReplyId)).toBe(true);
  if (options.identicalOtherUser) expect(useSessionStore.getState().messages.some((row) => row.id === "other-user")).toBe(true);
  expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
}
async function proveDetachedOperation(kind: "compose_message" | "compose_recompose" = "compose_message") {
  const api = await armDurableTransport();
  const terminal = deferred<import("@/types/composerOperations").ComposerOperationSnapshot>();
  vi.mocked(api.fetchComposerOperation).mockReturnValueOnce(terminal.promise);
  const controller = new AbortController();
  if (kind === "compose_recompose") useSessionStore.setState({ messages: [{ ...durableUser("55555555-5555-4555-8555-555555555555"), local_status: "failed" }] });
  const pending = kind === "compose_message" ? useSessionStore.getState().sendMessage("hello", controller.signal) : useSessionStore.getState().retryMessage(durableUserId, controller.signal);
  await vi.waitFor(() => expect(api.fetchComposerOperation).toHaveBeenCalledTimes(1));
  const descriptor = vi.mocked(api.submitComposerOperation).mock.calls[0][0];
  controller.abort("compose_timeout");
  terminal.resolve(durableCompleted(descriptor.operationId, kind));
  await pending;
  expect(api.cancelComposerOperation).not.toHaveBeenCalled();
  expect(findComposerOperationCustody(durableScope, durableSession).foreground?.operationId).toBe(descriptor.operationId);
  expect(useSessionStore.getState().messages.some((row) => row.id === durableReplyId)).toBe(false);
  vi.mocked(api.fetchComposerOperation).mockResolvedValue(durableCompleted(descriptor.operationId, kind));
  await useSessionStore.getState().resumeComposerOperation(durableSession);
  await vi.waitFor(() => expect(useSessionStore.getState().isComposing).toBe(false));
  expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
  expect(useSessionStore.getState().messages.some((row) => row.id === durableReplyId)).toBe(true);
}
async function proveStoppedOperation(saved = true, kind: "compose_message" | "compose_recompose" = "compose_message") {
  const api = await armDurableTransport();
  const terminal = deferred<import("@/types/composerOperations").ComposerOperationSnapshot>();
  vi.mocked(api.fetchComposerOperation).mockReturnValueOnce(terminal.promise);
  vi.mocked(api.cancelComposerOperation).mockResolvedValue({ ...durableCompleted("55555555-5555-4555-8555-555555555555", kind), status: "running", result: null, cancel_requested: true });
  if (kind === "compose_recompose") useSessionStore.setState({ messages: [{ ...durableUser("55555555-5555-4555-8555-555555555555"), local_status: "failed" }] });
  const pending = kind === "compose_message" ? useSessionStore.getState().sendMessage("hello") : useSessionStore.getState().retryMessage(durableUserId);
  await vi.waitFor(() => expect(api.fetchComposerOperation).toHaveBeenCalledTimes(1));
  const descriptor = vi.mocked(api.submitComposerOperation).mock.calls[0][0];
  useSessionStore.getState().cancelComposition();
  await vi.waitFor(() => expect(api.cancelComposerOperation).toHaveBeenCalledWith(durableSession, descriptor.operationId));
  expect(useSessionStore.getState().isComposing).toBe(true);
  expect(findComposerOperationCustody(durableScope, durableSession).foreground?.operationId).toBe(descriptor.operationId);
  expect(api.fetchCompositionState).not.toHaveBeenCalled();
  vi.mocked(api.fetchMessages).mockResolvedValue([durableUser(descriptor.operationId)]);
  vi.mocked(api.fetchCompositionState).mockResolvedValue(makeCompositionState(saved ? 3 : 1));
  terminal.resolve({ ...durableCompleted(descriptor.operationId, kind), status: "failed", result: null, cancel_requested: true, error: { http_status: 409, failure_code: "request_cancelled", error_type: "request_cancelled", diagnostic_id: null, body: { error_type: "request_cancelled", detail: "Composer request was stopped." } } });
  await pending;
  expect(useSessionStore.getState().isComposing).toBe(false);
  expect(useSessionStore.getState().compositionState?.version).toBe(saved ? 3 : 1);
  expect(useSessionStore.getState().messages.some((row) => row.id === durableReplyId)).toBe(false);
  expect(findComposerOperationCustody(durableScope, durableSession).foreground).toBeNull();
  expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
}
async function proveRestoredOperation() {
  const api = await armDurableTransport();
  const operationId = "55555555-5555-4555-8555-555555555555";
  const terminal = deferred<import("@/types/composerOperations").ComposerOperationSnapshot>();
  acquireComposerOperationCustody({ mode: "submitted", scope: durableScope, sessionId: durableSession, operationId, kind: "compose_message", createdAt: Date.now(), body: { operation_id: operationId, content: "hello", state_id: makeCompositionState(1).id } });
  vi.mocked(api.fetchComposerOperation).mockReturnValueOnce(terminal.promise);
  vi.mocked(api.fetchMessages).mockResolvedValue([]);
  await useSessionStore.getState().selectSession(durableSession);
  await vi.waitFor(() => expect(api.fetchComposerOperation).toHaveBeenCalledWith(durableSession, operationId));
  expect(api.submitComposerOperation).not.toHaveBeenCalled();
  expect(useSessionStore.getState().isComposing).toBe(true);
  vi.mocked(api.fetchMessages).mockResolvedValue([durableUser(operationId), durableReply()]);
  terminal.resolve(durableCompleted(operationId));
  await vi.waitFor(() => expect(useSessionStore.getState().isComposing).toBe(false));
  expect(api.submitComposerOperation).not.toHaveBeenCalled();
  expect(useSessionStore.getState().messages.map((row) => row.id)).toEqual([durableUserId, durableReplyId]);
}
async function proveCanonicalRecompose() {
  const api = await armDurableTransport();
  useSessionStore.setState({ messages: [{ ...durableUser("55555555-5555-4555-8555-555555555555"), local_status: "failed" }] });
  vi.mocked(api.fetchComposerOperation).mockImplementation(async (_session, operationId) => durableCompleted(operationId, "compose_recompose"));
  await useSessionStore.getState().retryMessage(durableUserId);
  const descriptor = vi.mocked(api.submitComposerOperation).mock.calls[0][0];
  expect(descriptor.kind).toBe("compose_recompose");
  expect(descriptor.body).toEqual({ operation_id: descriptor.operationId, expected_user_message_id: durableUserId, state_id: makeCompositionState(1).id });
  expect(descriptor.operationId).not.toBe(durableUserId);
  expect(api.sendMessage).not.toHaveBeenCalled();
  expect(api.recompose).not.toHaveBeenCalled();
  await useSessionStore.getState().retryMessage(durableUserId);
  expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
}
async function proveGenerationFence(kind: "compose_message" | "compose_recompose" = "compose_message") {
  const api = await armDurableTransport();
  const oldRead = deferred<import("@/types/composerOperations").ComposerOperationSnapshot>();
  const currentRead = deferred<import("@/types/composerOperations").ComposerOperationSnapshot>();
  vi.mocked(api.fetchComposerOperation).mockReturnValueOnce(oldRead.promise).mockReturnValueOnce(currentRead.promise);
  if (kind === "compose_recompose") useSessionStore.setState({ messages: [{ ...durableUser("55555555-5555-4555-8555-555555555555"), local_status: "failed" }] });
  const pending = kind === "compose_message" ? useSessionStore.getState().sendMessage("hello") : useSessionStore.getState().retryMessage(durableUserId);
  await vi.waitFor(() => expect(api.fetchComposerOperation).toHaveBeenCalledTimes(1));
  const descriptor = vi.mocked(api.submitComposerOperation).mock.calls[0][0];
  vi.mocked(api.fetchMessages).mockResolvedValue([]);
  await useSessionStore.getState().selectSession("20000000-0000-4000-8000-000000000002");
  await useSessionStore.getState().selectSession(durableSession);
  await vi.waitFor(() => expect(api.fetchComposerOperation).toHaveBeenCalledTimes(2));
  vi.mocked(api.fetchMessages).mockResolvedValue([durableUser(descriptor.operationId), durableReply()]);
  currentRead.resolve(durableCompleted(descriptor.operationId, kind, 5));
  await vi.waitFor(() => expect(useSessionStore.getState().isComposing).toBe(false));
  const newest = useSessionStore.getState().messages;
  oldRead.resolve({ ...durableCompleted(descriptor.operationId, kind, 1), result: { message: { ...durableReply(), id: "stale-final" }, state: makeCompositionState(1), proposals: [] } });
  await pending;
  expect(useSessionStore.getState().messages).toEqual(newest);
  expect(useSessionStore.getState().messages.some((row) => row.id === "stale-final")).toBe(false);
  expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
}


async function proveImmutableAdmissionReplay() {
  vi.useFakeTimers();
  const api = await armDurableTransport();
  vi.mocked(api.submitComposerOperation).mockRejectedValueOnce(new TypeError("Lost POST response"));
  vi.mocked(api.fetchComposerOperation).mockResolvedValueOnce({ kind: "operation_missing" }).mockImplementation(async (_session, operationId) => durableCompleted(operationId));
  const pending = useSessionStore.getState().sendMessage("hello");
  await vi.waitFor(() => expect(api.fetchComposerOperation).toHaveBeenCalledTimes(1));
  const original = vi.mocked(api.submitComposerOperation).mock.calls[0][0];
  useSessionStore.setState({ compositionState: makeCompositionState(7) });
  await vi.advanceTimersByTimeAsync(1000);
  await pending;
  const replay = vi.mocked(api.submitComposerOperation).mock.calls[1][0];
  expect(replay).toEqual(original);
  expect(replay.body.state_id).toBe(makeCompositionState(1).id);
  expect(api.submitComposerOperation).toHaveBeenCalledTimes(2);
  expect(api.recompose).not.toHaveBeenCalled();
}
async function provePermanentRefusalReload(reason: "admission_refused" | "accounting_unavailable") {
  const api = await armDurableTransport();
  const operationId = "55555555-5555-4555-8555-555555555555";
  const code = reason === "accounting_unavailable" ? "token_accounting_unavailable" : reason;
  acquireComposerOperationCustody({ mode: "submitted", scope: durableScope, sessionId: durableSession, operationId, kind: "compose_message", createdAt: Date.now(), body: { operation_id: operationId, content: "hello", state_id: makeCompositionState(1).id } });
  vi.mocked(api.fetchMessages).mockResolvedValue([durableUser(operationId)]);
  vi.mocked(api.fetchComposerOperation).mockResolvedValue({ ...durableCompleted(operationId), status: "failed", result: null, error: { http_status: 422, failure_code: "http_error", error_type: "composer_admission_refused", diagnostic_id: null, body: { error_type: "composer_admission_refused", failure_code: code, detail: "Admission permanently refused." } } });
  await useSessionStore.getState().selectSession(durableSession);
  await vi.waitFor(() => expect(useSessionStore.getState().messages[0].local_failure_code).toBe(code));
  await useSessionStore.getState().retryMessage(durableUserId);
  expect(api.submitComposerOperation).not.toHaveBeenCalled();
  expect(api.recompose).not.toHaveBeenCalled();
  expect(useSessionStore.getState().error).not.toMatch(/retry/i);
}
async function proveSameOwnerHydrationRace(outcome: "failed" | "fulfilled") {
  const api = await armDurableTransport();
  const staleState = deferred<CompositionState | null>();
  vi.mocked(api.fetchComposerOperation).mockImplementation(async (_session, operationId) => durableCompleted(operationId));
  vi.mocked(api.fetchMessages).mockResolvedValue([]);
  vi.mocked(api.fetchCompositionState).mockReturnValueOnce(staleState.promise);
  const pending = useSessionStore.getState().sendMessage("hello");
  await vi.waitFor(() => expect(api.fetchCompositionState).toHaveBeenCalled());
  const descriptor = vi.mocked(api.submitComposerOperation).mock.calls[0][0];
  const newest = [durableUser(descriptor.operationId), durableReply(), { ...durableUser("66666666-6666-4666-8666-666666666666"), id: "later-user", content: "Next request" }];
  useSessionStore.setState({ messages: newest, compositionState: makeCompositionState(5) });
  if (outcome === "failed") staleState.reject(new TypeError("Late reload failed"));
  else staleState.resolve(makeCompositionState(1));
  await pending;
  expect(useSessionStore.getState().messages).toEqual(newest);
  expect(useSessionStore.getState().compositionState?.version).toBe(5);
}


describe("sessionStore", () => {
  afterEach(async () => {
    const { detachComposerObservers } = await import("@/api/composerOperationObserver");
    detachComposerObservers();
    vi.useRealTimers();
  });
  beforeEach(async () => {
    vi.resetAllMocks();
    window.sessionStorage.clear();
    purgeComposerCustody();
    authenticateComposerCustody({ principalId: "test-user", authProvider: "local" });
    clearAllSessionOperationRetries();
    useSessionStore.getState().reset();
    resetStore(useSessionStore);
    useSessionStore.setState({ compositionStateLoaded: true });
    // Keep preferences isolated between store tests.
    const { usePreferencesStore } = await import("@/stores/preferencesStore");
    resetStore(usePreferencesStore);
    usePreferencesStore.setState({
      loaded: true,
      writing: false,
    });
    // Reseed the API mock cleared by vi.resetAllMocks().
    const apiMod = await import("@/api/client");
    vi.mocked(apiMod.fetchCurrentUser).mockResolvedValue({ user_id: "test-user", username: "test-user", display_name: "Test User", email: null, groups: [], dev_admin: false });
    vi.mocked(apiMod.fetchAuthConfig).mockResolvedValue({ provider: "local", registration_mode: "closed", sso_start_url: null });
    vi.mocked(apiMod.fetchMessages).mockRejectedValue(new Error("Terminal transcript reload unavailable"));
    vi.mocked(apiMod.fetchCompositionState).mockRejectedValue(new Error("Terminal state reload unavailable"));
    vi.mocked(apiMod.fetchCompositionProposals).mockRejectedValue(new Error("Terminal proposals reload unavailable"));
    installComposeTurnDouble({
      sendMessage: vi.mocked(apiMod.sendMessage), recompose: vi.mocked(apiMod.recompose),
      submitComposerOperation: vi.mocked(apiMod.submitComposerOperation), fetchComposerOperation: vi.mocked(apiMod.fetchComposerOperation),
      fetchComposerOperationStream: vi.mocked(apiMod.fetchComposerOperationStream), cancelComposerOperation: vi.mocked(apiMod.cancelComposerOperation),
    });
    vi.mocked(apiMod.fetchComposerProgress).mockResolvedValue({
      phase: "idle", inflight_requests: 0,
    } as ComposerProgressSnapshot);
    (apiMod.fetchUserComposerPreferences as ReturnType<typeof vi.fn>).mockResolvedValue({
      tutorial_completed_at: null,
      tutorial_stage: null,
      tutorial_session_id: null,
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
      updated_at: "2026-05-15T00:00:00Z",
    });
    // Phase 5b — sessionStore.selectSession fires a fire-and-forget
    // refreshAll on the interpretationEventsStore which awaits this
    // method.  vi.resetAllMocks() wiped the at-mock-declaration default,
    // so reseed it here; an unmocked or undefined-returning fn surfaces
    // as an unhandled rejection in the per-session refresh path.
    (apiMod.listInterpretationEvents as ReturnType<typeof vi.fn>).mockResolvedValue([]);
  });

  describe("initial state", () => {
    it("starts with empty sessions and no active session", () => {
      resetStore(useSessionStore);
      const state = useSessionStore.getState();
      expect(state.sessions).toEqual([]);
      expect(state.activeSessionId).toBeNull();
      expect(state.messages).toEqual([]);
      expect(state.compositionState).toBeNull();
      expect(state.isComposing).toBe(false);
      expect(state.error).toBeNull();
      // Loaded-ness flags: an unfetched list must be distinguishable from a
      // genuinely empty account / pipeline (auto-resume, empty landing, and
      // the #/{id}/yaml gate all depend on this).
      expect(state.sessionsLoaded).toBe(false);
      expect(state.compositionStateLoaded).toBe(false);
    });
  });

  describe("freeform send identity", () => {
    function recoveryUser(overrides: Partial<ChatMessage> = {}): ChatMessage {
      return {
        id: "30000000-0000-4000-8000-000000000001", session_id: "20000000-0000-4000-8000-000000000001", role: "user", content: "hello",
        tool_calls: null, created_at: "2026-10-05T00:00:00Z", ...overrides,
      };
    }

    async function armRecoveryReads(rows: ChatMessage[]) {
      const api = await import("@/api/client");
      vi.mocked(api.fetchMessages).mockResolvedValue(rows);
      vi.mocked(api.fetchCompositionState).mockResolvedValue(makeCompositionState(2));
      vi.mocked(api.fetchCompositionProposals).mockResolvedValue([]);
      vi.mocked(api.fetchComposerPreferences).mockResolvedValue({
        session_id: "20000000-0000-4000-8000-000000000001", trust_mode: "auto_commit", density_default: "high",
        interpretation_review_disabled: false, updated_at: "2026-10-05T00:00:00Z",
      });
      return api;
    }

    it.each([504, 524])("retrieves a saved final result after an unstructured HTTP %s without resending", async (status) => {
      const user = recoveryUser();
      const api = await armRecoveryReads([user, recoveryUser({
        id: "final-answer", role: "assistant", content: "Done",
      })]);
      vi.mocked(api.sendMessage).mockImplementationOnce(async (_session, _content, requestId) => {
        user.operation_id = requestId;
        throw { status, detail: "Gateway timeout" };
      });
      vi.mocked(api.fetchComposerProgress).mockResolvedValue({
        phase: "complete", inflight_requests: 0,
      } as ComposerProgressSnapshot);
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001", compositionState: makeCompositionState(1) });
      await useSessionStore.getState().sendMessage("hello");
      expect(useSessionStore.getState().messages.map((row) => row.id)).toEqual(["30000000-0000-4000-8000-000000000001", "final-answer"]);
      expect(useSessionStore.getState().messages[0].local_status).toBeUndefined();
      expect(useSessionStore.getState().compositionState?.version).toBe(2);
      await useSessionStore.getState().retryMessage("30000000-0000-4000-8000-000000000001");
      expect(api.sendMessage).toHaveBeenCalledTimes(1);
      expect(api.recompose).not.toHaveBeenCalled();
    });

    it.each([504, 524])("keeps a named application HTTP %s failure authoritative", async (status) => {
      const api = await import("@/api/client");
      vi.mocked(api.sendMessage).mockRejectedValueOnce({
        status, error_type: "application_refusal", failure_code: "admission_refused", detail: "Admission refused",
      });
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001" });
      await useSessionStore.getState().sendMessage("hello");
      expect(api.fetchMessages).toHaveBeenCalled();
      expect(api.fetchCompositionState).toHaveBeenCalled();
      const failed = useSessionStore.getState().messages[0];
      expect(failed.local_failure_code).toBe("admission_refused");
      await useSessionStore.getState().retryMessage(failed.id);
      expect(api.sendMessage).toHaveBeenCalledTimes(1);
    });

    it.each(["calling_model", "using_tools", "complete"] as const)(
      "keeps response-lost %s work read-only until final settlement and then retrieves it", async (phase) => {
      // Durable replacement: the exact operation terminal owns settlement.
      await provePendingOperation({ ambiguous: true, phase });
    },
    );

    it("treats complete progress without its final transcript as a mixed snapshot", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await provePendingOperation({ rejectedProgress: true });
    });

    it.each(["rejected", "missing_count"] as const)("keeps canonical retry read-only when progress is %s", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await provePendingOperation({ rejectedProgress: true });
    });

    it("serializes repeated refresh clicks and permits a separate explicit new response only after settlement", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveCanonicalRecompose();
    });

    it.each(["provider", "tool_prefix", "blank_final"] as const)("restores saved %s outcome on reload without starting work", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveRestoredOperation();
    });

    it("observes an admitted request before its user row exists, then retrieves the final reply", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await provePendingOperation({ ambiguous: true });
    });

    it("drops delayed recovery reads after an activation change", async () => {
      const user = recoveryUser({ local_status: "failed", local_failure_code: "compose_outcome_unconfirmed" });
      const api = await armRecoveryReads([user]);
      const read = deferred<ChatMessage[]>();
      vi.mocked(api.fetchMessages).mockReturnValueOnce(read.promise).mockResolvedValue([]);
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001", messages: [user] });
      const pending = useSessionStore.getState().retryMessage(user.id);
      await useSessionStore.getState().selectSession("20000000-0000-4000-8000-000000000002");
      read.resolve([user, recoveryUser({ id: "old-final", role: "assistant", content: "Done" })]);
      await pending;
      expect(useSessionStore.getState().activeSessionId).toBe("20000000-0000-4000-8000-000000000002");
      expect(useSessionStore.getState().messages).toEqual([]);
      expect(api.recompose).not.toHaveBeenCalled();
    });

    it("reconciles a saved proposal 409 and makes subsequent clicks read-only", async () => {
      const user = recoveryUser({ local_status: "failed" });
      const api = await armRecoveryReads([user]);
      vi.mocked(api.fetchCompositionProposals).mockResolvedValue([makeCompositionProposal({
        status: "pending", pipeline_metadata: {
          draft_hash: "draft", base: {}, repair_count: 0, skill_hash: "skill",
          audit_payload_hash: "audit", custody_result: "ready",
        },
      })]);
      vi.mocked(api.recompose).mockRejectedValueOnce({
        status: 409, error_type: "recompose_saved_proposal", detail: "Review the saved proposal",
      });
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001", messages: [user] });
      await useSessionStore.getState().retryMessage(user.id);
      expect(useSessionStore.getState().compositionProposals[0].status).toBe("pending");
      expect(useSessionStore.getState().messages[0].local_status).toBe("failed");
      await useSessionStore.getState().retryMessage(user.id);
      expect(api.recompose).toHaveBeenCalledTimes(1);
      expect(api.sendMessage).not.toHaveBeenCalled();
    });

    it("keeps an aborted turn refresh-only when settlement cannot be read", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveDetachedOperation();
    });

    it("keeps observing after a failed recovery GET and retrieves the final outcome automatically", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await provePendingOperation({ ambiguous: true });
    });

    it("retires the saved-proposal retry guard after authoritative rejection and settlement", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveCanonicalRecompose();
    });

    it.each(["failed", "fulfilled"] as const)("retains newer same-owner messages over an older %s recovery snapshot", async (outcome) => {
      await proveSameOwnerHydrationRace(outcome);
    });

    it("retires timed recovery after navigation without publishing the previous session", async () => {
      vi.useFakeTimers();
      try {
        const user = recoveryUser({ local_status: "failed", local_failure_code: "compose_outcome_unconfirmed" });
        const api = await armRecoveryReads([user]);
        vi.mocked(api.fetchComposerProgress).mockResolvedValue({ phase: "using_tools", inflight_requests: 1 } as ComposerProgressSnapshot);
        useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001", messages: [user] });
        await useSessionStore.getState().retryMessage(user.id);
        vi.mocked(api.fetchMessages).mockResolvedValue([]);
        vi.mocked(api.fetchComposerProgress).mockResolvedValue({ phase: "idle", inflight_requests: 0 } as ComposerProgressSnapshot);
        await useSessionStore.getState().selectSession("20000000-0000-4000-8000-000000000002");
        const reads = vi.mocked(api.fetchMessages).mock.calls.length;
        await vi.advanceTimersByTimeAsync(4500);
        expect(api.fetchMessages).toHaveBeenCalledTimes(reads);
        expect(useSessionStore.getState().activeSessionId).toBe("20000000-0000-4000-8000-000000000002");
        expect(useSessionStore.getState().messages).toEqual([]);
      } finally {
        useSessionStore.getState().reset();
        vi.useRealTimers();
      }
    });

    it.each(["admission_refused", "accounting_unavailable"] as const)("preserves permanent %s refusal discovered on reload", async (reason) => {
      await provePermanentRefusalReload(reason);
    });

    it("retains refresh-only retry when polling canonicalizes the user before an accepted 409", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await provePendingOperation({ ambiguous: true });
    });

    it("does not let a progress tick invalidate a slow accepted-receipt snapshot", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await provePendingOperation({ ambiguous: true });
    });

    it("keeps a canonical accepted message refresh-only when ambiguous transport reconciliation cannot load state", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await provePendingOperation({ ambiguous: true });
    });

    it("attaches provider failure to a canonical user published before the POST fails", async () => {
      const api = await import("@/api/client");
      const send = deferred<{ message: ChatMessage; state: null; proposals: CompositionProposal[] }>();
      vi.mocked(api.sendMessage).mockReturnValueOnce(send.promise);
      vi.mocked(api.fetchCompositionState).mockResolvedValue(null);
      vi.mocked(api.fetchCompositionProposals).mockResolvedValue([]);
      useSessionStore.setState({activeSessionId: "20000000-0000-4000-8000-000000000001"});
      const pending = useSessionStore.getState().sendMessage("hello");
      const requestId = useSessionStore.getState().messages[0].operation_id;
      vi.mocked(api.fetchMessages).mockResolvedValue([{
        id: "30000000-0000-4000-8000-000000000002", session_id: "20000000-0000-4000-8000-000000000001", role: "user", content: "hello",
        operation_id: requestId, tool_calls: null, created_at: "2026-09-27T00:00:00Z",
      } satisfies ChatMessage]);
      await useSessionStore.getState().loadInflightMessages("20000000-0000-4000-8000-000000000001");
      send.reject({status: 502, error_type: "llm_unavailable", detail: "Provider unavailable"});
      await pending;
      const canonical = useSessionStore.getState().messages[0];
      expect(canonical.id).toBe("30000000-0000-4000-8000-000000000002");
      expect(canonical.local_status).toBe("failed");
      expect(canonical.local_error).toContain("temporarily unavailable");
    });

    it("reuses the original UUID, content, and requested state when an uncertain local send is retried", async () => {
      await proveImmutableAdmissionReplay();
    });

    it("keeps an accepted receipt for refresh-only retry when reconciliation fails", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await provePendingOperation({ ambiguous: true });
    });

    it("offers a deliberate canonical recompose after acceptance with no assistant reply", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveCanonicalRecompose();
    });

    it("does not match a local intent to another user's identical text", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await provePendingOperation({ ambiguous: true, identicalOtherUser: true });
    });

    it("names the canonical user message in a deliberate recompose retry", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveCanonicalRecompose();
    });

    it("marks a conflicting reuse as terminal instead of offering an impossible retry", async () => {
      const apiMod = await import("@/api/client");
      vi.mocked(apiMod.submitComposerOperation).mockRejectedValueOnce({
        status: 409, error_type: "composer_operation_conflict", detail: "This operation id was already used for a different request.",
      });
      vi.mocked(apiMod.fetchMessages).mockResolvedValue([]);
      vi.mocked(apiMod.fetchCompositionState).mockResolvedValue(null);
      vi.mocked(apiMod.fetchCompositionProposals).mockResolvedValue([]);
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001" });
      await useSessionStore.getState().sendMessage("hello");
      const message = useSessionStore.getState().messages[0];
      expect(message.local_failure_code).toBe("composer_operation_conflict");
      expect(message.local_status).toBe("failed");
      expect(useSessionStore.getState().compositionStateLoaded).toBe(true);
      await useSessionStore.getState().retryMessage(message.id);
      expect(apiMod.submitComposerOperation).toHaveBeenCalledTimes(1);
      expect(apiMod.sendMessage).not.toHaveBeenCalled();
      expect(apiMod.recompose).not.toHaveBeenCalled();
    });

    it("does not automatically replay a pending send after a page reload", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveRestoredOperation();
    });
  });

  describe("durable authoring readiness", () => {
    it("starts with authoring gated until the selected session head is known", async () => { resetStore(useSessionStore); expect(useSessionStore.getState().compositionStateLoaded).toBe(false); });

    it("admits authoring only after the selected session head loads", async () => { const api = await armDurableTransport(); useSessionStore.setState({ compositionStateLoaded: false }); await useSessionStore.getState().sendMessage("hello"); expect(api.submitComposerOperation).not.toHaveBeenCalled(); useSessionStore.setState({ compositionStateLoaded: true }); vi.mocked(api.fetchComposerOperation).mockImplementation(async (_session, operationId) => durableCompleted(operationId)); await useSessionStore.getState().sendMessage("hello"); expect(api.submitComposerOperation).toHaveBeenCalledTimes(1); });

    it("observes the server deadline without a socket budget bootstrap gate", async () => { const api = await armDurableTransport(); vi.mocked(api.fetchComposerOperation).mockImplementation(async (_session, operationId) => durableCompleted(operationId)); await useSessionStore.getState().sendMessage("hello"); expect(api.submitComposerOperation).toHaveBeenCalledTimes(1); expect(api.fetchComposerProgress).not.toHaveBeenCalled(); expect(useSessionStore.getState().isComposing).toBe(false); });
  });

  describe("loaded-ness flags", () => {
    it("loadSessions marks sessionsLoaded on success", async () => {
      const apiMod = await import("@/api/client");
      (apiMod.fetchSessions as ReturnType<typeof vi.fn>).mockResolvedValue([]);

      await useSessionStore.getState().loadSessions();

      expect(useSessionStore.getState().sessionsLoaded).toBe(true);
    });

    it("loadSessions leaves sessionsLoaded false on failure", async () => {
      const apiMod = await import("@/api/client");
      (apiMod.fetchSessions as ReturnType<typeof vi.fn>).mockRejectedValue(
        new Error("network"),
      );

      await useSessionStore.getState().loadSessions();

      expect(useSessionStore.getState().sessionsLoaded).toBe(false);
      expect(useSessionStore.getState().error).toMatch(/failed to load sessions/i);
    });

    it("ignores a stale response that resolves after a subsequent create+rename replaced the list (elspeth-4d5b0e634a)", async () => {
      // Regression pin for the tutorial session-list race: an app-start
      // loadSessions can still be in flight when the user clicks "Let's go"
      // (createSession + renameSession). If the fetch resolves afterwards
      // with its pre-rename snapshot, it must not clobber the rename.
      const apiMod = await import("@/api/client");
      let resolveFetch!: (sessions: unknown[]) => void;
      (apiMod.fetchSessions as ReturnType<typeof vi.fn>).mockReturnValue(
        new Promise((resolve) => {
          resolveFetch = resolve;
        }),
      );

      // App-start loadSessions fires and is still in flight...
      const loading = useSessionStore.getState().loadSessions();

      // ...when the tutorial's "Let's go" creates and renames a session —
      // the exact sequence HelloWorldTutorial.onStart runs.
      (apiMod.createSession as ReturnType<typeof vi.fn>).mockResolvedValue({
        id: "tutorial-session",
        title: "Session — 10 Jul 2026",
        created_at: "2026-07-10T00:00:00Z",
        updated_at: "2026-07-10T00:00:00Z",
      });
      await useSessionStore.getState().createSession();

      const renamed = {
        id: "tutorial-session",
        title: "First-run tutorial (in progress)",
        created_at: "2026-07-10T00:00:00Z",
        updated_at: "2026-07-10T00:00:01Z",
      };
      (apiMod.renameSession as ReturnType<typeof vi.fn>).mockResolvedValue(renamed);
      await useSessionStore
        .getState()
        .renameSession("tutorial-session", "First-run tutorial (in progress)");

      const sessionsAfterMutation = useSessionStore.getState().sessions;
      expect(sessionsAfterMutation[0]).toEqual(renamed);

      // NOW the stale app-start fetch resolves with its pre-rename (empty)
      // snapshot.
      resolveFetch([]);
      await loading;

      // The rename must survive — the stale snapshot did not clobber it.
      expect(useSessionStore.getState().sessions).toBe(sessionsAfterMutation);
      expect(useSessionStore.getState().sessions[0]).toEqual(renamed);
      // The fetch itself still succeeded, so the loaded-ness flag is honest
      // to flip even though its (stale) snapshot was discarded.
      expect(useSessionStore.getState().sessionsLoaded).toBe(true);
    });

    it("selectSession clears compositionStateLoaded while fetching, sets it once settled", async () => {
      const apiMod = await import("@/api/client");
      let resolveState!: (value: null) => void;
      (apiMod.fetchMessages as ReturnType<typeof vi.fn>).mockResolvedValue([]);
      (apiMod.fetchCompositionState as ReturnType<typeof vi.fn>).mockReturnValue(
        new Promise((resolve) => {
          resolveState = resolve;
        }),
      );
      (
        apiMod.fetchCompositionProposals as ReturnType<typeof vi.fn>
      ).mockResolvedValue([]);
      (
        apiMod.fetchComposerPreferences as ReturnType<typeof vi.fn>
      ).mockResolvedValue(null);
      useSessionStore.setState({ compositionStateLoaded: true } as never);

      const selecting = useSessionStore.getState().selectSession("sess-1");
      expect(useSessionStore.getState().compositionStateLoaded).toBe(false);

      resolveState(null);
      await selecting;

      // Loaded-and-empty is a KNOWN state, distinct from still-fetching.
      expect(useSessionStore.getState().compositionStateLoaded).toBe(true);
      expect(useSessionStore.getState().compositionState).toBeNull();
    });

    it("createSession marks the fresh session's composition as known-empty", async () => {
      const apiMod = await import("@/api/client");
      (apiMod.createSession as ReturnType<typeof vi.fn>).mockResolvedValue({
        id: "20000000-0000-4000-8000-000000000010",
        title: "Session — 2 Jul 2026",
        created_at: "2026-07-02T00:00:00Z",
        updated_at: "2026-07-02T00:00:00Z",
      });

      await useSessionStore.getState().createSession();

      expect(useSessionStore.getState().activeSessionId).toBe("20000000-0000-4000-8000-000000000010");
      expect(useSessionStore.getState().compositionStateLoaded).toBe(true);
      expect(useSessionStore.getState().compositionState).toBeNull();
    });

    it.each(["success", "failure"])("createSession resets active compose state before an old send %s settles", async (outcome) => {
      const api = await import("@/api/client");
      const post = deferred<{ message: ChatMessage; state: CompositionState; proposals: CompositionProposal[] }>();
      vi.mocked(api.sendMessage).mockReturnValueOnce(post.promise);
      vi.mocked(api.createSession).mockResolvedValue({
        id: "20000000-0000-4000-8000-000000000007", title: "New session", created_at: "2026-10-02T00:00:00Z", updated_at: "2026-10-02T00:00:00Z",
      });
      vi.mocked(api.fetchComposerProgress).mockResolvedValue({ phase: "idle", inflight_requests: 0 } as ComposerProgressSnapshot);
      useSessionStore.setState({
        activeSessionId: "20000000-0000-4000-8000-000000000006", lastComposeChangedPipeline: true,
        errorDetails: ["Old validation issue"], isLoadingVersions: true,
      });
      const pending = useSessionStore.getState().sendMessage("Assess old data");
      expect(api.sendMessage).toHaveBeenCalledTimes(1);
      expect(useSessionStore.getState().isComposing).toBe(true);
      await useSessionStore.getState().createSession();
      const activated = useSessionStore.getState();
      if (outcome === "success") {
        post.resolve({
          message: { id: "old-reply", session_id: "20000000-0000-4000-8000-000000000006", role: "assistant", content: "Old reply", tool_calls: null, created_at: "2026-10-02T00:00:01Z" },
          state: makeCompositionState(2), proposals: [],
        });
      } else {
        post.reject({ status: 502, detail: "Old provider failure" });
      }
      await pending;
      expect(activated.isComposing).toBe(false);
      expect(activated.lastComposeChangedPipeline).toBeNull();
      expect(activated.errorDetails).toBeNull();
      expect(activated.isLoadingVersions).toBe(false);
      expect(useSessionStore.getState()).toMatchObject({
        activeSessionId: "20000000-0000-4000-8000-000000000007", messages: [], compositionState: null,
        compositionProposals: [], composerProgress: null, isComposing: false,
        error: null, errorDetails: null, lastComposeChangedPipeline: null,
      });
    });

    it("createSession requests authoring focus so a collapsed pane cannot hide a new session's composer", async () => {
      // The collapsed-pane preference persists globally (localStorage), so
      // without this a user who collapsed the pane once would find EVERY new
      // session opening with the chat — the primary composing surface —
      // hidden (2026-08-15 UX review). The request is a STORE FLAG, not a
      // window event: createSession can run while ComposerWorkspace is
      // unmounted (empty landing, tutorial graduation), where an event
      // would land on zero listeners. ComposerWorkspace consumes the flag
      // on mount or change (its truth-test lives beside that consumer).
      // Session SWITCHES deliberately do not set it, so the standing
      // preference still applies when revisiting existing sessions.
      const apiMod = await import("@/api/client");
      (apiMod.createSession as ReturnType<typeof vi.fn>).mockResolvedValue({
        id: "new-2",
        title: "Session — 2 Jul 2026",
        created_at: "2026-07-02T00:00:00Z",
        updated_at: "2026-07-02T00:00:00Z",
      });

      await useSessionStore.getState().createSession();

      expect(useSessionStore.getState().authoringFocusRequested).toBe(true);
    });

    it("createSession does not request authoring focus when session creation fails", async () => {
      const apiMod = await import("@/api/client");
      (apiMod.createSession as ReturnType<typeof vi.fn>).mockRejectedValue(
        new Error("boom"),
      );

      await useSessionStore.getState().createSession();

      expect(useSessionStore.getState().authoringFocusRequested).toBe(false);
    });

    it("createSession transfers blob ownership before the new session can upload", async () => {
      const apiMod = await import("@/api/client");
      const blobA = {
        id: "blob-a",
        session_id: "20000000-0000-4000-8000-000000000008",
        filename: "a.csv",
        mime_type: "text/csv",
        size_bytes: 1,
        content_hash: "a".repeat(64),
        created_at: "2026-07-26T00:00:00Z",
        created_by: "user",
        source_description: null,
        status: "ready",
        creation_modality: "verbatim",
        created_from_message_id: null,
        creating_model_identifier: null,
        creating_model_version: null,
        creating_provider: null,
        creating_composer_skill_hash: null,
        creating_arguments_hash: null,
      } satisfies BlobMetadata;
      const blobB = {
        ...blobA,
        id: "blob-b",
        session_id: "20000000-0000-4000-8000-000000000009",
        filename: "b.csv",
        content_hash: "b".repeat(64),
      } satisfies BlobMetadata;
      useBlobStore.getState().reset();
      (apiMod.listBlobs as ReturnType<typeof vi.fn>).mockResolvedValue([blobA]);
      await useBlobStore.getState().loadBlobs("20000000-0000-4000-8000-000000000008");
      (apiMod.createSession as ReturnType<typeof vi.fn>).mockResolvedValue({
        id: "20000000-0000-4000-8000-000000000009",
        title: "Session B",
        created_at: "2026-07-26T00:00:00Z",
        updated_at: "2026-07-26T00:00:00Z",
      });

      await useSessionStore.getState().createSession();

      expect(useSessionStore.getState().activeSessionId).toBe("20000000-0000-4000-8000-000000000009");
      expect(useBlobStore.getState().blobs).toEqual([]);

      (apiMod.uploadBlob as ReturnType<typeof vi.fn>).mockResolvedValue(blobB);
      const result = await useBlobStore
        .getState()
        .uploadBlob("20000000-0000-4000-8000-000000000009", new File(["b"], "b.csv"));

      expect(result).toEqual(blobB);
      expect(useBlobStore.getState().blobs).toEqual([blobB]);
    });
  });

  describe("sendMessage optimistic insert", () => {
    it("appends optimistic user message and sets composing", async () => {
      // Pre-condition: set an active session so sendMessage proceeds
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001" });

      // Start the send — it will await the mocked API call (which
      // returns undefined by default, causing the catch branch).
      // We only care about the intermediate optimistic state here.
      const sendPromise = useSessionStore.getState().sendMessage("hello");

      // After the synchronous part of sendMessage runs, check state
      const state = useSessionStore.getState();
      expect(state.isComposing).toBe(true);
      expect(state.messages).toHaveLength(1);
      expect(state.messages[0].role).toBe("user");
      expect(state.messages[0].content).toBe("hello");
      expect(state.messages[0].local_status).toBe("pending");

      // Let the promise settle (will hit error path since mock returns undefined)
      await sendPromise;
    });

    it("marks message as failed when API call throws", async () => {
      const { sendMessage: mockSendMessage } = await import("@/api/client");
      (mockSendMessage as ReturnType<typeof vi.fn>).mockRejectedValueOnce({
        status: 500,
        detail: "Server error",
      });

      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001" });
      await useSessionStore.getState().sendMessage("hello");

      const state = useSessionStore.getState();
      expect(state.isComposing).toBe(false);
      expect(state.error).toBe("Server error");
      expect(state.messages[0].local_status).toBe("failed");
      expect(state.messages[0].local_error).toBe("Server error");
    });

    it("clears local_status on successful response", async () => {
      const { sendMessage: mockSendMessage } = await import("@/api/client");
      (mockSendMessage as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
        message: {
          id: "asst-1",
          session_id: "20000000-0000-4000-8000-000000000001",
          role: "assistant",
          content: "Hello back",
          tool_calls: null,
          created_at: new Date().toISOString(),
        },
        state: null,
      });

      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001" });
      await useSessionStore.getState().sendMessage("hello");

      const state = useSessionStore.getState();
      expect(state.isComposing).toBe(false);
      // User message should have local_status cleared
      const userMsg = state.messages.find((m) => m.role === "user");
      expect(userMsg?.local_status).toBeUndefined();
      // Assistant message should be appended
      const asstMsg = state.messages.find((m) => m.role === "assistant");
      expect(asstMsg?.content).toBe("Hello back");
    });

    it("records whether the turn mutated the pipeline (elspeth-bf9c296ee5)", async () => {
      // The terminal completion badge derives "Response ready" vs "Pipeline
      // updated" from this flag — a discarded versionChanged means the badge
      // can only ever guess.
      const { sendMessage: mockSendMessage, fetchMessages, fetchCompositionState, fetchCompositionProposals } = await import("@/api/client");
      vi.mocked(fetchMessages).mockResolvedValue([]);
      vi.mocked(fetchCompositionState).mockResolvedValue(makeCompositionState(2));
      vi.mocked(fetchCompositionProposals).mockResolvedValue([]);

      // Turn 1: composer returns a NEW state version → mutated.
      (mockSendMessage as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
        message: {
          id: "asst-1",
          session_id: "20000000-0000-4000-8000-000000000001",
          role: "assistant",
          content: "Added the source.",
          tool_calls: null,
          created_at: new Date().toISOString(),
        },
        state: makeCompositionState(2),
      });
      useSessionStore.setState({
        activeSessionId: "20000000-0000-4000-8000-000000000001",
        compositionState: makeCompositionState(1),
      });

      // While the turn is in flight the flag must read null (unknown), not a
      // stale verdict from the previous turn.
      useSessionStore.setState({ lastComposeChangedPipeline: false });
      const sendPromise = useSessionStore.getState().sendMessage("add a source");
      expect(useSessionStore.getState().lastComposeChangedPipeline).toBeNull();
      await sendPromise;
      expect(useSessionStore.getState().lastComposeChangedPipeline).toBe(true);
      expect(useSessionStore.getState().compositionStateLoaded).toBe(true);

      // Turn 2: answer-only response (state: null) → not mutated.
      (mockSendMessage as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
        message: {
          id: "asst-2",
          session_id: "20000000-0000-4000-8000-000000000001",
          role: "assistant",
          content: "That plugin reads CSV files.",
          tool_calls: null,
          created_at: new Date().toISOString(),
        },
        state: null,
      });
      await useSessionStore.getState().sendMessage("what does csv do?");
      expect(useSessionStore.getState().lastComposeChangedPipeline).toBe(false);

      // Turn 3: composer echoes the SAME version → not mutated.
      (mockSendMessage as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
        message: {
          id: "asst-3",
          session_id: "20000000-0000-4000-8000-000000000001",
          role: "assistant",
          content: "No changes needed.",
          tool_calls: null,
          created_at: new Date().toISOString(),
        },
        state: makeCompositionState(2),
      });
      await useSessionStore.getState().sendMessage("looks fine?");
      expect(useSessionStore.getState().lastComposeChangedPipeline).toBe(false);
    });

    it("refreshes pending interpretation events after a successful freeform compose turn", async () => {
      // Regression: a freeform compose turn can create new pending
      // interpretation events (invented_source / llm_prompt_template /
      // llm_model_choice / pipeline_decision). The compose path must pull them into the
      // interpretationEventsStore — so the inline review widgets and their
      // sign-off buttons never rendered mid-session, while the run-gate still
      // blocked execution on the pending rows. selectSession refreshes on
      // reload, which previously masked the gap.
      resetStore(useInterpretationEventsStore);
      const apiMod = await import("@/api/client");
      (apiMod.sendMessage as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
        message: {
          id: "asst-1",
          session_id: "20000000-0000-4000-8000-000000000001",
          role: "assistant",
          content: "Drafted.",
          tool_calls: null,
          created_at: new Date().toISOString(),
        },
        state: null,
      });
      (apiMod.listInterpretationEvents as ReturnType<typeof vi.fn>).mockResolvedValue([
        makePendingInterpretationEvent("evt-1"),
      ]);

      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001" });
      await useSessionStore.getState().sendMessage("rate these pages");

      // refreshAll is fire-and-forget inside sendMessage; await the microtask.
      await vi.waitFor(() => {
        const map =
          useInterpretationEventsStore.getState().pendingBySession["20000000-0000-4000-8000-000000000001"];
        expect(map?.["evt-1"]).toBeDefined();
      });
    });

    it("refreshes pending interpretation events after a successful recompose (retry)", async () => {
      // Same bug class as the freeform sendMessage path: recompose can mint new
      // interpretive decisions, and without a refresh the inline review widgets
      // never surface mid-session.
      resetStore(useInterpretationEventsStore);
      const apiMod = await import("@/api/client");
      (apiMod.recompose as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
        message: {
          id: "asst-r",
          session_id: "20000000-0000-4000-8000-000000000001",
          role: "assistant",
          content: "Redone.",
          tool_calls: null,
          created_at: new Date().toISOString(),
        },
        state: null,
      });
      (apiMod.listInterpretationEvents as ReturnType<typeof vi.fn>).mockResolvedValue([
        makePendingInterpretationEvent("evt-recompose"),
      ]);

      const userMessage: ChatMessage = {
        id: "30000000-0000-4000-8000-000000000003",
        session_id: "20000000-0000-4000-8000-000000000001",
        role: "user",
        content: "hello",
        tool_calls: null,
        created_at: new Date().toISOString(),
      };
      useSessionStore.setState({
        activeSessionId: "20000000-0000-4000-8000-000000000001",
        messages: [userMessage],
      });
      await useSessionStore.getState().retryMessage("30000000-0000-4000-8000-000000000003");

      await vi.waitFor(() => {
        const map =
          useInterpretationEventsStore.getState().pendingBySession["20000000-0000-4000-8000-000000000001"];
        expect(map?.["evt-recompose"]).toBeDefined();
      });
    });

    it("refreshes pending interpretation events after accepting a proposal", async () => {
      // Same bug class: accepting a proposal (explicit_approve path) can create
      // interpretation events that must surface their review widgets.
      resetStore(useInterpretationEventsStore);
      const apiMod = await import("@/api/client");
      (apiMod.acceptCompositionProposal as ReturnType<typeof vi.fn>).mockResolvedValue({
        id: "proposal-1",
      });
      (apiMod.fetchCompositionState as ReturnType<typeof vi.fn>).mockResolvedValue(null);
      (apiMod.fetchCompositionProposals as ReturnType<typeof vi.fn>).mockResolvedValue([]);
      (apiMod.listInterpretationEvents as ReturnType<typeof vi.fn>).mockResolvedValue([
        makePendingInterpretationEvent("evt-accept"),
      ]);

      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001" });
      await useSessionStore.getState().acceptProposal("proposal-1");

      await vi.waitFor(() => {
        const map =
          useInterpretationEventsStore.getState().pendingBySession["20000000-0000-4000-8000-000000000001"];
        expect(map?.["evt-accept"]).toBeDefined();
      });
    });

    it("handles convergence error with specific message", async () => {
      const { sendMessage: mockSendMessage } = await import("@/api/client");
      (mockSendMessage as ReturnType<typeof vi.fn>).mockRejectedValueOnce({
        status: 422,
        error_type: "convergence",
        detail: "ignored",
      });

      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001" });
      await useSessionStore.getState().sendMessage("hello");

      const state = useSessionStore.getState();
      expect(state.error).toContain("couldn't complete the composition");
    });

    it("names the elapsed budget and the salvaged draft on a wall-clock timeout", async () => {
      const { sendMessage: mockSendMessage } = await import("@/api/client");
      (mockSendMessage as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
        makeTimeoutError(),
      );

      useSessionStore.setState({
        activeSessionId: "20000000-0000-4000-8000-000000000001",
        compositionState: makeCompositionState(5),
      });
      await useSessionStore.getState().sendMessage("hello");

      const state = useSessionStore.getState();
      expect(state.error).toContain(
        "ELSPETH ran out of time (240s). Your partial pipeline was saved — continue from it or retry.",
      );
      // The body's own recovery_text names the next practical action.
      expect(state.error).toContain(
        "ask an operator to raise the composer wall-clock budget",
      );
      expect(state.error).not.toContain("after multiple attempts");
    });

    it("shows the salvaged partial pipeline instead of the stale graph", async () => {
      const { sendMessage: mockSendMessage } = await import("@/api/client");
      const timeoutError = makeTimeoutError();
      (mockSendMessage as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
        timeoutError,
      );

      useSessionStore.setState({
        activeSessionId: "20000000-0000-4000-8000-000000000001",
        compositionState: makeCompositionState(5),
      });
      await useSessionStore.getState().sendMessage("hello");

      const state = useSessionStore.getState();
      expect(state.compositionState).toEqual(timeoutError.partial_state);
      // The recovery panel is now reachable (failed_turn rides the 422)...
      expect(state.recoveryError).toMatchObject(timeoutError);
      // ...and its apply-confirmation gate exists to catch a CONCURRENT
      // third-party edit. Our own fold-in of the partial is not one, so the
      // baseline moves with the store rather than firing a false alarm.
      expect(state.recoveryStartedCompositionVersion).toBe(6);
    });

    it("does not claim a saved draft when the timeout salvaged nothing", async () => {
      const { sendMessage: mockSendMessage } = await import("@/api/client");
      (mockSendMessage as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
        makeTimeoutError({ partial_state: null, failed_turn: null }),
      );

      const before = makeCompositionState(5);
      useSessionStore.setState({
        activeSessionId: "20000000-0000-4000-8000-000000000001",
        compositionState: before,
      });
      await useSessionStore.getState().sendMessage("hello");

      const state = useSessionStore.getState();
      expect(state.error).toContain("ELSPETH ran out of time (240s).");
      expect(state.error).not.toContain("partial pipeline was saved");
      expect(state.compositionState).toBe(before);
    });

    it("omits the elapsed budget when the 422 did not report one", async () => {
      const { sendMessage: mockSendMessage } = await import("@/api/client");
      (mockSendMessage as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
        makeTimeoutError({ timeout_seconds: undefined }),
      );

      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001" });
      await useSessionStore.getState().sendMessage("hello");

      // Never name a number the response did not stand behind.
      expect(useSessionStore.getState().error).toContain(
        "ELSPETH ran out of time. Your partial pipeline was saved",
      );
    });

    it("keeps the turn-budget copy for the non-timeout convergence causes", async () => {
      const { sendMessage: mockSendMessage } = await import("@/api/client");
      (mockSendMessage as ReturnType<typeof vi.fn>).mockRejectedValueOnce({
        status: 422,
        error_type: "convergence",
        detail: "ignored",
        reason: "convergence_composition_budget",
      });

      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001" });
      await useSessionStore.getState().sendMessage("hello");

      const state = useSessionStore.getState();
      expect(state.error).toContain("after multiple attempts");
      expect(state.error).not.toContain("ran out of time");
    });

    it("threads failure_code onto the failed optimistic message for policy_blocked (S1)", async () => {
      const { sendMessage: mockSendMessage } = await import("@/api/client");
      // policy_blocked is permanent by construction — a deployment policy
      // refused the pipeline — so the failed row must carry the code for
      // the Retry affordance to suppress itself (MessageBubble).
      (mockSendMessage as ReturnType<typeof vi.fn>).mockRejectedValueOnce({
        status: 403,
        detail: "This pipeline is not permitted by deployment policy.",
        failure_code: "policy_blocked",
      });

      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001" });
      await useSessionStore.getState().sendMessage("hello");

      const state = useSessionStore.getState();
      expect(state.isComposing).toBe(false);
      expect(state.messages[0].local_status).toBe("failed");
      expect(state.messages[0].local_failure_code).toBe("policy_blocked");
    });

    it("leaves local_failure_code unset when the send failure carries no failure_code (S1)", async () => {
      const { sendMessage: mockSendMessage } = await import("@/api/client");
      (mockSendMessage as ReturnType<typeof vi.fn>).mockRejectedValueOnce({
        status: 500,
        detail: "Something went wrong.",
      });

      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001" });
      await useSessionStore.getState().sendMessage("hello");

      const state = useSessionStore.getState();
      expect(state.messages[0].local_status).toBe("failed");
      expect(state.messages[0].local_failure_code).toBeUndefined();
    });

    it("renders the honest audit-integrity banner and keeps the saved user row un-failed (F-4b)", async () => {
      const { sendMessage: mockSendMessage } = await import("@/api/client");
      // The fail-closed audit-integrity 500 is a READ-side verification
      // refusal: the user row was committed before every raise site, so the
      // banner must say "your message was saved" (with the request id as the
      // support reference) and the optimistic row must NOT be marked failed —
      // a failed marker invites re-sending a duplicate of a committed row.
      (mockSendMessage as ReturnType<typeof vi.fn>).mockRejectedValueOnce({
        status: 500,
        error_type: "audit_integrity_error",
        detail:
          "ELSPETH stopped before replying because it could not verify this session's audit trail.",
        request_id: "req-0123456789ab",
      });

      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001" });
      await useSessionStore.getState().sendMessage("hello");

      const state = useSessionStore.getState();
      expect(state.isComposing).toBe(false);
      expect(state.error).toContain("ELSPETH stopped before replying");
      expect(state.error).toContain("Your message was saved.");
      expect(state.error).toContain("Reload the session");
      expect(state.error).toContain("req-0123456789ab");
      expect(state.messages[0].local_status).toBeUndefined();
      expect(state.messages[0].local_error).toBeUndefined();
    });

    it("omits the request-id reference when the audit-integrity envelope carries none (F-4b)", async () => {
      const { sendMessage: mockSendMessage } = await import("@/api/client");
      (mockSendMessage as ReturnType<typeof vi.fn>).mockRejectedValueOnce({
        status: 500,
        error_type: "audit_integrity_error",
        detail: "ignored",
      });

      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001" });
      await useSessionStore.getState().sendMessage("hello");

      const state = useSessionStore.getState();
      expect(state.error).toContain("Your message was saved.");
      expect(state.error).not.toContain("request ID");
      expect(state.messages[0].local_status).toBeUndefined();
    });

    it("maps a client-side AbortError to the compose-timeout copy, not the generic fallback", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveDetachedOperation();
    });

    it("maps the raw compose_timeout abort reason to the compose-timeout copy (elspeth-475647c47a)", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveDetachedOperation();
    });

    it("maps the raw compose_user_cancel abort reason to the cancelled copy (elspeth-475647c47a)", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveStoppedOperation(true);
    });

    it("resyncs durable server state after a compose abort (elspeth-06a23adfcc)", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveDetachedOperation();
    });

    it("states what persisted when a stopped turn had already saved pipeline changes (elspeth-2784531888)", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveStoppedOperation(true);
    });

    it("states that nothing was saved when a stopped turn had not advanced the pipeline (elspeth-2784531888)", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveStoppedOperation(false);
    });

    it("keeps the saved-changes statement on the compose-timeout abort flavour (elspeth-2784531888)", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveDetachedOperation();
    });

    it("waits for the cancelled turn's terminal progress before resyncing", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveStoppedOperation(true);
    });

    it("keeps waiting past any wall-clock budget while the cancelled turn is still unwinding", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveStoppedOperation(true);
    });

    it("abandons the settle wait when the user switches sessions", async () => {
      // The unbounded wait needs semantic exits: navigating away makes the
      // resync moot, so the loop must end rather than poll a session the
      // user has left (and must not fetch state for it either).
      vi.useFakeTimers();
      try {
        const apiMod = await import("@/api/client");
        const controller = new AbortController();
        (
          apiMod.sendMessage as ReturnType<typeof vi.fn>
        ).mockImplementationOnce(
          () =>
            new Promise((_resolve, reject) => {
              controller.signal.addEventListener("abort", () =>
                reject(controller.signal.reason),
              );
            }),
        );
        (
          apiMod.fetchComposerProgress as ReturnType<typeof vi.fn>
        ).mockResolvedValue({ phase: "using_tools", inflight_requests: 1 });
        (apiMod.fetchMessages as ReturnType<typeof vi.fn>).mockResolvedValue(
          [],
        );
        const stateMock = apiMod.fetchCompositionState as ReturnType<
          typeof vi.fn
        >;
        stateMock.mockResolvedValue(makeCompositionState(3));
        (
          apiMod.fetchCompositionProposals as ReturnType<typeof vi.fn>
        ).mockResolvedValue([]);

        useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001" });
        const sendPromise = useSessionStore
          .getState()
          .sendMessage("hello", controller.signal);
        controller.abort("compose_user_cancel");

        await vi.advanceTimersByTimeAsync(2_000);
        useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000002" });
        await vi.advanceTimersByTimeAsync(25_000);
        await sendPromise;

        expect(stateMock).not.toHaveBeenCalled();
      } finally {
        vi.useRealTimers();
      }
    });

    it("does not treat a previous turn's terminal snapshot as settlement", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveStoppedOperation(true);
    });

    it("abandons the settle wait when a newer compose turn claims the poller", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveGenerationFence();
    });

    it("does not apply a resync snapshot fetched before a newer turn finished", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveGenerationFence();
    });

    it("does not tear down a newer turn's pollers when the aborted turn settles", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveGenerationFence();
    });

    it("keeps the failed-row contract on a non-abort failure (no resync)", async () => {
      const apiMod = await import("@/api/client");
      (apiMod.sendMessage as ReturnType<typeof vi.fn>).mockRejectedValueOnce({
        status: 422,
        error_type: "convergence",
        detail: "ignored",
      });

      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001" });
      await useSessionStore.getState().sendMessage("hello");

      const state = useSessionStore.getState();
      // Non-abort failures keep the pre-existing contract: failed optimistic
      // row + retry affordance, no composition-state refetch (recovery-class
      // errors are mediated by the recovery panel, not a silent resync).
      expect(apiMod.fetchCompositionState).toHaveBeenCalled();
      expect(state.messages[0].local_status).toBe("failed");
    });

    it("reconciles a lost POST response from saved messages and state without resending", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await provePendingOperation({ ambiguous: true });
    });

    it("keeps the retry affordance when a lost POST response has no durable evidence", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await provePendingOperation({ ambiguous: true });
    });

    it("includes provider detail when an LLM unavailable response exposes it", async () => {
      const { sendMessage: mockSendMessage } = await import("@/api/client");
      (mockSendMessage as ReturnType<typeof vi.fn>).mockRejectedValueOnce({
        status: 502,
        error_type: "llm_unavailable",
        detail: "APIError",
        provider_detail:
          "litellm.APIError: OpenRouter upstream rejected request: insufficient credits",
        provider_status_code: 402,
      });

      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001" });
      await useSessionStore.getState().sendMessage("hello");

      const state = useSessionStore.getState();
      expect(state.error).toContain("The AI service is temporarily unavailable");
      expect(state.error).toContain(
        "litellm.APIError: OpenRouter upstream rejected request: insufficient credits",
      );
      expect(state.error).toContain("Provider status: 402");
      expect(state.messages[0].local_error).toBe(state.error);
    });

    it("shows the gateway's safe retry guidance instead of generic immediate retry copy", async () => {
      const { sendMessage: mockSendMessage } = await import("@/api/client");
      (mockSendMessage as ReturnType<typeof vi.fn>).mockRejectedValueOnce({
        status: 502,
        error_type: "llm_unavailable",
        detail: "BadGatewayError",
        guidance: "Retry later; if this continues, ask an administrator to investigate the model gateway.",
      });
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001" });
      await useSessionStore.getState().sendMessage("hello");
      expect(useSessionStore.getState().error).toContain("Retry later; if this continues");
      expect(useSessionStore.getState().error).not.toContain("try again in a moment");
    });

    it("opens recovery state for recovery-shaped compose failures", async () => {
      const { sendMessage: mockSendMessage } = await import("@/api/client");
      const recoveryError = makeRecoveryError();
      (mockSendMessage as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
        recoveryError,
      );

      useSessionStore.setState({
        activeSessionId: "20000000-0000-4000-8000-000000000001",
        compositionState: makeCompositionState(5),
      });
      await useSessionStore.getState().sendMessage("hello");

      const state = useSessionStore.getState();
      expect(state.recoveryError).toMatchObject(recoveryError);
      expect(state.recoveryStartedCompositionVersion).toBe(5);
      expect(state.messages[0].local_status).toBe("failed");
      expect(state.messages[0].local_error).toBe("compose failed");
    });

    it("does not open recovery state for convergence errors without recovery fields", async () => {
      const { sendMessage: mockSendMessage } = await import("@/api/client");
      (mockSendMessage as ReturnType<typeof vi.fn>).mockRejectedValueOnce({
        status: 422,
        error_type: "convergence",
        detail: "ignored",
      });

      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001" });
      await useSessionStore.getState().sendMessage("hello");

      const state = useSessionStore.getState();
      expect(state.error).toContain("couldn't complete the composition");
      expect(state.recoveryError).toBeNull();
      expect(state.recoveryStartedCompositionVersion).toBeNull();
    });

    it("drops stale sendMessage responses after the active session changes", async () => {
      const { sendMessage: mockSendMessage } = await import("@/api/client");
      const sendDeferred = deferred<{
        message: ChatMessage;
        state: CompositionState | null;
        proposals?: CompositionProposal[];
      }>();
      (mockSendMessage as ReturnType<typeof vi.fn>).mockReturnValueOnce(
        sendDeferred.promise,
      );

      useSessionStore.setState({
        activeSessionId: "20000000-0000-4000-8000-000000000008",
        messages: [],
        compositionState: makeCompositionState(1, ["a-start"]),
      });
      const sendPromise = useSessionStore.getState().sendMessage("build it");
      await Promise.resolve();

      const currentMessage: ChatMessage = {
        id: "b-message",
        session_id: "20000000-0000-4000-8000-000000000009",
        role: "user",
        content: "current session",
        tool_calls: null,
        created_at: "2026-05-14T00:00:00Z",
      };
      const currentState = makeCompositionState(20, ["b-node"]);
      useSessionStore.setState({
        activeSessionId: "20000000-0000-4000-8000-000000000009",
        messages: [currentMessage],
        compositionState: currentState,
        compositionProposals: [
          makeCompositionProposal({ id: "proposal-b", session_id: "20000000-0000-4000-8000-000000000009" }),
        ],
        isComposing: false,
      });

      sendDeferred.resolve({
        message: {
          id: "a-assistant",
          session_id: "20000000-0000-4000-8000-000000000008",
          role: "assistant",
          content: "stale response",
          tool_calls: null,
          created_at: "2026-05-14T00:00:01Z",
        },
        state: makeCompositionState(2, ["a-node"]),
        proposals: [
          makeCompositionProposal({ id: "proposal-a", session_id: "20000000-0000-4000-8000-000000000008" }),
        ],
      });
      await sendPromise;

      const state = useSessionStore.getState();
      expect(state.activeSessionId).toBe("20000000-0000-4000-8000-000000000009");
      expect(state.messages).toEqual([currentMessage]);
      expect(state.compositionState).toBe(currentState);
      expect(state.compositionProposals).toEqual([
        expect.objectContaining({ id: "proposal-b", session_id: "20000000-0000-4000-8000-000000000009" }),
      ]);
      expect(state.error).toBeNull();
    });

    it("polls composer progress only while a send is composing", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await provePendingOperation({ ambiguous: true });
    });

    it("polls inflight messages while a send is composing and stops on completion", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await provePendingOperation({ ambiguous: true });
    });

    it("stops inflight message polling when the store is reset", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveGenerationFence();
    });

    describe("inflight poll fence (elspeth-90f453d7b2)", () => {
      const pollUser: ChatMessage = {
        id: "msg-poll-user",
        session_id: "20000000-0000-4000-8000-000000000001",
        role: "user",
        content: "hi",
        tool_calls: null,
        created_at: "2026-04-26T10:00:00Z",
      };
      const pollReply: ChatMessage = {
        id: "msg-poll-reply",
        session_id: "20000000-0000-4000-8000-000000000001",
        role: "assistant",
        content: "REPLY",
        tool_calls: null,
        created_at: "2026-04-26T10:00:02Z",
      };

      async function armProgressIdle(): Promise<void> {
        const { fetchComposerProgress } = await import("@/api/client");
        (fetchComposerProgress as ReturnType<typeof vi.fn>).mockResolvedValue({
          session_id: "20000000-0000-4000-8000-000000000001",
          request_id: "msg-poll-user",
          phase: "idle",
          inflight_requests: 0,
          headline: "",
          evidence: [],
          likely_next: null,
          reason: null,
          updated_at: "2026-04-26T10:00:00Z",
        });
      }

      it("drops a poll tick that resolves after the turn settled and polling stopped", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveGenerationFence();
    });

      it("drops an old turn's poll tick once a newer same-session turn owns the poller", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveGenerationFence();
    });

      // A -> B -> A while the turn is in flight: selectSession stops the
      // poller (id null) but does not abort the POST, so the owning turn's
      // explicit post-settle sync must still land on its return.
      async function armSelectSessionReads(): Promise<void> {
        const api = await import("@/api/client");
        (api.fetchCompositionState as ReturnType<typeof vi.fn>).mockResolvedValue(
          null,
        );
        (
          api.fetchCompositionProposals as ReturnType<typeof vi.fn>
        ).mockResolvedValue([]);
        (
          api.fetchComposerPreferences as ReturnType<typeof vi.fn>
        ).mockResolvedValue(null);
      }

      it("does not publish an old accepted receipt over a newer A turn after A->B->A", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveGenerationFence();
    });

      const pollTool: ChatMessage = {
        id: "msg-poll-tool",
        session_id: "20000000-0000-4000-8000-000000000001",
        role: "assistant",
        content: "TOOLROW",
        tool_calls: null,
        created_at: "2026-04-26T10:00:01Z",
      };

      it("applies the owning turn's post-settle sync after an A->B->A switch stopped the poller", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveGenerationFence();
    });

      it("applies the owning turn's post-settle sync when the newer turn belongs to another session", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveGenerationFence();
    });

      it("drops an old turn's post-settle sync once a newer turn on the same session claimed the poller", async () => {
        const { sendMessage: mockSendMessage, fetchMessages } = await import(
          "@/api/client"
        );
        await armProgressIdle();
        await armSelectSessionReads();
        const firstSend = deferred<{ message: ChatMessage; state: null }>();
        const secondSend = deferred<{ message: ChatMessage; state: null }>();
        (mockSendMessage as ReturnType<typeof vi.fn>)
          .mockReturnValueOnce(firstSend.promise)
          .mockReturnValueOnce(secondSend.promise);
        const staleSentinel: ChatMessage = {
          ...pollTool,
          id: "msg-stale-sentinel",
          content: "STALE",
        };
        let sessionOneFetches = 0;
        (fetchMessages as ReturnType<typeof vi.fn>).mockImplementation(
          async (id: string) => {
            if (id !== "20000000-0000-4000-8000-000000000001") return [];
            sessionOneFetches += 1;
            // A reselect read is the only read needed once a newer turn has
            // claimed this session's compose state.
            return sessionOneFetches === 1
              ? [pollUser]
              : [pollUser, staleSentinel];
          },
        );

        useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001", messages: [] });
        const firstPromise = useSessionStore.getState().sendMessage("hi");
        await Promise.resolve();
        await useSessionStore.getState().selectSession("20000000-0000-4000-8000-000000000002");
        await useSessionStore.getState().selectSession("20000000-0000-4000-8000-000000000001");
        // selectSession cleared isComposing, so a newer turn on session-1
        // starts while the first POST is still in flight.
        const secondPromise = useSessionStore.getState().sendMessage("again");
        await Promise.resolve();

        firstSend.resolve({ message: pollReply, state: null });
        await firstPromise;

        expect(sessionOneFetches).toBe(1);
        expect(useSessionStore.getState().messages.map((m) => m.id)).not.toContain(
          "msg-stale-sentinel",
        );

        secondSend.resolve({ message: pollReply, state: null });
        await secondPromise;
      });

      it.each(["send", "retry"] as const)(
        "keeps the newer A turn when an old %s POST succeeds after A->B->A",
        async (oldOperation) => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveGenerationFence(oldOperation === "retry" ? "compose_recompose" : "compose_message");
    },
      );

      it.each(["send", "retry"] as const)(
        "keeps the newer A turn when an old %s POST fails after A->B->A",
        async (oldOperation) => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveGenerationFence(oldOperation === "retry" ? "compose_recompose" : "compose_message");
    },
      );

      it.each(["send", "retry"] as const)(
        "drops an old %s POST metadata after its message sync is superseded",
        async (oldOperation) => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveGenerationFence(oldOperation === "retry" ? "compose_recompose" : "compose_message");
    },
      );

      it("applies the aborted turn's resync message sync after an A->B->A switch stopped the poller", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveGenerationFence();
    });

      it("keeps the newer message list when overlapping ticks resolve out of order", async () => {
        // The interval launches a read without awaiting the previous one, so
        // two reads of the SAME generation can be in flight together. The
        // ownership fence passes both; only response ordering keeps the older
        // one from rolling the list back (polling audit 2026-09-22, finding 3).
        vi.useFakeTimers();
        try {
          const { fetchMessages } = await import("@/api/client");
          const firstTick = deferred<ChatMessage[]>();
          (fetchMessages as ReturnType<typeof vi.fn>)
            .mockReturnValueOnce(firstTick.promise)
            .mockResolvedValueOnce([pollUser, pollReply]);

          useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001", messages: [] });
          useSessionStore.getState().startInflightMessagesPolling("20000000-0000-4000-8000-000000000001");

          await vi.advanceTimersByTimeAsync(1500);
          expect(fetchMessages).toHaveBeenCalledTimes(1);
          await vi.advanceTimersByTimeAsync(1500);
          expect(fetchMessages).toHaveBeenCalledTimes(2);
          expect(useSessionStore.getState().messages.map((m) => m.id)).toEqual([
            "msg-poll-user",
            "msg-poll-reply",
          ]);

          // The first tick finally answers, carrying the list as it was two
          // ticks ago.
          firstTick.resolve([pollUser]);
          await vi.advanceTimersByTimeAsync(0);

          expect(useSessionStore.getState().messages.map((m) => m.id)).toEqual([
            "msg-poll-user",
            "msg-poll-reply",
          ]);
        } finally {
          useSessionStore.getState().stopInflightMessagesPolling();
          vi.useRealTimers();
        }
      });

      it("keeps the owning turn's sync when an earlier tick of the same generation lands after it", async () => {
        // The owning turn's post-settle sync runs BEFORE its finally stops the
        // poller, so a tick already in flight is still the live generation for
        // the live session — every ownership check passes and only ordering
        // stops it erasing the reply the sync just brought in.
        const { fetchMessages } = await import("@/api/client");
        const staleTick = deferred<ChatMessage[]>();
        (fetchMessages as ReturnType<typeof vi.fn>)
          .mockReturnValueOnce(staleTick.promise)
          .mockResolvedValueOnce([pollUser, pollReply]);

        useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001", messages: [] });
        const owner = useSessionStore
          .getState()
          .startInflightMessagesPolling("20000000-0000-4000-8000-000000000001");
        try {
          const tick = useSessionStore.getState().loadInflightMessages("20000000-0000-4000-8000-000000000001");
          await useSessionStore.getState().loadInflightMessages("20000000-0000-4000-8000-000000000001", owner);
          expect(useSessionStore.getState().messages.map((m) => m.id)).toEqual([
            "msg-poll-user",
            "msg-poll-reply",
          ]);

          staleTick.resolve([pollUser]);
          await tick;

          expect(useSessionStore.getState().messages.map((m) => m.id)).toEqual([
            "msg-poll-user",
            "msg-poll-reply",
          ]);
        } finally {
          useSessionStore.getState().stopInflightMessagesPolling();
        }
      });
    });

    describe("progress poll fence (polling audit 2026-09-22)", () => {
      function snapshot(
        requestId: string,
        phase: ComposerProgressSnapshot["phase"] = "using_tools",
      ): ComposerProgressSnapshot {
        return {
          session_id: "20000000-0000-4000-8000-000000000001",
          request_id: requestId,
          phase,
          headline: requestId,
          evidence: [],
          likely_next: null,
          reason: null,
          updated_at: "2026-04-26T10:00:00Z",
        };
      }

      it("drops a progress tick that resolves after the turn settled and polling stopped", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveGenerationFence();
    });

      it("drops an old turn's progress read once a newer same-session turn owns the poller", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveGenerationFence();
    });

      it("keeps the newer snapshot when overlapping progress ticks resolve out of order", async () => {
        vi.useFakeTimers();
        try {
          const { fetchComposerProgress } = await import("@/api/client");
          const slowTick = deferred<ComposerProgressSnapshot>();
          (fetchComposerProgress as ReturnType<typeof vi.fn>)
            .mockResolvedValueOnce(snapshot("first"))
            .mockReturnValueOnce(slowTick.promise)
            .mockResolvedValueOnce(snapshot("third"));

          useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001" });
          useSessionStore.getState().startComposerProgressPolling("20000000-0000-4000-8000-000000000001");
          await vi.advanceTimersByTimeAsync(0);
          expect(useSessionStore.getState().composerProgress?.request_id).toBe("first");

          await vi.advanceTimersByTimeAsync(1500);
          await vi.advanceTimersByTimeAsync(1500);
          expect(fetchComposerProgress).toHaveBeenCalledTimes(3);
          expect(useSessionStore.getState().composerProgress?.request_id).toBe("third");

          slowTick.resolve(snapshot("second"));
          await vi.advanceTimersByTimeAsync(0);

          expect(useSessionStore.getState().composerProgress?.request_id).toBe("third");
        } finally {
          useSessionStore.getState().stopComposerProgressPolling();
          vi.useRealTimers();
        }
      });

      it("control: drops a progress read for a session the user has navigated away from", async () => {
        const { fetchComposerProgress } = await import("@/api/client");
        const pending = deferred<ComposerProgressSnapshot>();
        (fetchComposerProgress as ReturnType<typeof vi.fn>).mockReturnValueOnce(
          pending.promise,
        );

        useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001" });
        const read = useSessionStore.getState().loadComposerProgress("20000000-0000-4000-8000-000000000001");
        useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000002" });
        pending.resolve(snapshot("orphan"));
        await read;

        expect(useSessionStore.getState().composerProgress).toBeNull();
      });
    });

    it("drops a stale send response after the active session changes", async () => {
      const { sendMessage: mockSendMessage } = await import("@/api/client");
      const sendDeferred = deferred<{
        message: ChatMessage;
        state: CompositionState | null;
      }>();
      (mockSendMessage as ReturnType<typeof vi.fn>).mockReturnValueOnce(
        sendDeferred.promise,
      );

      useSessionStore.setState({
        activeSessionId: "20000000-0000-4000-8000-000000000001",
        messages: [],
        compositionState: makeCompositionState(1),
      });
      const sendPromise = useSessionStore.getState().sendMessage("hello");
      await Promise.resolve();

      const sessionTwoState = makeCompositionState(2);
      useSessionStore.setState({
        activeSessionId: "20000000-0000-4000-8000-000000000002",
        messages: [],
        compositionState: sessionTwoState,
        isComposing: false,
      });
      sendDeferred.resolve({
        message: {
          id: "stale-assistant",
          session_id: "20000000-0000-4000-8000-000000000001",
          role: "assistant",
          content: "stale",
          tool_calls: null,
          created_at: "2026-04-26T10:00:01Z",
        },
        state: makeCompositionState(99),
      });
      await sendPromise;

      const state = useSessionStore.getState();
      expect(state.activeSessionId).toBe("20000000-0000-4000-8000-000000000002");
      expect(state.messages).toEqual([]);
      expect(state.compositionState).toBe(sessionTwoState);
      expect(state.isComposing).toBe(false);
    });

    it("preserves the optimistic user message when the canonical row has not yet appeared", async () => {
      vi.useFakeTimers();
      try {
        const {
          sendMessage: mockSendMessage,
          fetchMessages,
          fetchComposerProgress,
        } = await import("@/api/client");
        const sendDeferred =
          deferred<{ message: ChatMessage; state: null }>();
        // First poll returns an empty list (the canonical user hasn't been
        // persisted yet — should not happen in production, but the merge
        // logic must not silently drop the optimistic row if it does).
        (fetchMessages as ReturnType<typeof vi.fn>).mockResolvedValueOnce([]);
        (fetchComposerProgress as ReturnType<typeof vi.fn>).mockResolvedValue({
          session_id: "20000000-0000-4000-8000-000000000001",
          request_id: "msg-pending",
          phase: "idle",
          headline: "",
          evidence: [],
          likely_next: null,
          reason: null,
          updated_at: "2026-04-26T10:00:00Z",
        });
        (mockSendMessage as ReturnType<typeof vi.fn>).mockReturnValueOnce(
          sendDeferred.promise,
        );

        useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001", messages: [] });
        const sendPromise = useSessionStore.getState().sendMessage("the user's question");
        await Promise.resolve();
        await vi.advanceTimersByTimeAsync(1500);
        const afterEmptyPoll = useSessionStore.getState().messages;
        // The local-* optimistic row survives because no canonical row in
        // fresh matched its (role, content) tuple.
        expect(afterEmptyPoll).toHaveLength(1);
        expect(afterEmptyPoll[0].id).toMatch(/^local-/);
        expect(afterEmptyPoll[0].role).toBe("user");
        expect(afterEmptyPoll[0].content).toBe("the user's question");

        // Wind down cleanly.
        (fetchMessages as ReturnType<typeof vi.fn>).mockResolvedValue([
          {
            id: "msg-final",
            session_id: "20000000-0000-4000-8000-000000000001",
            role: "assistant",
            content: "ok",
            tool_calls: null,
            created_at: "2026-04-26T10:00:01Z",
          } as ChatMessage,
        ]);
        sendDeferred.resolve({
          message: {
            id: "msg-final",
            session_id: "20000000-0000-4000-8000-000000000001",
            role: "assistant",
            content: "ok",
            tool_calls: null,
            created_at: "2026-04-26T10:00:01Z",
          } as ChatMessage,
          state: null,
        });
        await sendPromise;
      } finally {
        vi.useRealTimers();
      }
    });
  });

  describe("freeform compose admission gate (elspeth-3f38ebb1b5)", () => {
    // Exactly one freeform compose may be admitted per session: without a
    // synchronous store-level gate, alternate entry points (Retry on a
    // failed bubble, Use-as-input in the blob manager) could start a second
    // compose whose AbortController replaced the first one's — leaving Stop
    // owning only the newest request.
    function assistantReply(id: string): {
      message: ChatMessage;
      state: null;
    } {
      return {
        message: {
          id,
          session_id: "20000000-0000-4000-8000-000000000001",
          role: "assistant",
          content: "done",
          tool_calls: null,
          created_at: "2026-08-09T10:00:00Z",
        },
        state: null,
      };
    }

    it("refuses a second sendMessage while one is in flight", async () => {
      const { sendMessage: mockSendMessage } = await import("@/api/client");
      const first = deferred<{ message: ChatMessage; state: null }>();
      (mockSendMessage as ReturnType<typeof vi.fn>).mockReturnValueOnce(
        first.promise,
      );

      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001", messages: [] });
      const firstSend = useSessionStore.getState().sendMessage("first");
      await Promise.resolve();

      await useSessionStore.getState().sendMessage("second");

      expect(mockSendMessage).toHaveBeenCalledTimes(1);
      const userContents = useSessionStore
        .getState()
        .messages.filter((m) => m.role === "user")
        .map((m) => m.content);
      expect(userContents).toEqual(["first"]);

      first.resolve(assistantReply("asst-gate-1"));
      await firstSend;
      expect(useSessionStore.getState().isComposing).toBe(false);
    });

    it("refuses retryMessage while a compose is in flight", async () => {
      const { sendMessage: mockSendMessage, recompose: mockRecompose } =
        await import("@/api/client");
      const first = deferred<{ message: ChatMessage; state: null }>();
      (mockSendMessage as ReturnType<typeof vi.fn>).mockReturnValueOnce(
        first.promise,
      );

      const failedMessage: ChatMessage = {
        id: "failed-1",
        session_id: "20000000-0000-4000-8000-000000000001",
        role: "user",
        content: "previously failed",
        tool_calls: null,
        created_at: "2026-08-09T09:59:00Z",
        local_status: "failed",
      };
      useSessionStore.setState({
        activeSessionId: "20000000-0000-4000-8000-000000000001",
        messages: [failedMessage],
      });
      const firstSend = useSessionStore.getState().sendMessage("first");
      await Promise.resolve();

      await useSessionStore.getState().retryMessage("failed-1");

      expect(mockRecompose).not.toHaveBeenCalled();
      // The failed message must not have been flipped to pending by a
      // refused retry.
      expect(
        useSessionStore
          .getState()
          .messages.find((m) => m.id === "failed-1")?.local_status,
      ).toBe("failed");

      first.resolve(assistantReply("asst-gate-2"));
      await firstSend;
    });

    it("admits a new compose after the previous settles (control)", async () => {
      const { sendMessage: mockSendMessage, fetchMessages, fetchCompositionState, fetchCompositionProposals } = await import("@/api/client");
      vi.mocked(fetchMessages).mockResolvedValue([]);
      vi.mocked(fetchCompositionState).mockResolvedValue(null);
      vi.mocked(fetchCompositionProposals).mockResolvedValue([]);
      (mockSendMessage as ReturnType<typeof vi.fn>)
        .mockResolvedValueOnce(assistantReply("asst-gate-3"))
        .mockResolvedValueOnce(assistantReply("asst-gate-4"));

      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001", messages: [] });
      await useSessionStore.getState().sendMessage("first");
      expect(useSessionStore.getState().compositionStateLoaded).toBe(true);
      await useSessionStore.getState().sendMessage("second");

      expect(mockSendMessage).toHaveBeenCalledTimes(2);
    });

    it("requires a refreshed session head before admitting another settled compose", async () => {
      const api = await import("@/api/client");
      vi.mocked(api.sendMessage).mockResolvedValueOnce({ ...assistantReply("asst-unrefreshed"), proposals: [] });
      useSessionStore.setState({ activeSessionId: "20000000-0000-4000-8000-000000000001", messages: [] });
      await useSessionStore.getState().sendMessage("first");
      expect(useSessionStore.getState().isComposing).toBe(false);
      expect(useSessionStore.getState().compositionStateLoaded).toBe(false);
      await useSessionStore.getState().sendMessage("second");
      expect(api.submitComposerOperation).toHaveBeenCalledTimes(1);
      expect(api.sendMessage).toHaveBeenCalledTimes(1);
      expect(useSessionStore.getState().messages.filter((message) => message.role === "user").map((message) => message.content)).toEqual(["first"]);
    });
  });

  describe("renameSession", () => {
    it("persists a trimmed title and updates the matching session", async () => {
      const apiClient = await import("@/api/client");
      const renamed = {
        id: "20000000-0000-4000-8000-000000000001",
        title: "Renamed pipeline",
        created_at: "2026-05-14T00:00:00Z",
        updated_at: "2026-05-14T00:01:00Z",
      };
      (
        apiClient.renameSession as ReturnType<typeof vi.fn>
      ).mockResolvedValue(renamed);
      useSessionStore.setState({
        activeSessionId: "20000000-0000-4000-8000-000000000001",
        sessions: [
          {
            id: "20000000-0000-4000-8000-000000000001",
            title: "Current session",
            created_at: "2026-05-14T00:00:00Z",
            updated_at: "2026-05-14T00:00:00Z",
          },
        ],
      });

      await useSessionStore.getState().renameSession("20000000-0000-4000-8000-000000000001", "  Renamed pipeline  ");

      expect(apiClient.renameSession).toHaveBeenCalledWith(
        "20000000-0000-4000-8000-000000000001",
        "Renamed pipeline",
      );
      expect(useSessionStore.getState().sessions[0]).toBe(renamed);
      expect(useSessionStore.getState().error).toBeNull();
    });

    it("does not call the API for a blank title", async () => {
      const apiClient = await import("@/api/client");
      useSessionStore.setState({
        sessions: [
          {
            id: "20000000-0000-4000-8000-000000000001",
            title: "Current session",
            created_at: "2026-05-14T00:00:00Z",
            updated_at: "2026-05-14T00:00:00Z",
          },
        ],
      });

      await useSessionStore.getState().renameSession("20000000-0000-4000-8000-000000000001", "   ");

      expect(apiClient.renameSession).not.toHaveBeenCalled();
      expect(useSessionStore.getState().sessions[0].title).toBe("Current session");
    });
  });

  describe("composer proposals", () => {
    it.each(["none", "tool", "reply"] as const)(
      "keeps accepted-message retry only when %s assistant evidence has no genuine reply",
      async (evidence) => {
        const api = await import("@/api/client");
        vi.mocked(api.sendMessage).mockImplementationOnce((_session, _content, requestId) =>
          Promise.reject({
            status: 409, error_type: "message_already_accepted",
            operation_id: requestId, user_message_id: "30000000-0000-4000-8000-000000000002",
            detail: "Already accepted",
          }),
        );
        vi.mocked(api.fetchMessages).mockImplementation(async () => {
          const rows: ChatMessage[] = [{
            id: "30000000-0000-4000-8000-000000000002", session_id: "20000000-0000-4000-8000-000000000001", role: "user", content: "hello",
            operation_id: vi.mocked(api.sendMessage).mock.calls[0][2],
            tool_calls: null, created_at: "2026-09-27T00:00:00Z",
          }];
          if (evidence !== "none") rows.push({
            id: "assistant", session_id: "20000000-0000-4000-8000-000000000001", role: "assistant",
            content: evidence === "tool" ? "Inspecting your source" : "Done",
            tool_calls: evidence === "tool"
              ? [{ id: "call", type: "function", function: { name: "list_plugins", arguments: "{}" } }]
              : null,
            created_at: "2026-09-27T00:00:01Z",
          });
          return rows;
        });
        vi.mocked(api.fetchCompositionState).mockResolvedValue(null);
        vi.mocked(api.fetchCompositionProposals).mockResolvedValue([]);
        vi.mocked(api.fetchComposerProgress).mockResolvedValue({
          phase: "failed", inflight_requests: 0,
        } as ComposerProgressSnapshot);
        useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001" });

        await useSessionStore.getState().sendMessage("hello");

        expect(api.recompose).not.toHaveBeenCalled();
        expect(useSessionStore.getState().messages[0].local_status).toBe(
          evidence === "reply" ? undefined : "failed",
        );
      },
    );

    it("keeps saved-message retry metadata when a poll sees only tool-call narration", async () => {
      const api = await import("@/api/client");
      const user: ChatMessage = {
        id: "30000000-0000-4000-8000-000000000002", session_id: "20000000-0000-4000-8000-000000000001", role: "user", content: "hello",
        operation_id: "request-1", tool_calls: null,
        created_at: "2026-09-27T00:00:00Z",
      };
      vi.mocked(api.fetchMessages).mockResolvedValueOnce([
        user,
        {
          id: "tool-narration", session_id: "20000000-0000-4000-8000-000000000001", role: "assistant",
          content: "Inspecting your source",
          tool_calls: [{ id: "call", type: "function", function: { name: "list_plugins", arguments: "{}" } }],
          created_at: "2026-09-27T00:00:01Z",
        },
      ]);
      useSessionStore.setState({
        activeSessionId: "20000000-0000-4000-8000-000000000001",
        messages: [{ ...user, local_status: "failed", local_error: "Retry saved message" }],
      });
      useSessionStore.getState().startInflightMessagesPolling("20000000-0000-4000-8000-000000000001");

      await useSessionStore.getState().loadInflightMessages("20000000-0000-4000-8000-000000000001");

      expect(useSessionStore.getState().messages[0].local_status).toBe("failed");
      useSessionStore.getState().stopInflightMessagesPolling("20000000-0000-4000-8000-000000000001");
    });

    it.each(["reject", "accept"] as const)(
      "keeps a newer %s receipt and state over a delayed accepted-message snapshot",
      async (decision) => {
        const api = await import("@/api/client");
        const slowState = deferred<CompositionState | null>();
        const pending = makeCompositionProposal();
        const receipt = makeCompositionProposal({ status: decision === "accept" ? "committed" : "rejected" });
        vi.mocked(api.sendMessage).mockImplementationOnce((_session, _content, requestId) =>
          Promise.reject({
            status: 409, error_type: "message_already_accepted",
            operation_id: requestId, user_message_id: "30000000-0000-4000-8000-000000000002",
            detail: "Already accepted",
          }),
        );
        vi.mocked(api.fetchMessages).mockImplementation(async () => [{
          id: "30000000-0000-4000-8000-000000000002", session_id: "20000000-0000-4000-8000-000000000001", role: "user", content: "hello",
          operation_id: vi.mocked(api.sendMessage).mock.calls[0][2],
          tool_calls: null, created_at: "2026-09-27T00:00:00Z",
        }]);
        vi.mocked(api.fetchCompositionState)
          .mockReturnValueOnce(slowState.promise)
          .mockResolvedValue(makeCompositionState(2));
        vi.mocked(api.fetchCompositionProposals)
          .mockResolvedValueOnce([pending])
          .mockResolvedValue([receipt]);
        vi.mocked(api.fetchComposerProgress).mockResolvedValue({
          phase: "failed", inflight_requests: 0,
        } as ComposerProgressSnapshot);
        vi.mocked(decision === "accept" ? api.acceptCompositionProposal : api.rejectCompositionProposal)
          .mockResolvedValue(receipt);
        useSessionStore.setState({
          activeSessionId: "20000000-0000-4000-8000-000000000001", compositionState: makeCompositionState(1),
          compositionStateLoaded: true, compositionProposals: [pending],
        });

        const send = useSessionStore.getState().sendMessage("hello");
        await vi.waitFor(() => expect(api.fetchCompositionState).toHaveBeenCalled());
        // A receipt from another authenticated observer may arrive while
        // this terminal hydration is delayed. Local acceptance remains gated.
        if (decision === "accept") {
          await useSessionStore.getState().acceptProposal(pending.id);
          expect(api.acceptCompositionProposal).not.toHaveBeenCalled();
        }
        useSessionStore.setState({ compositionProposals: [receipt], compositionState: makeCompositionState(2) });
        expect(useSessionStore.getState().compositionProposals[0].status).toBe(receipt.status);
        if (decision === "accept") expect(useSessionStore.getState().compositionState?.version).toBe(2);

        slowState.resolve(makeCompositionState(1));
        await send;

        expect(useSessionStore.getState().compositionProposals[0].status).toBe(receipt.status);
        if (decision === "accept") expect(useSessionStore.getState().compositionState?.version).toBe(2);
      },
    );

    it.each([409, 500])("retains confirmed staleness when rejection fails (%s)", async (status) => {
      const api = await import("@/api/client");
      const pending = makeCompositionProposal();
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001", compositionProposals: [pending], staleProposalIds: [pending.id] });
      vi.mocked(api.rejectCompositionProposal).mockRejectedValue({ status, detail: "Rejection failed" });
      vi.mocked(api.fetchCompositionProposals).mockResolvedValue([pending]);
      await useSessionStore.getState().rejectProposal(pending.id);
      expect(useSessionStore.getState().staleProposalIds).toContain(pending.id);
    });

    it("does not let an older conflict refresh retire a proposal found by a newer read", async () => {
      const api = await import("@/api/client");
      const pending = makeCompositionProposal();
      let finishOld!: (proposals: CompositionProposal[]) => void;
      vi.mocked(api.acceptCompositionProposal).mockRejectedValue({ status: 409, detail: "Busy" });
      vi.mocked(api.fetchCompositionProposals)
        .mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve; }))
        .mockResolvedValueOnce([pending]);
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001", compositionProposals: [pending] });
      const accepting = useSessionStore.getState().acceptProposal(pending.id);
      await vi.waitFor(() => expect(finishOld).toBeTypeOf("function"));
      await useSessionStore.getState().loadCompositionProposals();
      finishOld([]);
      await accepting;
      expect(useSessionStore.getState().compositionProposals).toEqual([pending]);
      expect(useSessionStore.getState().staleProposalIds).not.toContain(pending.id);
    });

    it("retains newer composition and pending arrivals after delayed accept hydration", async () => {
      const api = await import("@/api/client");
      const pending = makeCompositionProposal();
      const accepted = makeCompositionProposal({ status: "committed" });
      const fresh = makeCompositionProposal({ id: "fresh", tool_call_id: "fresh-call", base_state_id: "10000000-0000-4000-8000-000000000003" });
      let finishState!: (state: CompositionState) => void;
      let finishList!: (proposals: CompositionProposal[]) => void;
      vi.mocked(api.acceptCompositionProposal).mockResolvedValue(accepted);
      vi.mocked(api.fetchCompositionState).mockImplementationOnce(() => new Promise((resolve) => { finishState = resolve; }));
      vi.mocked(api.fetchCompositionProposals).mockImplementationOnce(() => new Promise((resolve) => { finishList = resolve; }));
      vi.mocked(api.sendMessage).mockResolvedValue({
        message: { id: "asst-new", session_id: "20000000-0000-4000-8000-000000000001", role: "assistant", content: "Updated", tool_calls: null, created_at: "2026-09-22T00:00:00Z" },
        state: makeCompositionState(3), proposals: [fresh],
      });
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001", compositionState: makeCompositionState(1), compositionProposals: [pending], messages: [] });
      const acceptance = useSessionStore.getState().acceptProposal(pending.id);
      await vi.waitFor(() => expect(finishState).toBeTypeOf("function"));
      await useSessionStore.getState().sendMessage("Revise");
      expect(api.submitComposerOperation).not.toHaveBeenCalled();
      // The head is deliberately unavailable until acceptance hydrates. An
      // independently authenticated state refresh may still publish newer data.
      useSessionStore.setState({ compositionState: makeCompositionState(3), compositionStateLoaded: true, compositionProposals: [accepted, fresh] });
      expect(useSessionStore.getState().compositionState?.id).toBe("10000000-0000-4000-8000-000000000003");
      finishState(makeCompositionState(2));
      finishList([accepted]);
      await acceptance;
      expect(useSessionStore.getState().compositionProposals).toContainEqual(fresh);
      expect(useSessionStore.getState().compositionState?.id).toBe("10000000-0000-4000-8000-000000000003");
    });

    it("retires a confirmed stale-base proposal even while its lifecycle is pending", async () => {
      const api = await import("@/api/client");
      const proposal = makeCompositionProposal({ base_state_id: "10000000-0000-4000-8000-000000000001" });
      vi.mocked(api.acceptCompositionProposal).mockRejectedValue({
        status: 409, error_type: "proposal_base_state_changed", detail: "Rebase required.",
      });
      vi.mocked(api.fetchCompositionProposals).mockResolvedValue([proposal]);
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001", compositionProposals: [proposal], compositionState: makeCompositionState(2) });
      await useSessionStore.getState().acceptProposal(proposal.id);
      expect(useSessionStore.getState().staleProposalIds).toContain(proposal.id);
      expect(useSessionStore.getState().error).toBe("Rebase required.");
    });

    it("keeps the newest proposal snapshot when reads complete in reverse order", async () => {
      const api = await import("@/api/client");
      const proposal = makeCompositionProposal();
      let finishOld!: (proposals: CompositionProposal[]) => void;
      vi.mocked(api.fetchCompositionProposals)
        .mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve; }))
        .mockResolvedValueOnce([proposal]);
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001", compositionProposals: [] });
      const oldRead = useSessionStore.getState().loadCompositionProposals();
      await useSessionStore.getState().loadCompositionProposals();
      finishOld([]);
      await oldRead;
      expect(useSessionStore.getState().compositionProposals).toEqual([proposal]);
    });

    it("keeps a new compose proposal when older rejection hydration finishes", async () => {
      const api = await import("@/api/client");
      const original = makeCompositionProposal();
      const receipt = makeCompositionProposal({ status: "rejected" });
      const fresh = makeCompositionProposal({ id: "new-proposal", tool_call_id: "new-call" });
      let finishRead!: (proposals: CompositionProposal[]) => void;
      vi.mocked(api.rejectCompositionProposal).mockResolvedValue(receipt);
      vi.mocked(api.fetchCompositionProposals).mockImplementationOnce(() => new Promise((resolve) => { finishRead = resolve; }));
      vi.mocked(api.sendMessage).mockResolvedValue({
        message: { id: "asst-new", session_id: "20000000-0000-4000-8000-000000000001", role: "assistant", content: "Review replacement", tool_calls: null, created_at: "2026-09-22T00:00:00Z" },
        state: null, proposals: [fresh],
      });
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001", compositionProposals: [original], messages: [] });
      const rejection = useSessionStore.getState().rejectProposal(original.id);
      await vi.waitFor(() => expect(finishRead).toBeTypeOf("function"));
      await useSessionStore.getState().sendMessage("Revise the proposal");
      expect(useSessionStore.getState().compositionProposals).toContainEqual(fresh);
      finishRead([receipt]);
      await rejection;
      expect(useSessionStore.getState().compositionProposals).toContainEqual(fresh);
      expect(useSessionStore.getState().compositionProposals).toContainEqual(receipt);
    });

    it("keeps a decision receipt when an older list read finishes afterward", async () => {
      const api = await import("@/api/client");
      const pending = makeCompositionProposal();
      const receipt = makeCompositionProposal({ status: "rejected" });
      let finishRead!: (proposals: CompositionProposal[]) => void;
      vi.mocked(api.fetchCompositionProposals)
        .mockImplementationOnce(() => new Promise((resolve) => { finishRead = resolve; }))
        .mockResolvedValueOnce([receipt]);
      vi.mocked(api.rejectCompositionProposal).mockResolvedValue(receipt);
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001", compositionProposals: [pending] });
      const oldRead = useSessionStore.getState().loadCompositionProposals();
      await useSessionStore.getState().rejectProposal(pending.id);
      finishRead([pending]);
      await oldRead;
      expect(useSessionStore.getState().compositionProposals).toEqual([receipt]);
      expect(api.rejectCompositionProposal).toHaveBeenCalledTimes(1);
    });

    it.each(["accept", "reject"] as const)("keeps a pending proposal actionable after %s contention", async (action) => {
      const api = await import("@/api/client");
      const proposal = makeCompositionProposal();
      vi.mocked(action === "accept" ? api.acceptCompositionProposal : api.rejectCompositionProposal)
        .mockRejectedValue({ status: 409, detail: "Session operation is already active" });
      vi.mocked(api.fetchCompositionProposals).mockResolvedValue([proposal]);
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001", compositionProposals: [proposal] });
      await useSessionStore.getState()[action === "accept" ? "acceptProposal" : "rejectProposal"](proposal.id);
      expect(useSessionStore.getState().staleProposalIds).not.toContain(proposal.id);
      expect(useSessionStore.getState().compositionProposals).toEqual([proposal]);
      expect(useSessionStore.getState().error).toBe("Session operation is already active");
    });

    it.each(["accept", "reject"] as const)("retains a successful %s receipt when hydration fails", async (action) => {
      const api = await import("@/api/client");
      const proposal = makeCompositionProposal();
      const receipt = makeCompositionProposal({ status: action === "accept" ? "committed" : "rejected" });
      vi.mocked(action === "accept" ? api.acceptCompositionProposal : api.rejectCompositionProposal).mockResolvedValue(receipt);
      vi.mocked(api.fetchCompositionState).mockResolvedValue(null);
      vi.mocked(api.fetchCompositionProposals).mockRejectedValue(new TypeError("Failed to fetch"));
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001", compositionProposals: [proposal], compositionState: makeCompositionState(1) });
      await useSessionStore.getState()[action === "accept" ? "acceptProposal" : "rejectProposal"](proposal.id);
      expect(useSessionStore.getState().compositionProposals).toEqual([receipt]);
      expect(useSessionStore.getState().error).toMatch(/Proposal (accepted|rejected), but/);
      expect(useSessionStore.getState().proposalActionPendingIds).toEqual([]);
      if (action === "accept") {
        expect(useSessionStore.getState().compositionStateLoaded).toBe(false);
        expect(useSessionStore.getState().compositionState).toBeNull();
      }
      vi.mocked(api.fetchCompositionProposals).mockResolvedValue([receipt]);
      await useSessionStore.getState().loadCompositionProposals("20000000-0000-4000-8000-000000000001");
      expect(vi.mocked(action === "accept" ? api.acceptCompositionProposal : api.rejectCompositionProposal)).toHaveBeenCalledTimes(1);
      expect(useSessionStore.getState().compositionProposals).toEqual([receipt]);
    });

    it.each(["accept", "reject"] as const)("keeps the %s receipt over a stale pending refresh", async (action) => {
      const api = await import("@/api/client");
      const pending = makeCompositionProposal();
      const receipt = makeCompositionProposal({ status: action === "accept" ? "committed" : "rejected" });
      vi.mocked(action === "accept" ? api.acceptCompositionProposal : api.rejectCompositionProposal).mockResolvedValue(receipt);
      vi.mocked(api.fetchCompositionState).mockResolvedValue(makeCompositionState(2));
      vi.mocked(api.fetchCompositionProposals).mockResolvedValue([pending]);
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001", compositionProposals: [pending] });
      await useSessionStore.getState()[action === "accept" ? "acceptProposal" : "rejectProposal"](pending.id);
      expect(useSessionStore.getState().compositionProposals).toEqual([receipt]);
      expect(useSessionStore.getState().error).toBeNull();
    });

    it.each(["accept", "reject"] as const)("preserves %s conflict detail when reconciliation fails", async (action) => {
      const api = await import("@/api/client");
      const pending = makeCompositionProposal();
      vi.mocked(action === "accept" ? api.acceptCompositionProposal : api.rejectCompositionProposal).mockRejectedValue({ status: 409, detail: "Session operation is already active" });
      vi.mocked(api.fetchCompositionProposals).mockRejectedValue(new TypeError("Failed to fetch"));
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001", compositionProposals: [pending] });
      await useSessionStore.getState()[action === "accept" ? "acceptProposal" : "rejectProposal"](pending.id);
      expect(useSessionStore.getState().compositionProposals).toEqual([pending]);
      expect(useSessionStore.getState().staleProposalIds).toEqual([]);
      expect(useSessionStore.getState().error).toBe("Session operation is already active");
    });

    it("loads proposals when selecting a session", async () => {
      const apiClient = await import("@/api/client");
      const proposal = makeCompositionProposal();
      (apiClient.fetchMessages as ReturnType<typeof vi.fn>).mockResolvedValue([]);
      (apiClient.fetchCompositionState as ReturnType<typeof vi.fn>).mockResolvedValue(
        null,
      );
      (
        apiClient.fetchCompositionProposals as ReturnType<typeof vi.fn>
      ).mockResolvedValue([proposal]);
      (
        apiClient.fetchComposerPreferences as ReturnType<typeof vi.fn>
      ).mockResolvedValue({
        session_id: "20000000-0000-4000-8000-000000000001",
        trust_mode: "explicit_approve",
        density_default: "high",
        interpretation_review_disabled: false,
        updated_at: "2026-05-14T00:00:00Z",
      });

      await useSessionStore.getState().selectSession("20000000-0000-4000-8000-000000000001");

      expect(useSessionStore.getState().compositionProposals).toEqual([
        proposal,
      ]);
      expect(useSessionStore.getState().composerPreferences?.trust_mode).toBe(
        "explicit_approve",
      );
    });

    it("appends proposals returned by sendMessage without waiting for a session reload", async () => {
      const apiClient = await import("@/api/client");
      const proposal = makeCompositionProposal();
      (apiClient.sendMessage as ReturnType<typeof vi.fn>).mockResolvedValue({
        message: {
          id: "asst-1",
          session_id: "20000000-0000-4000-8000-000000000001",
          role: "assistant",
          content: "Review this proposed change.",
          tool_calls: null,
          created_at: "2026-05-14T00:00:01Z",
        },
        state: null,
        proposals: [proposal],
      });

      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001", messages: [] });
      await useSessionStore.getState().sendMessage("build it");

      expect(useSessionStore.getState().compositionProposals).toEqual([
        proposal,
      ]);
    });

    it("marks stale proposals after accept returns a stale-state conflict", async () => {
      const apiClient = await import("@/api/client");
      (
        apiClient.acceptCompositionProposal as ReturnType<typeof vi.fn>
      ).mockRejectedValue(Object.assign(new Error("stale"), { status: 409 }));
      (
        apiClient.fetchCompositionProposals as ReturnType<typeof vi.fn>
      ).mockResolvedValue([]);

      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001" });
      await useSessionStore.getState().acceptProposal("proposal-1");

      expect(useSessionStore.getState().staleProposalIds).toContain(
        "proposal-1",
      );
    });

    it("echoes canonical pipeline draft hashes when accepting", async () => {
      const apiClient = await import("@/api/client");
      const proposal = makeCompositionProposal({
        pipeline_metadata: {
          draft_hash: "d".repeat(64),
          base: { kind: "absent" },
          repair_count: 0,
          skill_hash: "s".repeat(64),
          audit_payload_hash: "p".repeat(64),
          custody_result: "not_required",
        },
      });
      (
        apiClient.acceptCompositionProposal as ReturnType<typeof vi.fn>
      ).mockResolvedValue(proposal);
      (
        apiClient.fetchCompositionState as ReturnType<typeof vi.fn>
      ).mockResolvedValue(null);
      (
        apiClient.fetchCompositionProposals as ReturnType<typeof vi.fn>
      ).mockResolvedValue([]);
      useSessionStore.setState({
        activeSessionId: "20000000-0000-4000-8000-000000000001",
        compositionProposals: [proposal],
      });

      await useSessionStore.getState().acceptProposal(proposal.id);

      expect(apiClient.acceptCompositionProposal).toHaveBeenCalledWith(
        "20000000-0000-4000-8000-000000000001",
        proposal.id,
        proposal.pipeline_metadata?.draft_hash,
      );
    });
  });

  describe("applyResolvedInterpretation", () => {
    it("guards delayed approval publication across activation changes", async () => {
      const api = await import("@/api/client");
      vi.mocked(api.fetchMessages).mockResolvedValue([]);
      vi.mocked(api.fetchCompositionState).mockResolvedValue(makeCompositionState(2));
      vi.mocked(api.fetchCompositionProposals).mockResolvedValue([]);
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "20000000-0000-4000-8000-000000000001", compositionState: makeCompositionState(1) });
      const resolve = createInterpretationResolutionHandler("20000000-0000-4000-8000-000000000001");
      await useSessionStore.getState().selectSession("20000000-0000-4000-8000-000000000002");
      const destination = useSessionStore.getState().compositionState;
      validateMock.mockClear();
      resolve(makeCompositionState(3));
      expect(useSessionStore.getState().compositionState).toBe(destination);
      expect(validateMock).not.toHaveBeenCalled();
      await useSessionStore.getState().selectSession("20000000-0000-4000-8000-000000000001");
      const reactivated = useSessionStore.getState().compositionState;
      validateMock.mockClear();
      resolve(makeCompositionState(3));
      expect(useSessionStore.getState().compositionState).toBe(reactivated);
      expect(validateMock).not.toHaveBeenCalled();
      const currentResolve = createInterpretationResolutionHandler("20000000-0000-4000-8000-000000000001");
      const accepted = makeCompositionState(4);
      currentResolve(accepted);
      expect(useSessionStore.getState().compositionState).toBe(accepted);
      expect(validateMock).toHaveBeenCalledWith("20000000-0000-4000-8000-000000000001");
    });

    it("applies the patched composition state and re-validates so the run-gate can reopen", () => {
      const newState = makeCompositionState(3, ["analyze_colors"]);
      useSessionStore.setState({
        activeSessionId: "20000000-0000-4000-8000-000000000001",
        compositionState: makeCompositionState(2, ["analyze_colors"]),
      });

      useSessionStore.getState().applyResolvedInterpretation(newState);

      // Display sync: the resolved interpretation's patched pipeline is shown.
      expect(useSessionStore.getState().compositionState).toBe(newState);
      // Gate clearing: an explicit re-validate runs (the auto-validate
      // subscription only fires on a version bump, which a resolve can't
      // guarantee).
      expect(validateMock).toHaveBeenCalledWith("20000000-0000-4000-8000-000000000001");
    });

    it("re-validates even when the resolve returns no new state (null)", () => {
      useSessionStore.setState({
        activeSessionId: "20000000-0000-4000-8000-000000000001",
        compositionState: makeCompositionState(2),
      });

      useSessionStore.getState().applyResolvedInterpretation(null);

      // No state to apply, but the gate must still be re-checked.
      expect(useSessionStore.getState().compositionState?.version).toBe(2);
      expect(validateMock).toHaveBeenCalledWith("20000000-0000-4000-8000-000000000001");
    });

    it("is a no-op without an active session", () => {
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: null });

      useSessionStore
        .getState()
        .applyResolvedInterpretation(makeCompositionState(3));

      expect(validateMock).not.toHaveBeenCalled();
    });
  });

  describe("recovery state actions", () => {
    it("applies recovered state locally and clears validation selection and recovery state", () => {
      const recovered = makeCompositionState(2, ["kept"]);
      useSessionStore.setState({
        compositionState: makeCompositionState(1, ["stale"]),
        selectedNodeId: "stale",
        recoveryError: makeRecoveryError(recovered),
        recoveryStartedCompositionVersion: 1,
      });

      const result = useSessionStore.getState().applyRecoveredState();

      const state = useSessionStore.getState();
      expect(result).toEqual({ applied: true, needsConfirmation: false });
      expect(state.compositionState).toBe(recovered);
      expect(state.selectedNodeId).toBeNull();
      expect(state.recoveryError).toBeNull();
      expect(state.recoveryStartedCompositionVersion).toBeNull();
      expect(clearValidationMock).toHaveBeenCalledTimes(1);
    });

    it("refuses to apply a recovered state that was not saved server-side", () => {
      const current = makeCompositionState(1, ["current"]);
      const recovered = { ...makeCompositionState(2, ["recovered"]), id: "" };
      const recoveryError: ComposerRecoveryError = {
        ...makeRecoveryError(recovered),
        partial_state_save_failed: true,
      };
      useSessionStore.setState({
        compositionState: current,
        recoveryError,
        recoveryStartedCompositionVersion: 1,
      });

      const result = useSessionStore.getState().applyRecoveredState();

      const state = useSessionStore.getState();
      expect(result).toEqual({ applied: false, needsConfirmation: false });
      expect(state.compositionState).toBe(current);
      expect(state.recoveryError).toMatchObject(recoveryError);
      expect(state.error).toMatch(/not saved on the server/i);
      expect(clearValidationMock).not.toHaveBeenCalled();
    });

    it("requires confirmation when current version differs from compose-start version", () => {
      const recovered = makeCompositionState(3, ["next"]);
      useSessionStore.setState({
        compositionState: makeCompositionState(2, ["current"]),
        recoveryError: makeRecoveryError(recovered),
        recoveryStartedCompositionVersion: 1,
      });

      const first = useSessionStore.getState().applyRecoveredState();
      expect(first).toEqual({ applied: false, needsConfirmation: true });
      expect(useSessionStore.getState().compositionState?.version).toBe(2);

      const confirmed = useSessionStore
        .getState()
        .applyRecoveredState({ confirmed: true });
      expect(confirmed).toEqual({ applied: true, needsConfirmation: false });
      expect(useSessionStore.getState().compositionState).toBe(recovered);
    });

    it("discardRecovery closes local recovery state without API calls or state mutation", async () => {
      const apiClient = await import("@/api/client");
      const current = makeCompositionState(4, ["current"]);
      useSessionStore.setState({
        compositionState: current,
        recoveryError: makeRecoveryError(makeCompositionState(5)),
        recoveryStartedCompositionVersion: 4,
      });

      useSessionStore.getState().discardRecovery();
      useSessionStore.getState().discardRecovery();

      expect(useSessionStore.getState().compositionState).toBe(current);
      expect(useSessionStore.getState().recoveryError).toBeNull();
      expect(useSessionStore.getState().recoveryStartedCompositionVersion).toBeNull();
      expect(apiClient.sendMessage).not.toHaveBeenCalled();
      expect(apiClient.recompose).not.toHaveBeenCalled();
      expect(apiClient.fetchMessages).not.toHaveBeenCalled();
    });

    it("replaces stale recovery state with the newest recovery failure", async () => {
      const { sendMessage: mockSendMessage, fetchMessages, fetchCompositionState, fetchCompositionProposals } = await import("@/api/client");
      vi.mocked(fetchMessages).mockResolvedValue([]);
      vi.mocked(fetchCompositionState).mockResolvedValueOnce(makeCompositionState(2)).mockResolvedValueOnce(makeCompositionState(3));
      vi.mocked(fetchCompositionProposals).mockResolvedValue([]);
      const first = makeRecoveryError(makeCompositionState(2));
      const second = makeRecoveryError(makeCompositionState(3));
      (mockSendMessage as ReturnType<typeof vi.fn>)
        .mockRejectedValueOnce(first)
        .mockRejectedValueOnce(second);

      useSessionStore.setState({
        activeSessionId: "20000000-0000-4000-8000-000000000001",
        compositionState: makeCompositionState(1),
      });
      await useSessionStore.getState().sendMessage("first");
      useSessionStore.getState().discardRecovery();
      await useSessionStore.getState().sendMessage("second");

      expect(useSessionStore.getState().recoveryError).toMatchObject(second);
      expect(useSessionStore.getState().recoveryStartedCompositionVersion).toBe(3);
    });

    it("successful compose while recovery is open makes later apply require confirmation", async () => {
      const { sendMessage: mockSendMessage } = await import("@/api/client");
      const recovered = makeCompositionState(2);
      useSessionStore.setState({
        activeSessionId: "20000000-0000-4000-8000-000000000001",
        compositionState: makeCompositionState(1),
        recoveryError: makeRecoveryError(recovered),
        recoveryStartedCompositionVersion: 1,
      });
      const assistantMessage: ChatMessage = {
        id: "assistant-2",
        session_id: "20000000-0000-4000-8000-000000000001",
        role: "assistant",
        content: "new success",
        tool_calls: null,
        created_at: new Date().toISOString(),
      };
      (mockSendMessage as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
        message: assistantMessage,
        state: makeCompositionState(3),
      });

      await useSessionStore.getState().sendMessage("new work");
      const result = useSessionStore.getState().applyRecoveredState();

      expect(result).toEqual({ applied: false, needsConfirmation: true });
      expect(useSessionStore.getState().compositionState?.version).toBe(3);
    });

    it("clears recovery state on session transitions and reset", async () => {
      const apiClient = await import("@/api/client");
      const session = {
        id: "20000000-0000-4000-8000-000000000007",
        title: "New",
        created_at: "2026-05-14T00:00:00Z",
        updated_at: "2026-05-14T00:00:00Z",
      };
      (apiClient.createSession as ReturnType<typeof vi.fn>).mockResolvedValue(
        session,
      );
      (apiClient.archiveSession as ReturnType<typeof vi.fn>).mockResolvedValue(
        undefined,
      );
      (apiClient.fetchMessages as ReturnType<typeof vi.fn>).mockResolvedValue(
        [],
      );
      (apiClient.fetchCompositionState as ReturnType<typeof vi.fn>).mockResolvedValue(
        makeCompositionState(1),
      );
      (apiClient.fetchCompositionProposals as ReturnType<typeof vi.fn>).mockResolvedValue([]);
      (apiClient.fetchComposerPreferences as ReturnType<typeof vi.fn>).mockResolvedValue(null);
      (apiClient.forkFromMessage as ReturnType<typeof vi.fn>).mockResolvedValue({
        session_id: "00000000-0000-4000-8000-000000000702",
      });
      (apiClient.fetchSessions as ReturnType<typeof vi.fn>).mockResolvedValue([
        { ...session, id: "00000000-0000-4000-8000-000000000702" },
      ]);

      const seedRecovery = () =>
        useSessionStore.setState({
          activeSessionId: "00000000-0000-4000-8000-000000000701",
          recoveryError: makeRecoveryError(),
          recoveryStartedCompositionVersion: 1,
        });

      seedRecovery();
      await useSessionStore.getState().selectSession("20000000-0000-4000-8000-000000000002");
      expect(useSessionStore.getState().recoveryError).toBeNull();

      seedRecovery();
      await useSessionStore.getState().createSession();
      expect(useSessionStore.getState().recoveryError).toBeNull();

      seedRecovery();
      await useSessionStore.getState().archiveSession("00000000-0000-4000-8000-000000000701");
      expect(useSessionStore.getState().recoveryError).toBeNull();

      seedRecovery();
      await useSessionStore.getState().forkFromMessage("message-1", "fork");
      expect(useSessionStore.getState().recoveryError).toBeNull();

      seedRecovery();
      useSessionStore.getState().reset();
      expect(useSessionStore.getState().recoveryError).toBeNull();
      expect(useSessionStore.getState().recoveryStartedCompositionVersion).toBeNull();
    });
  });

  describe("selectSession stale session handling", () => {
    it("drops stale selectSession responses after the active session changes", async () => {
      const apiClient = await import("@/api/client");
      const aMessages = deferred<ChatMessage[]>();
      const aState = deferred<CompositionState | null>();
      const aProposals = deferred<CompositionProposal[]>();
      const aPreferences = deferred<ComposerPreferences | null>();
      const bMessages = deferred<ChatMessage[]>();
      const bState = deferred<CompositionState | null>();
      const bProposals = deferred<CompositionProposal[]>();
      const bPreferences = deferred<ComposerPreferences | null>();
      const messagePromises = new Map([
        ["20000000-0000-4000-8000-000000000008", aMessages.promise],
        ["20000000-0000-4000-8000-000000000009", bMessages.promise],
      ]);
      const statePromises = new Map([
        ["20000000-0000-4000-8000-000000000008", aState.promise],
        ["20000000-0000-4000-8000-000000000009", bState.promise],
      ]);
      const proposalPromises = new Map([
        ["20000000-0000-4000-8000-000000000008", aProposals.promise],
        ["20000000-0000-4000-8000-000000000009", bProposals.promise],
      ]);
      const preferencePromises = new Map([
        ["20000000-0000-4000-8000-000000000008", aPreferences.promise],
        ["20000000-0000-4000-8000-000000000009", bPreferences.promise],
      ]);
      const lookup = <T,>(promises: Map<string, Promise<T>>, id: string) => {
        const promise = promises.get(id);
        if (!promise) {
          throw new Error(`unexpected session ${id}`);
        }
        return promise;
      };
      (apiClient.fetchMessages as ReturnType<typeof vi.fn>).mockImplementation(
        (id: string) => lookup(messagePromises, id),
      );
      (
        apiClient.fetchCompositionState as ReturnType<typeof vi.fn>
      ).mockImplementation((id: string) => lookup(statePromises, id));
      (
        apiClient.fetchCompositionProposals as ReturnType<typeof vi.fn>
      ).mockImplementation((id: string) => lookup(proposalPromises, id));
      (
        apiClient.fetchComposerPreferences as ReturnType<typeof vi.fn>
      ).mockImplementation((id: string) => lookup(preferencePromises, id));

      const selectA = useSessionStore.getState().selectSession("20000000-0000-4000-8000-000000000008");
      const selectB = useSessionStore.getState().selectSession("20000000-0000-4000-8000-000000000009");

      const bMessage: ChatMessage = {
        id: "b-message",
        session_id: "20000000-0000-4000-8000-000000000009",
        role: "user",
        content: "current",
        tool_calls: null,
        created_at: "2026-05-14T00:00:00Z",
      };
      const bCompositionState = makeCompositionState(2, ["b-node"]);
      bMessages.resolve([bMessage]);
      bState.resolve(bCompositionState);
      bProposals.resolve([
        makeCompositionProposal({ id: "proposal-b", session_id: "20000000-0000-4000-8000-000000000009" }),
      ]);
      bPreferences.resolve({
        session_id: "20000000-0000-4000-8000-000000000009",
        trust_mode: "explicit_approve",
        density_default: "high",
        interpretation_review_disabled: false,
        updated_at: "2026-05-14T00:00:00Z",
      });
      await selectB;

      aMessages.resolve([
        {
          id: "a-message",
          session_id: "20000000-0000-4000-8000-000000000008",
          role: "user",
          content: "stale",
          tool_calls: null,
          created_at: "2026-05-14T00:00:00Z",
        },
      ]);
      aState.resolve(makeCompositionState(1, ["a-node"]));
      aProposals.resolve([
        makeCompositionProposal({ id: "proposal-a", session_id: "20000000-0000-4000-8000-000000000008" }),
      ]);
      aPreferences.resolve({
        session_id: "20000000-0000-4000-8000-000000000008",
        trust_mode: "explicit_approve",
        density_default: "high",
        interpretation_review_disabled: false,
        updated_at: "2026-05-14T00:00:00Z",
      });
      await selectA;

      const state = useSessionStore.getState();
      expect(state.activeSessionId).toBe("20000000-0000-4000-8000-000000000009");
      expect(state.messages).toEqual([expect.objectContaining({
        ...bMessage,
      })]);
      expect(state.compositionState).toBe(bCompositionState);
      expect(state.compositionProposals).toEqual([
        expect.objectContaining({ id: "proposal-b", session_id: "20000000-0000-4000-8000-000000000009" }),
      ]);
      expect(state.composerPreferences?.session_id).toBe("20000000-0000-4000-8000-000000000009");
    });

    it("clears stale active session when selected session no longer exists", async () => {
      const apiClient = await import("@/api/client");
      (apiClient.fetchMessages as ReturnType<typeof vi.fn>).mockRejectedValueOnce({
        status: 404,
        detail: "Session not found",
      });
      (apiClient.fetchCompositionState as ReturnType<typeof vi.fn>).mockRejectedValueOnce({
        status: 404,
        detail: "Session not found",
      });
      (
        apiClient.fetchCompositionProposals as ReturnType<typeof vi.fn>
      ).mockRejectedValueOnce({
        status: 404,
        detail: "Session not found",
      });
      (
        apiClient.fetchComposerPreferences as ReturnType<typeof vi.fn>
      ).mockRejectedValueOnce({
        status: 404,
        detail: "Session not found",
      });

      useSessionStore.setState({
        sessions: [],
        activeSessionId: null,
        messages: [
          {
            id: "old-message",
            session_id: "20000000-0000-4000-8000-000000000006",
            role: "user",
            content: "stale",
            tool_calls: null,
            created_at: "2026-05-14T00:00:00Z",
          },
        ],
        compositionState: makeCompositionState(7),
        error: null,
      });

      await useSessionStore.getState().selectSession("missing-session");

      const state = useSessionStore.getState();
      expect(state.activeSessionId).toBeNull();
      expect(state.messages).toEqual([]);
      expect(state.compositionState).toBeNull();
      expect(state.compositionProposals).toEqual([]);
      expect(state.composerPreferences).toBeNull();
      expect(state.error).toBeNull();
    });
  });


  describe("retryMessage abort handling", () => {
    it("drops stale retryMessage responses after the active session changes", async () => {
      const { recompose: mockRecompose } = await import("@/api/client");
      const retryDeferred = deferred<{
        message: ChatMessage;
        state: CompositionState | null;
        proposals?: CompositionProposal[];
      }>();
      (mockRecompose as ReturnType<typeof vi.fn>).mockReturnValueOnce(
        retryDeferred.promise,
      );
      const retriedMessage: ChatMessage = {
        id: "a-user",
        session_id: "20000000-0000-4000-8000-000000000008",
        role: "user",
        content: "retry this",
        tool_calls: null,
        created_at: "2026-05-14T00:00:00Z",
      };
      useSessionStore.setState({
        activeSessionId: "20000000-0000-4000-8000-000000000008",
        messages: [retriedMessage],
        compositionState: makeCompositionState(1, ["a-start"]),
      });
      const retryPromise = useSessionStore
        .getState()
        .retryMessage("a-user");
      await Promise.resolve();

      const currentMessage: ChatMessage = {
        id: "b-message",
        session_id: "20000000-0000-4000-8000-000000000009",
        role: "user",
        content: "current session",
        tool_calls: null,
        created_at: "2026-05-14T00:00:00Z",
      };
      const currentState = makeCompositionState(20, ["b-node"]);
      useSessionStore.setState({
        activeSessionId: "20000000-0000-4000-8000-000000000009",
        messages: [currentMessage],
        compositionState: currentState,
        compositionProposals: [
          makeCompositionProposal({ id: "proposal-b", session_id: "20000000-0000-4000-8000-000000000009" }),
        ],
        isComposing: false,
      });

      retryDeferred.resolve({
        message: {
          id: "a-assistant",
          session_id: "20000000-0000-4000-8000-000000000008",
          role: "assistant",
          content: "stale retry response",
          tool_calls: null,
          created_at: "2026-05-14T00:00:01Z",
        },
        state: makeCompositionState(2, ["a-node"]),
        proposals: [
          makeCompositionProposal({ id: "proposal-a", session_id: "20000000-0000-4000-8000-000000000008" }),
        ],
      });
      await retryPromise;

      const state = useSessionStore.getState();
      expect(state.activeSessionId).toBe("20000000-0000-4000-8000-000000000009");
      expect(state.messages).toEqual([currentMessage]);
      expect(state.compositionState).toBe(currentState);
      expect(state.compositionProposals).toEqual([
        expect.objectContaining({ id: "proposal-b", session_id: "20000000-0000-4000-8000-000000000009" }),
      ]);
      expect(state.error).toBeNull();
    });

    it("maps a client-side AbortError to the compose-timeout copy", async () => {
      await proveDetachedOperation("compose_recompose");
    });

    it("maps the raw compose_timeout abort reason to the compose-timeout copy (elspeth-475647c47a)", async () => {
      // Durable replacement: the exact operation terminal owns settlement.
      await proveDetachedOperation();
    });

    it("surfaces the timeout copy and the salvaged draft on the recompose path (R2-F9)", async () => {
      // The 422 handler is shared by send_message and recompose, so the
      // store's two catch arms must not drift apart.
      const { recompose: mockRecompose } = await import("@/api/client");
      const timeoutError = makeTimeoutError();
      (mockRecompose as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
        timeoutError,
      );

      const userMessage: ChatMessage = {
        id: "30000000-0000-4000-8000-000000000003",
        session_id: "20000000-0000-4000-8000-000000000001",
        role: "user",
        content: "hello",
        tool_calls: null,
        created_at: new Date().toISOString(),
      };
      useSessionStore.setState({
        activeSessionId: "20000000-0000-4000-8000-000000000001",
        messages: [userMessage],
        compositionState: makeCompositionState(5),
      });

      await useSessionStore.getState().retryMessage("30000000-0000-4000-8000-000000000003");

      const state = useSessionStore.getState();
      expect(state.error).toContain(
        "ELSPETH ran out of time (240s). Your partial pipeline was saved — continue from it or retry.",
      );
      expect(state.compositionState).toEqual(timeoutError.partial_state);
      expect(state.recoveryError).toMatchObject(timeoutError);
      expect(state.recoveryStartedCompositionVersion).toBe(6);
    });

    it("resyncs durable server state after an aborted retry (elspeth-06a23adfcc)", async () => {
      await proveStoppedOperation(true, "compose_recompose");
    });
  });

  describe("reset", () => {
    it("restores initial state", () => {
      useSessionStore.setState({
        activeSessionId: "20000000-0000-4000-8000-000000000001",
        isComposing: true,
        error: "some error",
      });

      useSessionStore.getState().reset();

      const state = useSessionStore.getState();
      expect(state.activeSessionId).toBeNull();
      expect(state.isComposing).toBe(false);
      expect(state.error).toBeNull();
    });
  });

  describe("resetForTutorialSession", () => {
    it("binds activeSessionId and hydrates every field a stale session could leave behind", () => {
      // Dirty every field resetForTutorialSession is responsible for
      // clearing after another session was active.
      useSessionStore.setState({
        activeSessionId: "20000000-0000-4000-8000-000000000006",
        messages: [{ id: "old-message" } as unknown as ChatMessage],
        compositionState: makeCompositionState(1),
        compositionProposals: [makeCompositionProposal()],
        composerPreferences: {
          session_id: "20000000-0000-4000-8000-000000000006",
          trust_mode: "explicit_approve",
          density_default: "medium",
          interpretation_review_disabled: false,
          updated_at: "2026-05-15T00:00:00Z",
        },
        staleProposalIds: ["proposal-1"],
        proposalActionPendingIds: ["proposal-1"],
        composerProgress: {} as ComposerProgressSnapshot,
        stateVersions: [{ id: "10000000-0000-4000-8000-000000000001", version: 1 } as never],
        isComposing: true,
        error: "some stale error",
        selectedNodeId: "old-node",
        recoveryError: makeRecoveryError(),
        recoveryStartedCompositionVersion: 3,
      });

      useSessionStore.getState().resetForTutorialSession("tutorial-session-1");

      const state = useSessionStore.getState();
      expect(state.activeSessionId).toBe("tutorial-session-1");
      expect(state.messages).toEqual([]);
      expect(state.compositionState).toBeNull();
      expect(state.compositionProposals).toEqual([]);
      expect(state.composerPreferences).toBeNull();
      expect(state.staleProposalIds).toEqual([]);
      expect(state.proposalActionPendingIds).toEqual([]);
      expect(state.composerProgress).toBeNull();
      expect(state.stateVersions).toEqual([]);
      expect(state.isComposing).toBe(false);
      expect(state.error).toBeNull();
      expect(state.selectedNodeId).toBeNull();
      expect(state.recoveryError).toBeNull();
      expect(state.recoveryStartedCompositionVersion).toBeNull();
    });

    it("does not touch fields it is not responsible for (sessions list)", () => {
      const sessions = [
        {
          id: "20000000-0000-4000-8000-000000000001",
          title: "Existing",
          created_at: "2026-05-14T00:00:00Z",
          updated_at: "2026-05-14T00:00:00Z",
        },
      ];
      useSessionStore.setState({ sessions });

      useSessionStore.getState().resetForTutorialSession("tutorial-session-2");

      expect(useSessionStore.getState().sessions).toBe(sessions);
    });
  });

  describe("unbindMissingSession", () => {
    it("releases the binding and session-scoped fields when the dead id is still active", () => {
      // The dead-resume recovery path: resetForTutorialSession bound the
      // (dead) resume id before the server 404'd it. Consumers keyed on
      // activeSessionId (InlineRunResults' run list) would keep polling the
      // corpse unless recovery releases the binding.
      useSessionStore.setState({
        activeSessionId: "dead-session",
        messages: [{ id: "stale-message" } as unknown as ChatMessage],
        isComposing: true,
        error: "stale error",
      });

      useSessionStore.getState().unbindMissingSession("dead-session");

      const state = useSessionStore.getState();
      expect(state.activeSessionId).toBeNull();
      expect(state.messages).toEqual([]);
      expect(state.isComposing).toBe(false);
      expect(state.error).toBeNull();
    });

    it("is a no-op when a different session has since been bound (recovery races a re-bind)", () => {
      useSessionStore.setState({
        activeSessionId: "fresh-session",
        messages: [{ id: "fresh-message" } as unknown as ChatMessage],
      });

      useSessionStore.getState().unbindMissingSession("dead-session");

      const state = useSessionStore.getState();
      expect(state.activeSessionId).toBe("fresh-session");
      expect(state.messages).toHaveLength(1);
    });
  });



  describe("session fork retry custody", () => {
    const parentId = "00000000-0000-4000-8000-000000000701";
    const childId = "00000000-0000-4000-8000-000000000702";
    const child = {
      id: childId,
      title: "Fork",
      created_at: "2026-07-19T00:00:00Z",
      updated_at: "2026-07-19T00:00:00Z",
      forked_from_session_id: parentId,
    };

    it("reuses the exact operation id after an ambiguous POST failure", async () => {
      const apiClient = await import("@/api/client");
      (apiClient.forkFromMessage as ReturnType<typeof vi.fn>)
        .mockRejectedValueOnce(new TypeError("Failed to fetch"))
        .mockRejectedValueOnce(new TypeError("Failed to fetch"));
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: parentId });

      await useSessionStore.getState().forkFromMessage("message-1", "edited");
      await useSessionStore.getState().forkFromMessage("message-1", "edited");

      const calls = (apiClient.forkFromMessage as ReturnType<typeof vi.fn>).mock.calls;
      expect(calls[0]?.[1]).toBe(calls[1]?.[1]);
      expect(calls[0]?.slice(2)).toEqual(["message-1", "edited"]);
    });

    it("reports conflicting fork content without rejecting or sending another request", async () => {
      const apiClient = await import("@/api/client");
      (apiClient.forkFromMessage as ReturnType<typeof vi.fn>)
        .mockRejectedValue(new TypeError("response lost"));
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: parentId });

      await useSessionStore.getState().forkFromMessage("message-1", "first edit");
      await expect(
        useSessionStore.getState().forkFromMessage("message-1", "different edit"),
      ).resolves.toBeUndefined();

      expect(apiClient.forkFromMessage).toHaveBeenCalledTimes(1);
      expect(useSessionStore.getState().isComposing).toBe(false);
      expect(useSessionStore.getState().error).toMatch(/unsettled/i);

      await useSessionStore.getState().forkFromMessage("message-1", "first edit");
      expect(apiClient.forkFromMessage).toHaveBeenCalledTimes(2);
      const calls = (apiClient.forkFromMessage as ReturnType<typeof vi.fn>).mock.calls;
      expect(calls[1]?.[1]).toBe(calls[0]?.[1]);
    });

    it("reuses the exact operation id after a 5xx POST failure", async () => {
      const apiClient = await import("@/api/client");
      (apiClient.forkFromMessage as ReturnType<typeof vi.fn>)
        .mockRejectedValueOnce({ status: 503 })
        .mockRejectedValueOnce({ status: 503 });
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: parentId });

      await useSessionStore.getState().forkFromMessage("message-1", "edited");
      await useSessionStore.getState().forkFromMessage("message-1", "edited");

      const calls = (apiClient.forkFromMessage as ReturnType<typeof vi.fn>).mock.calls;
      expect(calls[0]?.[1]).toBe(calls[1]?.[1]);
    });

    it("retires operation custody after a definitive 4xx failure", async () => {
      const apiClient = await import("@/api/client");
      (apiClient.forkFromMessage as ReturnType<typeof vi.fn>)
        .mockRejectedValueOnce({ status: 409 })
        .mockRejectedValueOnce({ status: 409 });
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: parentId });

      await useSessionStore.getState().forkFromMessage("message-1", "edited");
      await useSessionStore.getState().forkFromMessage("message-1", "edited");

      const calls = (apiClient.forkFromMessage as ReturnType<typeof vi.fn>).mock.calls;
      expect(calls[0]?.[1]).not.toBe(calls[1]?.[1]);
    });

    it("keeps the operation id after locator success until authoritative child refresh succeeds", async () => {
      const apiClient = await import("@/api/client");
      (apiClient.forkFromMessage as ReturnType<typeof vi.fn>).mockResolvedValue({
        session_id: childId,
      });
      (apiClient.fetchSessions as ReturnType<typeof vi.fn>)
        .mockRejectedValueOnce(new TypeError("refresh failed"))
        .mockResolvedValueOnce([child]);
      (apiClient.fetchMessages as ReturnType<typeof vi.fn>).mockResolvedValue([]);
      (apiClient.fetchCompositionState as ReturnType<typeof vi.fn>).mockResolvedValue(null);
      (apiClient.fetchCompositionProposals as ReturnType<typeof vi.fn>).mockResolvedValue([]);
      (apiClient.fetchComposerPreferences as ReturnType<typeof vi.fn>).mockResolvedValue(null);
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: parentId });

      await useSessionStore.getState().forkFromMessage("message-1", "edited");
      expect(useSessionStore.getState().activeSessionId).toBe(parentId);
      await useSessionStore.getState().forkFromMessage("message-1", "edited");

      const calls = (apiClient.forkFromMessage as ReturnType<typeof vi.fn>).mock.calls;
      expect(calls[0]?.[1]).toBe(calls[1]?.[1]);
      expect(useSessionStore.getState().activeSessionId).toBe(childId);
      expect(useSessionStore.getState().sessions).toEqual([child]);
      expect(useSessionStore.getState().error).toBeNull();
    });

    it("keeps parent active and reuses the operation id when a child read fails after the locator", async () => {
      const apiClient = await import("@/api/client");
      (apiClient.forkFromMessage as ReturnType<typeof vi.fn>).mockResolvedValue({ session_id: childId });
      (apiClient.fetchSessions as ReturnType<typeof vi.fn>).mockResolvedValue([child]);
      (apiClient.fetchMessages as ReturnType<typeof vi.fn>).mockResolvedValue([]);
      (apiClient.fetchCompositionState as ReturnType<typeof vi.fn>)
        .mockRejectedValueOnce(new TypeError("child state failed"))
        .mockResolvedValueOnce(null);
      (apiClient.fetchCompositionProposals as ReturnType<typeof vi.fn>).mockResolvedValue([]);
      (apiClient.fetchComposerPreferences as ReturnType<typeof vi.fn>).mockResolvedValue(null);
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: parentId });

      await useSessionStore.getState().forkFromMessage("message-1", "edited");
      expect(useSessionStore.getState().activeSessionId).toBe(parentId);
      await useSessionStore.getState().forkFromMessage("message-1", "edited");

      const calls = (apiClient.forkFromMessage as ReturnType<typeof vi.fn>).mock.calls;
      expect(calls[0]?.[1]).toBe(calls[1]?.[1]);
      expect(useSessionStore.getState().activeSessionId).toBe(childId);
    });


    it("retains retry custody for malformed 2xx fork responses", async () => {
      const apiClient = await import("@/api/client");
      (apiClient.forkFromMessage as ReturnType<typeof vi.fn>)
        .mockRejectedValueOnce({ committedSuccessResponse: true })
        .mockRejectedValueOnce({ committedSuccessResponse: true });
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: parentId });

      await useSessionStore.getState().forkFromMessage("message-1", "edited");
      await useSessionStore.getState().forkFromMessage("message-1", "edited");

      const calls = (apiClient.forkFromMessage as ReturnType<typeof vi.fn>).mock.calls;
      expect(calls[0]?.[1]).toBe(calls[1]?.[1]);
    });

    it("publishes the child without clobbering intervening rename, create, or archive mutations", async () => {
      const apiClient = await import("@/api/client");
      const delayedSessions = deferred<Array<Record<string, unknown>>>();
      (apiClient.forkFromMessage as ReturnType<typeof vi.fn>).mockResolvedValue({ session_id: childId });
      (apiClient.fetchSessions as ReturnType<typeof vi.fn>).mockReturnValue(delayedSessions.promise);
      (apiClient.fetchMessages as ReturnType<typeof vi.fn>).mockResolvedValue([]);
      (apiClient.fetchCompositionState as ReturnType<typeof vi.fn>).mockResolvedValue(null);
      (apiClient.fetchCompositionProposals as ReturnType<typeof vi.fn>).mockResolvedValue([]);
      (apiClient.fetchComposerPreferences as ReturnType<typeof vi.fn>).mockResolvedValue(null);
      const renamedParent = { ...child, id: parentId, title: "Renamed while hydrating" };
      const archivedElsewhere = { ...child, id: "session-archived", title: "Archived elsewhere" };
      const createdElsewhere = { ...child, id: "session-created", title: "Created elsewhere" };
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: parentId, sessions: [renamedParent, archivedElsewhere] });

      const pending = useSessionStore.getState().forkFromMessage("message-1", "edited");
      useSessionStore.setState({
        sessions: [{ ...renamedParent, title: "Newest title" }, createdElsewhere],
      });
      delayedSessions.resolve([
        { ...renamedParent, title: "Stale parent title" },
        archivedElsewhere,
        child,
      ]);
      await pending;

      const state = useSessionStore.getState();
      expect(state.activeSessionId).toBe(childId);
      expect(state.sessions.map((session) => session.id)).toEqual([
        childId,
        parentId,
        "session-created",
      ]);
      expect(state.sessions[1]?.title).toBe("Newest title");
    });

    it("keeps a pre-locator user selection sovereign while publishing the committed child", async () => {
      const apiClient = await import("@/api/client");
      const delayedLocator = deferred<{ session_id: string }>();
      (apiClient.forkFromMessage as ReturnType<typeof vi.fn>)
        .mockReturnValueOnce(delayedLocator.promise)
        .mockRejectedValueOnce(new TypeError("lost retry response"));
      (apiClient.fetchSessions as ReturnType<typeof vi.fn>).mockResolvedValue([child]);
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: parentId });

      const forkPromise = useSessionStore.getState().forkFromMessage("message-1", "edited");
      useSessionStore.setState({
        activeSessionId: "00000000-0000-4000-8000-000000000703",
        isComposing: false,
        error: null,
      });
      delayedLocator.resolve({ session_id: childId });
      await forkPromise;

      expect(useSessionStore.getState().activeSessionId).toBe(
        "00000000-0000-4000-8000-000000000703",
      );
      expect(useSessionStore.getState().sessions).toEqual([child]);
      expect(useSessionStore.getState().error).toBeNull();
      expect(apiClient.fetchMessages).not.toHaveBeenCalled();

      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: parentId });
      await useSessionStore.getState().forkFromMessage("message-1", "edited");
      const calls = (apiClient.forkFromMessage as ReturnType<typeof vi.fn>).mock.calls;
      expect(calls[0]?.[1]).not.toBe(calls[1]?.[1]);
    });

    it("keeps a mid-hydration user selection sovereign while publishing the committed child", async () => {
      const apiClient = await import("@/api/client");
      const delayedMessages = deferred<ChatMessage[]>();
      (apiClient.forkFromMessage as ReturnType<typeof vi.fn>)
        .mockResolvedValueOnce({ session_id: childId })
        .mockRejectedValueOnce(new TypeError("second request"));
      (apiClient.fetchSessions as ReturnType<typeof vi.fn>).mockResolvedValue([child]);
      (apiClient.fetchMessages as ReturnType<typeof vi.fn>).mockReturnValue(delayedMessages.promise);
      (apiClient.fetchCompositionState as ReturnType<typeof vi.fn>).mockResolvedValue(null);
      (apiClient.fetchCompositionProposals as ReturnType<typeof vi.fn>).mockResolvedValue([]);
      (apiClient.fetchComposerPreferences as ReturnType<typeof vi.fn>).mockResolvedValue(null);
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: parentId, sessions: [] });

      const pending = useSessionStore.getState().forkFromMessage("message-1", "edited");
      await vi.waitFor(() => expect(apiClient.fetchMessages).toHaveBeenCalledWith(childId));
      useSessionStore.setState({
        activeSessionId: "00000000-0000-4000-8000-000000000704",
        messages: [{ id: "selected-message" } as ChatMessage],
        isComposing: false,
        error: null,
      });
      delayedMessages.resolve([]);
      await pending;

      const state = useSessionStore.getState();
      expect(state.activeSessionId).toBe("00000000-0000-4000-8000-000000000704");
      expect(state.messages).toEqual([{ id: "selected-message" }]);
      expect(state.sessions).toEqual([child]);

      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: parentId });
      await useSessionStore.getState().forkFromMessage("message-1", "edited");
      const calls = (apiClient.forkFromMessage as ReturnType<typeof vi.fn>).mock.calls;
      expect(calls[0]?.[1]).not.toBe(calls[1]?.[1]);
    });

    it("retains parent-scoped retry custody when child publication fails after a user switch", async () => {
      const apiClient = await import("@/api/client");
      const delayedLocator = deferred<{ session_id: string }>();
      (apiClient.forkFromMessage as ReturnType<typeof vi.fn>)
        .mockReturnValueOnce(delayedLocator.promise)
        .mockRejectedValueOnce(new TypeError("lost retry response"));
      (apiClient.fetchSessions as ReturnType<typeof vi.fn>).mockRejectedValue(
        new TypeError("publication failed"),
      );
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: parentId });

      const pending = useSessionStore.getState().forkFromMessage("message-1", "edited");
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: "selected-session", error: null });
      delayedLocator.resolve({ session_id: childId });
      await pending;

      expect(useSessionStore.getState().activeSessionId).toBe("selected-session");
      expect(useSessionStore.getState().sessions).toEqual([]);
      useSessionStore.setState({ compositionStateLoaded: true, activeSessionId: parentId });
      await useSessionStore.getState().forkFromMessage("message-1", "edited");
      const calls = (apiClient.forkFromMessage as ReturnType<typeof vi.fn>).mock.calls;
      expect(calls[0]?.[1]).toBe(calls[1]?.[1]);
    });
  });
});

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
} {
  let resolve: (value: T) => void = () => undefined;
  let reject: (reason?: unknown) => void = () => undefined;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}
