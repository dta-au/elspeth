import producerFixtures from "@/test/composerRecoveryProducerFixtures.json";
import { describe, expect, it } from "vitest";
import { ComposerSseDecoder, decodeComposerOperationAck, decodeComposerOperationFrame, decodeComposerOperationSnapshot } from "./composerOperationDecoder";
const sid = "11111111-1111-4111-8111-111111111111";
const id = "22222222-2222-4222-8222-222222222222";
const heartbeat = { schema_version: "composer-operation-stream.v1", session_id: sid, operation_id: id, sequence: 0, event: "heartbeat" };
const encode = new TextEncoder();
describe("composer operation external admission", () => {
  it("admits each UTF-8 byte split and split CRLF with coalesced frames", () => {
    const value = { ...heartbeat, event: "terminal", payload: { status: "completed" }, label: "€🐈" };
    const wire = encode.encode(`data: ${JSON.stringify(value)}\r\n\r\ndata: ${JSON.stringify(heartbeat)}\n\n`);
    const decoder = new ComposerSseDecoder();
    const frames = [...wire].flatMap((byte) => decoder.push(new Uint8Array([byte])));
    expect(frames).toEqual([value, heartbeat]); expect(decoder.finish()).toEqual([]);
    // The framing positive control does not bypass strict DTO admission.
    expect(() => decodeComposerOperationFrame(frames[0], sid, id)).toThrow();
    expect(decodeComposerOperationFrame(frames[1], sid, id)).toEqual(heartbeat);
  });
  it("admits multiline data and explicit event binding", () => {
    const decoder = new ComposerSseDecoder();
    const json = JSON.stringify(heartbeat);
    const split = json.indexOf(',');
    const frames = decoder.push(encode.encode(`event: heartbeat\ndata: ${json.slice(0, split + 1)}\ndata: ${json.slice(split + 1)}\n\n`));
    expect(decodeComposerOperationFrame(frames[0], sid, id)).toEqual(heartbeat);
  });
  it("rejects invalid UTF-8, invalid JSON, event mismatch, unknown fields, oversized and truncated frames", () => {
    expect(() => new ComposerSseDecoder().push(new Uint8Array([0xff]))).toThrow();
    expect(() => new ComposerSseDecoder().push(encode.encode("data: nope\n\n"))).toThrow();
    expect(() => new ComposerSseDecoder().push(encode.encode(`event: status\ndata: ${JSON.stringify(heartbeat)}\n\n`))).toThrow();
    expect(() => new ComposerSseDecoder().push(encode.encode("retry: 1\ndata: {}\n\n"))).toThrow();
    expect(() => new ComposerSseDecoder(12).push(encode.encode("data: \"123456789\"\n\n"))).toThrow();
    const crlfWire = encode.encode('data: {}\r\n\r\n');
    expect(new ComposerSseDecoder(crlfWire.length).push(crlfWire)).toEqual([{}]);
    expect(() => new ComposerSseDecoder(crlfWire.length - 1).push(crlfWire)).toThrow();
    const truncated = new ComposerSseDecoder(); truncated.push(encode.encode("data: {}\n")); expect(() => truncated.finish()).toThrow();
    const utf8 = new ComposerSseDecoder(); utf8.push(new Uint8Array([0xf0, 0x9f])); expect(() => utf8.finish()).toThrow();
  });
  it("rejects wrong identities, unsupported variants and heartbeat replay bodies", () => {
    expect(() => decodeComposerOperationFrame(heartbeat, sid, sid)).toThrow();
    expect(() => decodeComposerOperationFrame({ ...heartbeat, answer: "secret" }, sid, id)).toThrow();
    expect(() => decodeComposerOperationFrame({ ...heartbeat, payload: {} }, sid, id)).toThrow();
    expect(() => decodeComposerOperationFrame({ ...heartbeat, event: "answer_delta", payload: "secret" }, sid, id)).toThrow();
    expect(() => decodeComposerOperationFrame({ ...heartbeat, sequence: -1 }, sid, id)).toThrow();
    expect(() => decodeComposerOperationFrame({ ...heartbeat, event: "terminal", payload: { status: "running" } }, sid, id)).toThrow();
  });
  it("admits closed status/terminal DTOs and rejects malformed acknowledgement", () => {
    expect(decodeComposerOperationFrame({ ...heartbeat, event: "status", payload: { status: "running", cancel_requested: false, deadline_remaining_ms: 30 } }, sid, id).event).toBe("status");
    expect(decodeComposerOperationFrame({ ...heartbeat, event: "terminal", payload: { status: "failed" } }, sid, id).event).toBe("terminal");
    const ack = { operation_id: id, kind: "compose_message", status: "queued", poll_after_ms: 1000 };
    expect(decodeComposerOperationAck(ack)).toEqual(ack);
    expect(() => decodeComposerOperationAck({ ...ack, result: {} })).toThrow();
    expect(() => decodeComposerOperationAck({ ...ack, poll_after_ms: 0 })).toThrow();
    expect(() => decodeComposerOperationAck({ ...ack, status: "cancelled" })).toThrow();
  });
});

