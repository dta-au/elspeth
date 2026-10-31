import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/api/client";
import { advanceAuthGeneration } from "@/api/authSession";
import { detachComposerObservers } from "@/api/composerOperationObserver";
import { COMPOSER_OPERATION_VERSION_HEADER, setComposerAdmissionVersion } from "@/config/composerProtocol";
import { authenticateComposerCustody, composerStopRequested, findComposerOperationCustody, purgeComposerCustody } from "./composerOperationCustody";
import { useExecutionStore } from "./executionStore";
import { useSessionStore } from "./sessionStore";
import type { SafeRequestRejection } from "@/types";

// Optional enrichment is outside these controls. Submission, HTTP error
// admission, both durable decoders and both observers remain the real code.
vi.mock("@/api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/api/client")>(),
  fetchMessages: vi.fn(), fetchCompositionState: vi.fn(), fetchCompositionProposals: vi.fn(),
  fetchComposerOperationFailures: vi.fn(), fetchSessions: vi.fn(), listBlobs: vi.fn(), listInterpretationEvents: vi.fn(),
}));

const sid = "11111111-1111-4111-8111-111111111111";
const runId = "33333333-3333-4333-8333-333333333333";
const scope = { principalId: "principal", authProvider: "local" };
const iso = "2026-10-31T00:00:00Z";
const reference = `provider-failure:${"b".repeat(64)}`;
const privateCanary = "PRIVATE_PRIMARY_DEBUG_BODY_CANARY";
const safeDetail = "LLM request rejected (BadRequestError)";
const cases: { label: string; descriptor: SafeRequestRejection; explanation: string }[] = [
  {
    label: "context limit",
    descriptor: { category: "request_rejected", exception_type: "BadRequestError", reason_code: "context_length_exceeded", reason_disposition: "retained", reason_source: "http_response", diagnostic_reference: reference, retryable: false },
    explanation: "The request exceeded the model's context limit. Reduce the review context before trying again.",
  },
  {
    label: "unsupported parameter",
    descriptor: { category: "request_rejected", exception_type: "BadRequestError", reason_code: "unsupported_parameter", reason_disposition: "retained", reason_source: "sdk_body", diagnostic_reference: reference, retryable: false },
    explanation: "The request included an unsupported parameter. Check the provider request configuration before trying again.",
  },
  {
    label: "invalid request",
    descriptor: { category: "request_rejected", exception_type: "BadRequestError", reason_code: "invalid_request", reason_disposition: "retained", reason_source: "sdk_body", diagnostic_reference: reference, retryable: false },
    explanation: "The provider rejected invalid request input. A more specific rejection reason is unavailable.",
  },
  {
    label: "inspected envelope without code",
    descriptor: { category: "request_rejected", exception_type: "BadRequestError", reason_code: null, reason_disposition: "absent", reason_source: "http_response", diagnostic_reference: reference, retryable: false },
    explanation: "The provider rejected the request. No structured reason code was supplied in the inspected error envelope.",
  },
  {
    label: "unrecognized code",
    descriptor: { category: "request_rejected", exception_type: "BadRequestError", reason_code: null, reason_disposition: "unrecognized", reason_source: "http_response", diagnostic_reference: reference, retryable: false },
    explanation: "The provider rejected the request. Its structured reason could not be safely recognized.",
  },
  {
    label: "unavailable evidence",
    descriptor: { category: "request_rejected", exception_type: "BadRequestError", reason_code: null, reason_disposition: "unavailable", reason_source: "unavailable", diagnostic_reference: reference, retryable: false },
    explanation: "The provider rejected the request. The structured rejection reason is unavailable.",
  },
];

function errorBody(category: "llm_unavailable" | "run_diagnostics_explanation_failed", descriptor?: SafeRequestRejection, debug = true) {
  return { detail: {
    error_type: category, detail: safeDetail,
    ...(descriptor === undefined ? {} : { request_rejection: descriptor }),
    ...(debug ? { provider_detail: privateCanary, provider_status_code: 400 } : {}),
  } };
}

function terminal(version: 1 | 2, operationId: string, descriptor?: SafeRequestRejection, debug = true) {
  const common = { operation_id: operationId, kind: "compose_message", status: "failed", cancel_requested: false, poll_after_ms: 1000, result: null,
    error: { http_status: 502, error_type: "llm_unavailable", failure_code: "http_error", diagnostic_id: null, body: errorBody("llm_unavailable", descriptor, debug) } };
  return version === 1
    ? { ...common, deadline_at: iso, deadline_remaining_ms: 0 }
    : { ...common, protocol_version: 2, phase: "settled", status_revision: 9, created_at: iso, observed_at: iso, segment: null };
}

function queued(version: 1 | 2, operationId: string) {
  return version === 1
    ? { operation_id: operationId, kind: "compose_message", status: "queued", poll_after_ms: 1000 }
    : { protocol_version: 2, operation_id: operationId, kind: "compose_message", status: "queued", phase: "queued", status_revision: 1, created_at: iso, observed_at: iso, poll_after_ms: 1000, cancel_requested: false, segment: { sequence: 1, generation: 1, state: "queued", deadline_at: null }, result: null, error: null };
}

