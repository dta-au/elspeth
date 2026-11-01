import producerFixtures from "@/test/composerRecoveryProducerFixtures.json";
import executorFixtures from "../../../../../../tests/fixtures/web/composer/composer_executor_failure_producer.json";
import { describe, expect, it } from "vitest";
import { ComposerOperationDecodeError, ComposerSseDecoder, decodeComposerActiveOperation, decodeComposerOperationFrame, decodeComposerOperationSnapshot, decodeComposerOperationFailurePage } from "./composerOperationDecoder";
const sid = "11111111-1111-4111-8111-111111111111";
const id = "22222222-2222-4222-8222-222222222222";
const heartbeat = { session_id: sid, operation_id: id, sequence: 0, event: "heartbeat" };
/** An SSE frame as the framing decoder hands it to the DTO decoder. */
const sse = (data: unknown, eventId: string | null = null) => ({ id: eventId, data });
/** A settled operation envelope; cases set status, result and error. */
const envelope = { operation_id: id, kind: "compose_message", poll_after_ms: 1000, cancel_requested: false, phase: "settled", status_revision: 9, created_at: "2026-10-06T00:00:00Z", observed_at: "2026-10-06T00:00:00Z", segment: null };
const encode = new TextEncoder();

describe.each([
  ["generation", executorFixtures.generation],
  ["admission", executorFixtures.admission],
] as const)("actual backend executor failure %s", (_family, producer) => {
  const sessionId = producer.history.session_id;
  const operationId = producer.terminal.operation_id;

  it("preserves the actual failed terminal, safe body and correlation", () => {
    const decoded = decodeComposerOperationSnapshot(producer.terminal, sessionId, operationId);
    expect(decoded).toEqual(producer.terminal);
    expect(decoded.error).toEqual(producer.terminal.error);
    expect(decoded.error?.http_status).toBe(503);
    expect(decoded.error?.failure_code).toBe("http_error");
    expect(decoded.error?.error_type).toBe("executor_unavailable");
    expect(decoded.error?.diagnostic_id).toBeNull();
    expect(decoded.error?.body.request_id).toBe(producer.history.items[0].request_id);
    expect(decoded.result).toBeNull();
  });

  it("preserves the actual executor category through closed operation history", () => {
    const decoded = decodeComposerOperationFailurePage(producer.history, sessionId, operationId);
    expect(decoded).toEqual(producer.history);
    expect(decodeComposerOperationFailurePage(producer.history, sessionId)).toEqual(producer.history);
    expect(decoded.items[0].error_type).toBe("executor_unavailable");
    expect(decoded.items[0].failure_code).toBe(producer.terminal.error.failure_code);
    expect(decoded.items[0].request_id).toBe(producer.terminal.error.body.request_id);
    expect(decoded.items[0].saved_revisions).toEqual(producer.history.items[0].saved_revisions);
  });

  it("preserves the intentionally open terminal category independently of closed history", () => {
    const variant = {
      ...producer.terminal,
      error: { ...producer.terminal.error, error_type: "unrecognized_terminal_category" },
    };
    expect(decodeComposerOperationSnapshot(variant, sessionId, operationId).error?.error_type).toBe("unrecognized_terminal_category");
    expect(decodeComposerOperationSnapshot(producer.terminal, sessionId, operationId)).toEqual(producer.terminal);
  });

  it("rejects terminal schema and operation-scope corruption after its actual positive", () => {
    expect(decodeComposerOperationSnapshot(producer.terminal, sessionId, operationId)).toEqual(producer.terminal);
    expect(sessionId).not.toBe(operationId);
    const mutations = [
      { ...producer.terminal, operation_id: sessionId },
      { ...producer.terminal, error: { ...producer.terminal.error, private_exception: "PRIVATE sentinel" } },
      { ...producer.terminal, error: { ...producer.terminal.error, error_type: 503 } },
      { ...producer.terminal, error: { ...producer.terminal.error, http_status: 200 } },
      { ...producer.terminal, error: { ...producer.terminal.error, failure_code: "unreviewed_failure_code" } },
    ];
    for (const changed of mutations) {
      expect(() => decodeComposerOperationSnapshot(changed, sessionId, operationId)).toThrow(ComposerOperationDecodeError);
      expect(decodeComposerOperationSnapshot(producer.terminal, sessionId, operationId)).toEqual(producer.terminal);
    }
  });

  it("keeps every current closed history category including U29 and rejects unknown vocabulary", () => {
    // These are controlled one-field vocabulary neighbors of the captured page,
    // not additional producer captures or a rewritten accepted fixture.
    for (const category of [
      "request_cancelled", "composer_operation_deadline_expired", "composer_operation_worker_lost",
      "operation_failed", "audit_integrity_error", "database_unavailable", "storage_unavailable",
      "stale_compose_state", "fingerprint_key_missing", "secret_decryption_failed",
      "composer_mutation_authority_revoked", null,
    ]) {
      const control = { ...producer.history, items: [{ ...producer.history.items[0], error_type: category }] };
      expect(decodeComposerOperationFailurePage(control, sessionId, operationId)).toEqual(control);
      const changed = { ...control, items: [{ ...control.items[0], error_type: "unreviewed_history_category" }] };
      expect(() => decodeComposerOperationFailurePage(changed, sessionId, operationId)).toThrow("Unknown public failure category");
      expect(decodeComposerOperationFailurePage(control, sessionId, operationId)).toEqual(control);
    }
  });

  it("rejects private, scope and saved-fact corruption from a reached current-history positive", () => {
    const control = {
      ...producer.history,
      items: [{ ...producer.history.items[0], error_type: "database_unavailable" }],
    };
    expect(decodeComposerOperationFailurePage(control, sessionId, operationId)).toEqual(control);
    const mutations = [
      { ...control, body: { detail: "PRIVATE sentinel" } },
      { ...control, session_id: operationId },
      { ...control, items: [{ ...control.items[0], body: { detail: "PRIVATE sentinel" } }] },
      { ...control, items: [{ ...control.items[0], session_id: operationId }] },
      { ...control, items: [{ ...control.items[0], operation_id: sessionId }] },
      { ...control, items: [{ ...control.items[0], saved_revisions: { ...control.items[0].saved_revisions, count: false } }] },
      { ...control, items: [control.items[0], control.items[0]] },
    ];
    for (const changed of mutations) {
      expect(() => decodeComposerOperationFailurePage(changed, sessionId, operationId)).toThrow(ComposerOperationDecodeError);
      expect(decodeComposerOperationFailurePage(control, sessionId, operationId)).toEqual(control);
    }
    expect(() => decodeComposerOperationFailurePage(control, sessionId, sessionId)).toThrow("Failure page operation mismatch");
  });
});