describe("completed answer durable GET boundary", () => {
  const message = { id, operation_id: null, session_id: sid, role: "assistant", content: "done", raw_content: null, segments: [{ kind: "text", content: "done" }], tool_calls: null, created_at: "2026-10-06T00:00:00Z", composition_state_id: null, tool_call_id: null, parent_assistant_id: null, sequence_no: 1, rejection: null };
  const complete = { operation_id: id, kind: "compose_message", status: "completed", poll_after_ms: 1000, cancel_requested: false, deadline_at: "2026-10-06T00:00:00Z", deadline_remaining_ms: 0, result: { message, state: null, proposals: [] }, error: null };
  it("admits exact final response and rejects extra answer/identity/status corruption", () => {
    expect(decodeComposerOperationSnapshot(complete, sid, id).result?.message.content).toBe("done");
    expect(() => decodeComposerOperationSnapshot({ ...complete, result: { ...complete.result, answer_delta: "secret" } }, sid, id)).toThrow();
    expect(() => decodeComposerOperationSnapshot({ ...complete, result: { ...complete.result, message: { ...message, session_id: id } } }, sid, id)).toThrow();
    expect(() => decodeComposerOperationSnapshot({ ...complete, cancel_requested: true }, sid, id)).toThrow();
    expect(() => decodeComposerOperationSnapshot({ ...complete, deadline_remaining_ms: 1 }, sid, id)).toThrow();
    expect(() => decodeComposerOperationSnapshot({ ...complete, status: "running" }, sid, id)).toThrow();
  });
  it("rejects missing/invalid nested message fields and unknown proposals", () => {
    const { segments: omitted, ...incomplete } = message;
    expect(omitted).toHaveLength(1);
    expect(() => decodeComposerOperationSnapshot({ ...complete, result: { ...complete.result, message: incomplete } }, sid, id)).toThrow();
    expect(() => decodeComposerOperationSnapshot({ ...complete, result: { ...complete.result, message: { ...message, created_at: "not a date" } } }, sid, id)).toThrow();
    expect(() => decodeComposerOperationSnapshot({ ...complete, result: { ...complete.result, proposals: [{ id, session_id: sid }] } }, sid, id)).toThrow();
  });
});

describe("strict failed terminal recovery", () => {
  const failedTurn = { assistant_message_id: null, tool_calls_attempted: 1, tool_responses_persisted: 1, transcript_url: null };
  const failed = (failed_turn: unknown) => ({ operation_id: id, kind: "compose_message", status: "failed", poll_after_ms: 1000, cancel_requested: false, deadline_at: "2026-10-06T00:00:00Z", deadline_remaining_ms: 0, result: null, error: { http_status: 500, failure_code: "http_error", error_type: "composer_plugin_crash", diagnostic_id: null, body: { detail: { failed_turn } } } });
  it("admits producer-shaped nullable evidence and rejects field/type/count/URL mutations", () => {
    expect(decodeComposerOperationSnapshot(failed(failedTurn), sid, id).status).toBe("failed");
    for (const value of [{ ...failedTurn, assistant_message_id: 22 }, { ...failedTurn, tool_calls_attempted: {} }, { ...failedTurn, tool_responses_persisted: -3 }, { ...failedTurn, tool_responses_persisted: 2 }, { ...failedTurn, transcript_url: 5 }, { ...failedTurn, transcript_url: "//evil" }, { ...failedTurn, extra: true }, { assistant_message_id: null }]) expect(() => decodeComposerOperationSnapshot(failed(value), sid, id)).toThrow();
  });
});

describe("live backend producer serialization parity", () => {
  const base = { operation_id: id, kind: "compose_message", status: "failed", poll_after_ms: 1000, cancel_requested: false, deadline_at: "2026-10-06T00:00:00Z", deadline_remaining_ms: 0, result: null };
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
