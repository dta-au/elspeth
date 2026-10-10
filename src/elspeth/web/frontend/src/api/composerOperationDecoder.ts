import { decodeCompositionState } from "./compositionDecoder";
import type { ChatMessage, MessageWithStateResponse } from "@/types/index";
import type { ComposerOperationAck, ComposerOperationError, ComposerOperationSnapshot, ComposerOperationFrame, OperationKind, OperationStatus } from "@/types/composerOperations";

export class ComposerOperationDecodeError extends Error {
  constructor(message: string) { super(message); this.name = "ComposerOperationDecodeError"; }
}
export function exactRecord(value: unknown, keys: readonly string[], required = keys): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new ComposerOperationDecodeError("Expected object");
  const record = value as Record<string, unknown>;
  if (Object.keys(record).some((key) => !keys.includes(key)) || required.some((key) => !Object.prototype.hasOwnProperty.call(record, key))) throw new ComposerOperationDecodeError("Unexpected or missing field");
  return record;
}
export function canonicalUuid(value: unknown): string {
  if (typeof value !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value)) throw new ComposerOperationDecodeError("Expected canonical UUID");
  return value;
}
export function operationKind(value: unknown): OperationKind {
  if (value !== "compose_message" && value !== "compose_recompose") throw new ComposerOperationDecodeError("Unknown operation kind");
  return value;
}
export function operationStatus(value: unknown): OperationStatus {
  if (value !== "queued" && value !== "running" && value !== "completed" && value !== "failed") throw new ComposerOperationDecodeError("Unknown operation status");
  return value;
}
export function integer(value: unknown, minimum = 0): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < minimum) throw new ComposerOperationDecodeError("Expected bounded integer");
  return value;
}
export function text(value: unknown): string {
  if (typeof value !== "string") throw new ComposerOperationDecodeError("Expected string");
  return value;
}
function timestamp(value: unknown): string {
  const result = text(value);
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(result) || !Number.isFinite(Date.parse(result))) throw new ComposerOperationDecodeError("Invalid timestamp");
  return result;
}
export function decodeComposerOperationAck(value: unknown): ComposerOperationAck {
  const record = exactRecord(value, ["operation_id", "kind", "status", "poll_after_ms"]);
  return { operation_id: canonicalUuid(record.operation_id), kind: operationKind(record.kind), status: operationStatus(record.status), poll_after_ms: pollAfter(record.poll_after_ms) };
}
export function validateComposerRecoveryBody(body: unknown, sessionId?: string): void {
  if (typeof body !== "object" || body === null || Array.isArray(body)) return;
  const outer = body as Record<string, unknown>;
  const nested = typeof outer.detail === "object" && outer.detail !== null && !Array.isArray(outer.detail) ? outer.detail as Record<string, unknown> : null;
  for (const record of [outer, nested]) {
    if (record === null) continue;
    if (record.partial_state != null) {
      const state = decodeCompositionState(record.partial_state);
      if (sessionId !== undefined && state.session_id !== sessionId) throw new ComposerOperationDecodeError("Recovery state scope mismatch");
    }
    if (record.failed_turn != null) {
      const failed = exactRecord(record.failed_turn, ["assistant_message_id", "tool_calls_attempted", "tool_responses_persisted", "transcript_url"]);
      if (failed.assistant_message_id !== null) canonicalUuid(failed.assistant_message_id);
      const attempted = integer(failed.tool_calls_attempted);
      if (integer(failed.tool_responses_persisted) > attempted) throw new ComposerOperationDecodeError("Invalid recovery counts");
      if (failed.transcript_url !== null) {
        const url = text(failed.transcript_url);
        if (!url.startsWith("/") || url.startsWith("//") || url.includes("\\") || /[\s]/.test(url)) throw new ComposerOperationDecodeError("Invalid recovery transcript URL");
      }
    }
    if (record.partial_state_save_failed !== undefined && typeof record.partial_state_save_failed !== "boolean") throw new ComposerOperationDecodeError("Invalid recovery save flag");
    if (record.partial_state_save_error !== undefined && record.partial_state_save_error !== null) text(record.partial_state_save_error);
  }
}
function decodeError(value: unknown, sessionId: string): ComposerOperationError {
  const record = exactRecord(value, ["http_status", "body", "error_type", "failure_code", "diagnostic_id"]);
  const status = integer(record.http_status, 400);
  if (status > 599) throw new ComposerOperationDecodeError("Invalid HTTP error status");
  if (typeof record.body !== "object" || record.body === null || Array.isArray(record.body)) throw new ComposerOperationDecodeError("Invalid error body");
  validateComposerRecoveryBody(record.body, sessionId);
  const codes = ["http_error", "operation_failed", "worker_lost", "request_cancelled", "deadline_expired"] as const;
  const code = codes.find((item) => item === record.failure_code);
  if (code === undefined) throw new ComposerOperationDecodeError("Unknown failure code");
  const diagnostic_id = record.diagnostic_id === null ? null : canonicalUuid(record.diagnostic_id);
  if ((code === "operation_failed") !== (diagnostic_id !== null)) throw new ComposerOperationDecodeError("Invalid diagnostic binding");
  return { http_status: status, body: record.body as Record<string, unknown>, error_type: record.error_type === null ? null : text(record.error_type), failure_code: code, diagnostic_id };
}
export function decodeComposerResult(value: unknown, sessionId: string): MessageWithStateResponse {
  const record = exactRecord(value, ["message", "state", "proposals"]);
  const message = exactRecord(record.message, ["id", "operation_id", "session_id", "role", "content", "raw_content", "segments", "tool_calls", "created_at", "composition_state_id", "tool_call_id", "parent_assistant_id", "sequence_no", "rejection"]);
  canonicalUuid(message.id);
  if (message.session_id !== sessionId || message.role !== "assistant") throw new ComposerOperationDecodeError("Final message scope mismatch");
  text(message.content);
  timestamp(message.created_at);
  if (message.operation_id !== undefined && message.operation_id !== null) canonicalUuid(message.operation_id);
  for (const field of ["composition_state_id", "tool_call_id", "parent_assistant_id"]) if (message[field] !== undefined && message[field] !== null) text(message[field]);
  if (message.raw_content !== undefined && message.raw_content !== null) text(message.raw_content);
  if (message.sequence_no !== undefined && message.sequence_no !== null) integer(message.sequence_no);
  if (!Array.isArray(message.segments) || !Array.isArray(record.proposals) || (message.tool_calls !== null && !Array.isArray(message.tool_calls))) throw new ComposerOperationDecodeError("Invalid final lists");
  if (Array.isArray(message.tool_calls)) for (const value of message.tool_calls) {
    const call = exactRecord(value, ["id", "type", "function", "outcome", "applied_state_version", "related_proposal_id"], ["id", "type", "function"]);
    text(call.id); text(call.type);
    const fn = exactRecord(call.function, ["name", "arguments"]); text(fn.name); text(fn.arguments);
    if (call.outcome !== undefined && !["applied", "rejected", "failed", "cancelled", "completed"].includes(text(call.outcome))) throw new ComposerOperationDecodeError("Invalid tool outcome");
    if (call.applied_state_version !== undefined && call.applied_state_version !== null) integer(call.applied_state_version, 1);
    if (call.related_proposal_id !== undefined && call.related_proposal_id !== null) canonicalUuid(call.related_proposal_id);
  }
  if (message.rejection !== undefined && message.rejection !== null) throw new ComposerOperationDecodeError("Assistant cannot carry a tool rejection");
  for (const segment of message.segments) {
    const item = exactRecord(segment, ["kind", "content"]);
    if (item.kind !== "text" && item.kind !== "trusted_system_notice") throw new ComposerOperationDecodeError("Unknown segment");
    text(item.content);
  }
  for (const value of record.proposals) {
    const proposal = exactRecord(value, ["id", "session_id", "tool_call_id", "tool_name", "status", "summary", "rationale", "affects", "arguments_redacted_json", "base_state_id", "committed_state_id", "audit_event_id", "pipeline_metadata", "created_at", "updated_at"]);
    canonicalUuid(proposal.id);
    if (proposal.session_id !== sessionId || !["pending", "committed", "rejected"].includes(text(proposal.status))) throw new ComposerOperationDecodeError("Invalid proposal identity/status");
    for (const field of ["tool_call_id", "tool_name", "summary", "rationale", "created_at", "updated_at"]) text(proposal[field]);
    for (const field of ["created_at", "updated_at"]) timestamp(proposal[field]);
    for (const field of ["base_state_id", "committed_state_id", "audit_event_id"]) if (proposal[field] !== null) canonicalUuid(proposal[field]);
    if (!Array.isArray(proposal.affects) || proposal.affects.some((item) => typeof item !== "string")) throw new ComposerOperationDecodeError("Invalid proposal affects");
    if (typeof proposal.arguments_redacted_json !== "object" || proposal.arguments_redacted_json === null || Array.isArray(proposal.arguments_redacted_json)) throw new ComposerOperationDecodeError("Invalid proposal arguments");
    if (proposal.pipeline_metadata !== null) {
      const metadata = exactRecord(proposal.pipeline_metadata, ["draft_hash", "base", "repair_count", "skill_hash", "audit_payload_hash", "custody_result"]);
      for (const field of ["draft_hash", "skill_hash", "audit_payload_hash"]) if (!/^[0-9a-f]{64}$/.test(text(metadata[field]))) throw new ComposerOperationDecodeError("Invalid metadata hash");
      integer(metadata.repair_count);
      if (metadata.custody_result !== "not_required" && metadata.custody_result !== "ready") throw new ComposerOperationDecodeError("Invalid metadata custody");
      if (typeof metadata.base !== "object" || metadata.base === null || Array.isArray(metadata.base)) throw new ComposerOperationDecodeError("Invalid metadata base");
    }
  }
  const state = record.state === null ? null : decodeCompositionState(record.state);
  if (state !== null && state.session_id !== sessionId) throw new ComposerOperationDecodeError("Final state scope mismatch");
  return { message: message as unknown as ChatMessage, state, proposals: record.proposals as MessageWithStateResponse["proposals"] };
}
export function decodeComposerOperationSnapshot(value: unknown, sessionId: string, operationId: string): ComposerOperationSnapshot {
  const record = exactRecord(value, ["operation_id", "kind", "status", "poll_after_ms", "cancel_requested", "deadline_at", "deadline_remaining_ms", "result", "error"]);
  const operation_id = canonicalUuid(record.operation_id);
  if (operation_id !== operationId) throw new ComposerOperationDecodeError("Operation scope mismatch");
  const status = operationStatus(record.status);
  if (typeof record.cancel_requested !== "boolean") throw new ComposerOperationDecodeError("Invalid cancel flag");
  const deadline_at = text(record.deadline_at);
  const remaining = integer(record.deadline_remaining_ms);
  if ((status === "completed" || status === "failed") && remaining !== 0) throw new ComposerOperationDecodeError("Terminal deadline must be zero");
  if (status === "completed" && record.cancel_requested) throw new ComposerOperationDecodeError("Completed cancellation conflict");
  if (!Number.isFinite(Date.parse(deadline_at))) throw new ComposerOperationDecodeError("Invalid deadline");
  if ((status === "completed" && (record.result === null || record.error !== null)) || (status === "failed" && (record.error === null || record.result !== null)) || ((status === "queued" || status === "running") && (record.result !== null || record.error !== null))) throw new ComposerOperationDecodeError("Invalid terminal payload");
  return { operation_id, kind: operationKind(record.kind), status, poll_after_ms: pollAfter(record.poll_after_ms), cancel_requested: record.cancel_requested, deadline_at, deadline_remaining_ms: integer(record.deadline_remaining_ms), result: record.result === null ? null : decodeComposerResult(record.result, sessionId), error: record.error === null ? null : decodeError(record.error, sessionId) };
}