describe("retained failure page admission", () => {
  const outcome = { session_id: sid, operation_id: id, kind: "compose_message", user_message_id: null, settled_at: "2026-10-14T08:00:00Z", failure_code: "operation_failed", error_type: "operation_failed", diagnostic_id: id, request_id: "request-reference", saved_revisions: { attribution: "fenced", count: 2, latest_state_id: id, latest_version: 7 } };
  const page = { session_id: sid, items: [outcome], next_cursor: null };
  it("admits closed saved facts and distinguishes unavailable progress from zero", () => {
    expect(decodeComposerOperationFailurePage(page, sid, id)).toEqual(page);
    for (const saved of [
      { attribution: "unavailable", count: null, latest_state_id: null, latest_version: null },
      { attribution: "unstarted", count: 0, latest_state_id: null, latest_version: null },
      { attribution: "fenced", count: 0, latest_state_id: null, latest_version: null },
    ]) expect(decodeComposerOperationFailurePage({ ...page, items: [{ ...outcome, saved_revisions: saved }] }, sid).items[0].saved_revisions).toEqual(saved);
  });
  it("rejects private fields, mixed scope, forged identifiers and inconsistent saved facts", () => {
    for (const changes of [
      { body: { detail: "private canary" } }, { error_type: "private exception class" }, { session_id: id },
      { operation_id: "invalid" }, { diagnostic_id: null }, { failure_code: "worker_lost" },
      { settled_at: "2026-10-14" }, { request_id: "" },
      ...[
        { attribution: "unavailable", count: 0, latest_state_id: null, latest_version: null },
        { attribution: "unstarted", count: 2, latest_state_id: id, latest_version: 7 },
        { attribution: "fenced", count: true, latest_state_id: id, latest_version: 7 },
        { attribution: "fenced", count: 0, latest_state_id: id, latest_version: 7 },
        { attribution: "fenced", count: 2, latest_state_id: null, latest_version: 7 },
      ].map((saved_revisions) => ({ saved_revisions })),
    ]) expect(() => decodeComposerOperationFailurePage({ ...page, items: [{ ...outcome, ...changes }] }, sid)).toThrow();
    for (const changes of [{ body: "private" }, { session_id: id }, { items: [outcome, outcome] }, { items: Array(101).fill(outcome) }, { next_cursor: "" }]) expect(() => decodeComposerOperationFailurePage({ ...page, ...changes }, sid)).toThrow();
    expect(() => decodeComposerOperationFailurePage(page, sid, sid)).toThrow();
  });
});
describe("composer operation external admission", () => {
  it("admits each UTF-8 byte split and split CRLF with coalesced frames", () => {
    const value = { ...heartbeat, event: "terminal", payload: { status: "completed" }, label: "€🐈" };
    const wire = encode.encode(`data: ${JSON.stringify(value)}\r\n\r\ndata: ${JSON.stringify(heartbeat)}\n\n`);
    const decoder = new ComposerSseDecoder();
    const frames = [...wire].flatMap((byte) => decoder.push(new Uint8Array([byte])));
    expect(frames).toEqual([sse(value), sse(heartbeat)]); expect(decoder.finish()).toEqual([]);
    // The framing positive control does not bypass strict DTO admission.
    expect(() => decodeComposerOperationFrame(frames[0], sid, id)).toThrow();
    expect(decodeComposerOperationFrame(frames[1], sid, id)).toEqual({ ...heartbeat, id: null });
  });
  it("admits multiline data and explicit event binding", () => {
    const decoder = new ComposerSseDecoder();
    const json = JSON.stringify(heartbeat);
    const split = json.indexOf(',');
    const frames = decoder.push(encode.encode(`event: heartbeat\ndata: ${json.slice(0, split + 1)}\ndata: ${json.slice(split + 1)}\n\n`));
    expect(decodeComposerOperationFrame(frames[0], sid, id)).toEqual({ ...heartbeat, id: null });
  });
  it("rejects invalid UTF-8, invalid JSON, event mismatch, unknown fields, oversized and truncated frames", () => {
    expect(() => new ComposerSseDecoder().push(new Uint8Array([0xff]))).toThrow();
    expect(() => new ComposerSseDecoder().push(encode.encode("data: nope\n\n"))).toThrow();
    expect(() => new ComposerSseDecoder().push(encode.encode(`event: status\ndata: ${JSON.stringify(heartbeat)}\n\n`))).toThrow();
    expect(() => new ComposerSseDecoder().push(encode.encode("retry: 1\ndata: {}\n\n"))).toThrow();
    expect(() => new ComposerSseDecoder().push(encode.encode("id: 1\nid: 2\ndata: {}\n\n"))).toThrow();
    expect(() => new ComposerSseDecoder().push(encode.encode("id: -1\ndata: {}\n\n"))).toThrow();
    expect(() => new ComposerSseDecoder(12).push(encode.encode("data: \"123456789\"\n\n"))).toThrow();
    const crlfWire = encode.encode('data: {}\r\n\r\n');
    expect(new ComposerSseDecoder(crlfWire.length).push(crlfWire)).toEqual([sse({})]);
    expect(() => new ComposerSseDecoder(crlfWire.length - 1).push(crlfWire)).toThrow();
    const truncated = new ComposerSseDecoder(); truncated.push(encode.encode("data: {}\n")); expect(() => truncated.finish()).toThrow();
    const utf8 = new ComposerSseDecoder(); utf8.push(new Uint8Array([0xf0, 0x9f])); expect(() => utf8.finish()).toThrow();
  });
  it("rejects wrong identities, unsupported variants and heartbeat replay bodies", () => {
    expect(() => decodeComposerOperationFrame(sse(heartbeat), sid, sid)).toThrow();
    expect(() => decodeComposerOperationFrame(sse({ ...heartbeat, answer: "secret" }), sid, id)).toThrow();
    expect(() => decodeComposerOperationFrame(sse({ ...heartbeat, payload: {} }), sid, id)).toThrow();
    expect(() => decodeComposerOperationFrame(sse({ ...heartbeat, event: "answer_delta", payload: "secret" }), sid, id)).toThrow();
    expect(() => decodeComposerOperationFrame(sse({ ...heartbeat, sequence: -1 }), sid, id)).toThrow();
    expect(() => decodeComposerOperationFrame(sse({ ...heartbeat, event: "terminal", payload: { status: "running", phase: "settled", status_revision: 9 } }, "9"), sid, id)).toThrow();
    expect(() => decodeComposerOperationFrame(sse(heartbeat, "1"), sid, id)).toThrow("Only status and terminal frames carry an id");
  });
  it("admits closed status/terminal DTOs bound to their SSE id", () => {
    const running = { ...envelope, status: "running", phase: "executing", segment: { sequence: 1, generation: 1, state: "queued", deadline_at: null } };
    expect(decodeComposerOperationFrame(sse({ ...heartbeat, event: "status", payload: running }, "9"), sid, id).event).toBe("status");
    expect(() => decodeComposerOperationFrame(sse({ ...heartbeat, event: "status", payload: running }, "8"), sid, id)).toThrow("revision");
    expect(decodeComposerOperationFrame(sse({ ...heartbeat, event: "terminal", payload: { status: "failed", phase: "settled", status_revision: 9 } }, "9"), sid, id).event).toBe("terminal");
    expect(decodeComposerOperationFrame(sse({ ...heartbeat, event: "reconnect" }), sid, id).event).toBe("reconnect");
  });
  it("refuses the retired version fields on every record (one lifecycle)", () => {
    const live = { ...envelope, status: "queued", phase: "queued", segment: { sequence: 1, generation: 1, state: "queued", deadline_at: null }, result: null, error: null };
    expect(decodeComposerOperationSnapshot(live, sid, id).status).toBe("queued");
    expect(() => decodeComposerOperationSnapshot({ ...live, protocol_version: 2 }, sid, id)).toThrow("Unexpected or missing field");
    expect(() => decodeComposerOperationFrame(sse({ ...heartbeat, schema_version: "composer-operation-stream.v2" }), sid, id)).toThrow("Unexpected or missing field");
    const active = { operation_id: id, kind: "compose_message", status: "running" };
    expect(decodeComposerActiveOperation(active)).toEqual(active);
    expect(() => decodeComposerActiveOperation({ ...active, lifecycle_version: 2 })).toThrow("Unexpected or missing field");
    expect(() => decodeComposerActiveOperation({ ...active, status: "completed" })).toThrow("not live");
  });
});