function response(value: unknown, version: 1 | 2, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: version === 2 ? { [COMPOSER_OPERATION_VERSION_HEADER]: "2" } : {} });
}

function installComposerTransport(version: 1 | 2, descriptor?: SafeRequestRejection, debug = true, delayedPoll?: () => Promise<Response>) {
  let operationId = "";
  const requests: { url: string; method: string }[] = [];
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const method = init?.method ?? "GET";
    requests.push({ url, method });
    if (url === `/api/sessions/${sid}/messages` && method === "POST") {
      if (typeof init?.body !== "string") throw new Error("Expected the real serialized admission body");
      const body = JSON.parse(init.body) as { operation_id: string };
      operationId = body.operation_id;
      expect(new Headers(init.headers).get(COMPOSER_OPERATION_VERSION_HEADER)).toBe(version === 2 ? "2" : null);
      return response(queued(version, operationId), version, 202);
    }
    if (url === `/api/sessions/${sid}/operations/${operationId}/stream`) {
      const frame = { schema_version: `composer-operation-stream.v${version}`, session_id: sid, operation_id: operationId, sequence: 0, event: "terminal",
        payload: version === 1 ? { status: "failed" } : { status: "failed", phase: "settled", status_revision: 9 } };
      return new Response(`${version === 2 ? "id: 9\n" : ""}data: ${JSON.stringify(frame)}\n\n`, { headers: { "Content-Type": "text/event-stream", ...(version === 2 ? { [COMPOSER_OPERATION_VERSION_HEADER]: "2" } : {}) } });
    }
    if (url === `/api/sessions/${sid}/operations/${operationId}/cancel` && method === "POST") return response(terminal(version, operationId, descriptor, debug), version);
    if (url === `/api/sessions/${sid}/operations/${operationId}`) return delayedPoll ? delayedPoll() : response(terminal(version, operationId, descriptor, debug), version);
    throw new Error(`Unexpected physical client request: ${method} ${url}`);
  });
  vi.stubGlobal("fetch", fetch);
  return { requests, operationId: () => operationId };
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.setItem("auth_token", "fixture-primary-rejection-token");
  useSessionStore.getState().reset(); useExecutionStore.getState().reset();
  purgeComposerCustody(); sessionStorage.clear(); authenticateComposerCustody(scope);
  useSessionStore.setState({ activeSessionId: sid, compositionStateLoaded: true });
  vi.mocked(api.fetchMessages).mockResolvedValue([]);
  vi.mocked(api.fetchCompositionState).mockResolvedValue(null);
  vi.mocked(api.fetchCompositionProposals).mockResolvedValue([]);
  vi.mocked(api.fetchComposerOperationFailures).mockImplementation(async (sessionId) => ({ session_id: sessionId, items: [], next_cursor: null }));
  vi.mocked(api.fetchSessions).mockResolvedValue([]);
  vi.mocked(api.listBlobs).mockResolvedValue([]);
  vi.mocked(api.listInterpretationEvents).mockResolvedValue([]);
});

afterEach(() => {
  detachComposerObservers(); purgeComposerCustody(); setComposerAdmissionVersion(1);
  vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); sessionStorage.clear();
});