/** Bounded incremental SSE framing. UTF-8 is admitted before any JSON is parsed. */
export class ComposerSseDecoder {
  private readonly utf8 = new TextDecoder("utf-8", { fatal: true });
  private line = "";
  private lines: string[] = [];
  private bytes = 0;
  private pendingCR = false;
  private readonly encode = new TextEncoder();
  constructor(private readonly maxBytes = 64 * 1024) {}
  push(chunk: Uint8Array): unknown[] {
    const frames: unknown[] = [];
    for (let offset = 0; offset < chunk.byteLength; offset += 4096) frames.push(...this.consume(this.utf8.decode(chunk.subarray(offset, offset + 4096), { stream: true })));
    return frames;
  }
  finish(): unknown[] {
    const frames = this.consume(this.utf8.decode());
    if (this.pendingCR) { this.pendingCR = false; this.endLine(frames); }
    if (this.line !== "" || this.lines.length !== 0 || this.bytes !== 0) throw new ComposerOperationDecodeError("Truncated SSE frame");
    return frames;
  }
  private consume(input: string): unknown[] {
    const frames: unknown[] = [];
    for (const character of input) {
      if (this.pendingCR) {
        this.pendingCR = false;
        if (character === "\n") { this.count(character); this.endLine(frames); continue; }
        this.endLine(frames);
      }
      this.count(character);
      if (character === "\r") this.pendingCR = true;
      else if (character === "\n") this.endLine(frames);
      else this.line += character;
    }
    return frames;
  }
  private count(character: string): void {
    this.bytes += this.encode.encode(character).byteLength;
    if (this.bytes > this.maxBytes) throw new ComposerOperationDecodeError("Oversized SSE frame");
  }
  private endLine(frames: unknown[]): void {
    if (this.line !== "") { this.lines.push(this.line); this.line = ""; return; }
    const data: string[] = [];
    let event: string | undefined;
    for (const line of this.lines) {
      if (line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon < 0 ? line : line.slice(0, colon);
      let value = colon < 0 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "data") data.push(value);
      else if (field === "event" && event === undefined) event = value;
      else throw new ComposerOperationDecodeError("Unknown or duplicate SSE field");
    }
    if (data.length > 0) {
      let value: unknown;
      try { value = JSON.parse(data.join("\n")); } catch { throw new ComposerOperationDecodeError("Invalid SSE JSON"); }
      if (event !== undefined && (typeof value !== "object" || value === null || !("event" in value) || value.event !== event)) throw new ComposerOperationDecodeError("SSE event mismatch");
      frames.push(value);
    } else if (event !== undefined) throw new ComposerOperationDecodeError("SSE event missing data");
    this.lines = []; this.bytes = 0;
  }
}