describe("completed answer durable GET boundary", () => {
  const message = { id, operation_id: null, session_id: sid, role: "assistant", content: "done", raw_content: null, segments: [{ kind: "text", content: "done" }], tool_calls: null, created_at: "2026-10-06T00:00:00Z", composition_state_id: null, tool_call_id: null, parent_assistant_id: null, sequence_no: 1, rejection: null };
  const complete = { ...envelope, status: "completed", result: { message, state: null, proposals: [] }, error: null };
  it("admits exact final response and rejects extra answer/identity/status corruption", () => {
    expect(decodeComposerOperationSnapshot(complete, sid, id).result?.message.content).toBe("done");
    expect(() => decodeComposerOperationSnapshot({ ...complete, result: { ...complete.result, answer_delta: "secret" } }, sid, id)).toThrow();
    expect(() => decodeComposerOperationSnapshot({ ...complete, result: { ...complete.result, message: { ...message, session_id: id } } }, sid, id)).toThrow();
    expect(() => decodeComposerOperationSnapshot({ ...complete, cancel_requested: true }, sid, id)).toThrow();
    expect(() => decodeComposerOperationSnapshot({ ...complete, segment: { sequence: 1, generation: 1, state: "queued", deadline_at: null } }, sid, id)).toThrow();
    expect(() => decodeComposerOperationSnapshot({ ...complete, phase: "executing" }, sid, id)).toThrow();
    expect(() => decodeComposerOperationSnapshot({ ...complete, status: "running" }, sid, id)).toThrow();
  });
  it("rejects missing/invalid nested message fields and unknown proposals", () => {
    const { segments: omitted, ...incomplete } = message;
    expect(omitted).toHaveLength(1);
    expect(() => decodeComposerOperationSnapshot({ ...complete, result: { ...complete.result, message: incomplete } }, sid, id)).toThrow();
    expect(() => decodeComposerOperationSnapshot({ ...complete, result: { ...complete.result, message: { ...message, created_at: "not a date" } } }, sid, id)).toThrow();
    expect(() => decodeComposerOperationSnapshot({ ...complete, result: { ...complete.result, proposals: [{ id, session_id: sid }] } }, sid, id)).toThrow();
  });
  it("admits a bound system recovery receipt and rejects forged scope or role", () => {
    const receipt = { ...message, operation_id: id, id: "33333333-3333-4333-8333-333333333333", role: "system", content: "Restored a saved draft", sequence_no: 2 };
    const withReceipt = (notice: unknown) => ({ ...complete, result: { ...complete.result, message: { ...message, operation_id: id }, recovery_notice: notice } });
    expect(decodeComposerOperationSnapshot(withReceipt(receipt), sid, id).result?.recovery_notice).toEqual(receipt);
    expect(decodeComposerOperationSnapshot(withReceipt(null), sid, id).result?.recovery_notice).toBeNull();
    for (const override of [{ session_id: id }, { role: "assistant" }, { id: message.id }, { operation_id: null }, { tool_calls: [] }, { injected: true }]) {
      expect(() => decodeComposerOperationSnapshot(withReceipt({ ...receipt, ...override }), sid, id)).toThrow();
    }
    for (const operation_id of [null, sid]) {
      const mixed = withReceipt({ ...receipt, operation_id });
      mixed.result.message.operation_id = operation_id as string;
      expect(() => decodeComposerOperationSnapshot(mixed, sid, id)).toThrow();
    }
  });
});