describe.each([1, 2] as const)("actual V%s Composer terminal rejection rendering", (version) => {
  it.each(cases)("renders contextual safe copy for $label through the real shared client and observer", async ({ descriptor, explanation }) => {
    setComposerAdmissionVersion(version);
    const transport = installComposerTransport(version, descriptor);
    await useSessionStore.getState().sendMessage("Explain this primary request.");
    const expected = `The Composer request was rejected. ${explanation} Diagnostic reference: ${reference}.`;
    const state = useSessionStore.getState();
    expect(state.error).toBe(expected);
    expect(state.messages).toEqual([expect.objectContaining({ role: "user", local_status: "failed", local_error: expected })]);
    expect(state.error).not.toContain(privateCanary);
    expect(state.error).not.toMatch(/advisory|completion remains withheld/i);
    expect(state.isComposing).toBe(false);
    expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
    expect(transport.requests.filter(request => request.method === "POST")).toEqual([{ method: "POST", url: `/api/sessions/${sid}/messages` }]);
    expect(transport.requests.filter(request => request.url.endsWith(`/operations/${transport.operationId()}`))).toHaveLength(1);
  });

  it("renders the same safe copy with debug exposure absent", async () => {
    setComposerAdmissionVersion(version);
    const { descriptor, explanation } = cases[0];
    installComposerTransport(version, descriptor, false);
    await useSessionStore.getState().sendMessage("Explain this primary request.");
    expect(useSessionStore.getState().error).toBe(`The Composer request was rejected. ${explanation} Diagnostic reference: ${reference}.`);
  });

  it("omits a diagnostic reference when the admitted descriptor has none", async () => {
    setComposerAdmissionVersion(version);
    const { descriptor, explanation } = cases[0];
    installComposerTransport(version, { ...descriptor, diagnostic_reference: null }, false);
    await useSessionStore.getState().sendMessage("Explain this rejected request without a reference.");
    expect(useSessionStore.getState().error).toBe(`The Composer request was rejected. ${explanation}`);
  });

  it("preserves the historical primary fallback without inventing reason evidence", async () => {
    setComposerAdmissionVersion(version);
    installComposerTransport(version, undefined, false);
    await useSessionStore.getState().sendMessage("Explain this historical request.");
    expect(useSessionStore.getState().error).toBe("The AI service is temporarily unavailable. Please try again in a moment.");
    expect(useSessionStore.getState().error).not.toContain("No structured reason");
    expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
  });

  it("does not publish a late rejection after the actual auth generation changes", async () => {
    setComposerAdmissionVersion(version);
    let release!: (value: Response) => void;
    let reached = false;
    const waiting = new Promise<Response>(resolve => { release = resolve; });
    const transport = installComposerTransport(version, cases[0].descriptor, true, () => { reached = true; return waiting; });
    const sending = useSessionStore.getState().sendMessage("Keep this read scoped to its login.");
    try {
      await vi.waitFor(() => expect(reached).toBe(true));
      advanceAuthGeneration();
    } finally {
      release(response(terminal(version, transport.operationId(), cases[0].descriptor), version));
      await sending;
    }
    expect(useSessionStore.getState().error).toBeNull();
    expect(useSessionStore.getState().messages.every(message => message.local_error === undefined)).toBe(true);
    expect(transport.requests.filter(request => request.method === "POST")).toHaveLength(1);
  });

  it("keeps explicit Stop on the same custody while the already settled rejection wins the read race", async () => {
    setComposerAdmissionVersion(version);
    let release!: (value: Response) => void;
    let reached = false;
    const waiting = new Promise<Response>(resolve => { release = resolve; });
    const { descriptor, explanation } = cases[0];
    const transport = installComposerTransport(version, descriptor, true, () => { reached = true; return waiting; });
    const sending = useSessionStore.getState().sendMessage("Stop this same primary request.");
    try {
      await vi.waitFor(() => expect(reached).toBe(true));
      const held = findComposerOperationCustody(scope, sid).foreground;
      expect(held).not.toBeNull();
      if (held === null) throw new Error("The real observer did not retain its submitted custody");
      useSessionStore.getState().cancelComposition();
      expect(composerStopRequested(held)).toBe(true);
      await vi.waitFor(() => expect(transport.requests.filter(request => request.url.endsWith("/cancel")).length).toBeGreaterThan(0));
      expect(transport.requests.filter(request => request.url.endsWith("/cancel")).every(request => request.url === `/api/sessions/${sid}/operations/${held.operationId}/cancel`)).toBe(true);
    } finally {
      release(response(terminal(version, transport.operationId(), descriptor), version));
      await sending;
    }
    expect(transport.requests.filter(request => request.method === "POST" && request.url.endsWith("/messages"))).toHaveLength(1);
    expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
    expect(useSessionStore.getState().composerStopPendingOperationId).toBeNull();
    expect(useSessionStore.getState().error).toBe(`The Composer request was rejected. ${explanation} Diagnostic reference: ${reference}.`);
  });
});

describe("actual execution-store diagnostics rejection rendering", () => {
  it.each(cases)("renders diagnostics-specific safe copy for $label through actual HTTP admission", async ({ descriptor, explanation }) => {
    const fetch = vi.fn(async () => new Response(JSON.stringify(errorBody("run_diagnostics_explanation_failed", descriptor)), { status: 502 }));
    vi.stubGlobal("fetch", fetch);
    await useExecutionStore.getState().evaluateRunDiagnostics(runId);
    const state = useExecutionStore.getState();
    expect(state.diagnosticsErrorByRunId[runId]).toBe(`The run diagnostics request was rejected. ${explanation} Diagnostic reference: ${reference}.`);
    expect(state.diagnosticsErrorByRunId[runId]).not.toContain(privateCanary);
    expect(state.diagnosticsErrorByRunId[runId]).not.toMatch(/advisory|completion remains withheld/i);
    expect(state.diagnosticsEvaluatingByRunId[runId]).toBe(false);
    expect(state.diagnosticsExplanationByRunId[runId]).toBeUndefined();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0]).toEqual([`/api/runs/${runId}/diagnostics/evaluate?limit=50`, expect.objectContaining({ method: "POST" })]);
  });

  it("keeps the historical diagnostics fallback when the descriptor is absent", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(errorBody("run_diagnostics_explanation_failed", undefined, false)), { status: 502 })));
    await useExecutionStore.getState().evaluateRunDiagnostics(runId);
    expect(useExecutionStore.getState().diagnosticsErrorByRunId[runId]).toBe(safeDetail);
  });

  it("uses safe diagnostics copy without debug exposure or an invented reference", async () => {
    const { descriptor, explanation } = cases[5];
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(errorBody("run_diagnostics_explanation_failed", { ...descriptor, diagnostic_reference: null }, false)), { status: 502 })));
    await useExecutionStore.getState().evaluateRunDiagnostics(runId);
    expect(useExecutionStore.getState().diagnosticsErrorByRunId[runId]).toBe(`The run diagnostics request was rejected. ${explanation}`);
  });
});