function pollAfter(value: unknown): number { const result = integer(value, 100); if (result > 60000) throw new ComposerOperationDecodeError("Invalid poll interval"); return result; }
export function decodeComposerOperationFrame(value: unknown, sessionId: string, operationId: string): ComposerOperationFrame {
  const base = ["schema_version", "session_id", "operation_id", "sequence", "event"];
  const record = exactRecord(value, [...base, "payload"], base);
  if (record.schema_version !== "composer-operation-stream.v1" || canonicalUuid(record.session_id) !== sessionId || canonicalUuid(record.operation_id) !== operationId) throw new ComposerOperationDecodeError("Stream scope mismatch");
  const common = { schema_version: "composer-operation-stream.v1" as const, session_id: sessionId, operation_id: operationId, sequence: integer(record.sequence) };
  if (record.event === "heartbeat") {
    if ("payload" in record) throw new ComposerOperationDecodeError("Heartbeat payload forbidden");
    return { ...common, event: "heartbeat" };
  }
  if (record.event === "status") {
    const payload = exactRecord(record.payload, ["status", "cancel_requested", "deadline_remaining_ms"]);
    if ((payload.status !== "queued" && payload.status !== "running") || typeof payload.cancel_requested !== "boolean") throw new ComposerOperationDecodeError("Invalid status frame");
    return { ...common, event: "status", payload: { status: payload.status, cancel_requested: payload.cancel_requested, deadline_remaining_ms: integer(payload.deadline_remaining_ms) } };
  }
  if (record.event === "terminal") {
    const payload = exactRecord(record.payload, ["status"]);
    if (payload.status !== "completed" && payload.status !== "failed") throw new ComposerOperationDecodeError("Invalid terminal frame");
    return { ...common, event: "terminal", payload: { status: payload.status } };
  }
  if (record.event !== "progress") throw new ComposerOperationDecodeError("Unknown stream event");
  const payload = exactRecord(record.payload, ["session_operation_id", "session_operation_epoch", "request_token", "request_id", "phase", "headline", "evidence", "likely_next", "reason", "updated_at"]);
  const phases = ["idle", "starting", "calling_model", "using_tools", "validating", "saving", "complete", "failed", "cancelled"] as const;
  const reasons = ["convergence_composition_budget", "convergence_discovery_budget", "convergence_wall_clock_timeout", "tool_call_cap_exceeded", "provider_auth_failed", "provider_unavailable", "plugin_crash", "runtime_preflight_failed", "planner_repair_exhausted", "service_setup_failed", "admission_refused", "accounting_unavailable", "client_cancelled", "composer_idle", "composer_complete"] as const;
  const phase = phases.find((item) => item === payload.phase);
  const reason = payload.reason === null ? null : reasons.find((item) => item === payload.reason);
  if (phase === undefined || reason === undefined || ((phase === "failed" || phase === "cancelled") && reason === null)) throw new ComposerOperationDecodeError("Unknown progress vocabulary");
  const boundedText = (value: unknown, max: number): string => { const result = text(value); if ([...result].length < 1 || [...result].length > max) throw new ComposerOperationDecodeError("Unbounded progress text"); return result; };
  if (!Array.isArray(payload.evidence) || payload.evidence.length > 4) throw new ComposerOperationDecodeError("Unbounded progress evidence");
  const updated_at = text(payload.updated_at);
  if (!Number.isFinite(Date.parse(updated_at))) throw new ComposerOperationDecodeError("Invalid progress timestamp");
  return { ...common, event: "progress", payload: { session_operation_id: boundedText(payload.session_operation_id, 128), session_operation_epoch: integer(payload.session_operation_epoch, 1), request_token: boundedText(payload.request_token, 128), request_id: payload.request_id === null ? null : boundedText(payload.request_id, 128), phase, headline: boundedText(payload.headline, 180), evidence: payload.evidence.map((item) => boundedText(item, 180)), likely_next: payload.likely_next === null ? null : boundedText(payload.likely_next, 180), reason, updated_at } };
}