describe("strict failed terminal recovery", () => {
  const failedTurn = { assistant_message_id: null, tool_calls_attempted: 1, tool_responses_persisted: 1, transcript_url: null };
  const failed = (failed_turn: unknown) => ({ ...envelope, status: "failed", result: null, error: { http_status: 500, failure_code: "http_error", error_type: "composer_plugin_crash", diagnostic_id: null, body: { detail: { failed_turn } } } });
  it("admits producer-shaped nullable evidence and rejects field/type/count/URL mutations", () => {
    expect(decodeComposerOperationSnapshot(failed(failedTurn), sid, id).status).toBe("failed");
    for (const value of [{ ...failedTurn, assistant_message_id: 22 }, { ...failedTurn, tool_calls_attempted: {} }, { ...failedTurn, tool_responses_persisted: -3 }, { ...failedTurn, tool_responses_persisted: 2 }, { ...failedTurn, transcript_url: 5 }, { ...failedTurn, transcript_url: "//evil" }, { ...failedTurn, extra: true }, { assistant_message_id: null }]) expect(() => decodeComposerOperationSnapshot(failed(value), sid, id)).toThrow();
  });
});

describe("live backend producer serialization parity", () => {
  const base = { ...envelope, status: "failed", result: null };
  it("admits all serialized failure DTO variants and both actual failed-turn identity variants", () => {
    for (const error of [...producerFixtures.errors, producerFixtures.projected_audit_error]) expect(decodeComposerOperationSnapshot({ ...base, error }, sid, id).error?.failure_code).toBe(error.failure_code);
    for (const failed_turn of producerFixtures.recovery) for (const body of [{ failed_turn }, { detail: { failed_turn } }]) expect(decodeComposerOperationSnapshot({ ...base, error: { ...producerFixtures.errors[0], body } }, sid, id).status).toBe("failed");
  });
  it("admits serialized proposal datetimes and rejects timestamp corruption", () => {
    const message = { id, operation_id: null, session_id: sid, role: "assistant", content: "done", raw_content: null, segments: [], tool_calls: null, created_at: "2026-10-06T00:00:00Z", composition_state_id: null, tool_call_id: null, parent_assistant_id: null, sequence_no: 1, rejection: null };
    const complete = { ...base, status: "completed", error: null, result: { message, state: null, proposals: [producerFixtures.proposal, producerFixtures.projected_proposal] } };
    expect(decodeComposerOperationSnapshot(complete, sid, id).result?.proposals).toHaveLength(2);
    for (const field of ["created_at", "updated_at"]) for (const value of ["not a timestamp", "1", 22, null]) expect(() => decodeComposerOperationSnapshot({ ...complete, result: { ...complete.result, proposals: [{ ...producerFixtures.proposal, [field]: value }] } }, sid, id)).toThrow();
  });
});


it("retains actual saved facts for mutation authority refusal without inventing proposals", () => {
  const outcome = {
    session_id: sid, operation_id: id, kind: "compose_message", user_message_id: null,
    settled_at: "2026-10-22T08:00:00Z", failure_code: "http_error",
    error_type: "composer_mutation_authority_revoked", diagnostic_id: null,
    request_id: "request", saved_revisions: { attribution: "fenced", count: 1, latest_state_id: id, latest_version: 2 },
  };
  const page = { session_id: sid, items: [outcome], next_cursor: null };
  expect(decodeComposerOperationFailurePage(page, sid).items[0]).toEqual(outcome);
  for (const changes of [{ proposal_id: id }, { error_type: "invented_auto_proposal" }]) {
    expect(() => decodeComposerOperationFailurePage({ ...page, items: [{ ...outcome, ...changes }] }, sid)).toThrow();
  }
  const frame = { ...heartbeat, event: "progress", payload: {
    request_id: "request",
    phase: "failed", headline: "Automatic mutation authority was revoked.", evidence: [],
    likely_next: "Review the saved session before retrying.", reason: "mutation_authority_revoked",
    updated_at: "2026-10-22T08:00:00Z", segment_sequence: 1, segment_generation: 1,
  } };
  expect(decodeComposerOperationFrame(sse(frame), sid, id)).toEqual({ ...frame, id: null });
  expect(() => decodeComposerOperationFrame(sse({ ...frame, payload: { ...frame.payload, reason: "invented_auto_proposal" } }), sid, id)).toThrow("Unknown progress vocabulary");
});

it("decodes the context-unsupported progress reason (U-22 F4)", () => {
  const frame = { ...heartbeat, event: "progress", payload: {
    request_id: "request",
    phase: "failed", headline: "This request is too large to send to the model.",
    evidence: ["The current pipeline is too large to send to the model. Reduce its size and try again."],
    likely_next: "Reduce the pipeline or conversation size, then send the request again.", reason: "context_unsupported",
    updated_at: "2026-10-25T08:00:00Z", segment_sequence: 2, segment_generation: 1,
  } };
  expect(decodeComposerOperationFrame(sse(frame), sid, id)).toEqual({ ...frame, id: null });
  expect(() => decodeComposerOperationFrame(sse({ ...frame, payload: { ...frame.payload, reason: "context_unsupported_invented" } }), sid, id)).toThrow("Unknown progress vocabulary");
});

it("closes the progress vocabulary and bounds its text", () => {
  const payload = { request_id: null, phase: "calling_model", headline: "Asking the model.", evidence: [], likely_next: null, reason: null, updated_at: "2026-10-25T08:00:00Z", segment_sequence: 1, segment_generation: 1 };
  const decode = (changes: Record<string, unknown>) => decodeComposerOperationFrame(sse({ ...heartbeat, event: "progress", payload: { ...payload, ...changes } }), sid, id);
  expect(decode({}).event).toBe("progress");
  expect(() => decode({ phase: "thinking" })).toThrow("Unknown progress vocabulary");
  // A failed or cancelled phase must say why.
  expect(() => decode({ phase: "failed" })).toThrow("Unknown progress vocabulary");
  expect(() => decode({ phase: "cancelled" })).toThrow("Unknown progress vocabulary");
  expect(decode({ phase: "cancelled", reason: "client_cancelled" }).event).toBe("progress");
  expect(decode({ headline: "x".repeat(180) }).event).toBe("progress");
  expect(() => decode({ headline: "x".repeat(181) })).toThrow("Unbounded progress text");
  expect(() => decode({ headline: "" })).toThrow("Unbounded progress text");
  expect(() => decode({ evidence: ["a", "b", "c", "d", "e"] })).toThrow("Unbounded progress evidence");
  expect(() => decode({ request_id: "r".repeat(129) })).toThrow("Unbounded progress text");
});
