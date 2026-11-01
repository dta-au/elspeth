import { decodeCompositionState } from "./compositionDecoder";
import type { ChatMessage, ComposerProgressPhase, ComposerProgressReason, MessageWithStateResponse } from "@/types/index";
import type { ComposerActiveOperation, ComposerOperationError, ComposerOperationFailureOutcome, ComposerOperationFailurePage, ComposerOperationFrame, ComposerOperationPhase, ComposerOperationProgress, ComposerOperationPublicFailureType, ComposerOperationSegment, ComposerOperationSnapshot, ComposerOperationStatus, OperationKind, OperationStatus } from "@/types/composerOperations";

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
export function timestamp(value: unknown): string {
  const result = text(value);
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(result) || !Number.isFinite(Date.parse(result))) throw new ComposerOperationDecodeError("Invalid timestamp");
  return result;
}
function failureCode(value: unknown): ComposerOperationError["failure_code"] {
  const codes = ["http_error", "operation_failed", "worker_lost", "request_cancelled", "deadline_expired"] as const;
  const code = codes.find((item) => item === value);
  if (code === undefined) throw new ComposerOperationDecodeError("Unknown failure code");
  return code;
}
function failureOutcome(value: unknown, sessionId: string): ComposerOperationFailureOutcome {
  const record = exactRecord(value, ["session_id", "operation_id", "kind", "user_message_id", "settled_at", "failure_code", "error_type", "diagnostic_id", "request_id", "saved_revisions"]);
  if (canonicalUuid(record.session_id) !== sessionId) throw new ComposerOperationDecodeError("Failure outcome scope mismatch");
  const code = failureCode(record.failure_code);
  const diagnostic = record.diagnostic_id === null ? null : canonicalUuid(record.diagnostic_id);
  if ((code === "operation_failed") !== (diagnostic !== null)) throw new ComposerOperationDecodeError("Invalid diagnostic binding");
  const categories: readonly ComposerOperationPublicFailureType[] = ["request_cancelled", "composer_operation_deadline_expired", "composer_operation_worker_lost", "operation_failed", "audit_integrity_error", "database_unavailable", "executor_unavailable", "storage_unavailable", "stale_compose_state", "fingerprint_key_missing", "secret_decryption_failed", "composer_mutation_authority_revoked"];
  const category = record.error_type === null ? null : categories.find((item) => item === record.error_type);
  if (category === undefined) throw new ComposerOperationDecodeError("Unknown public failure category");
  const requestId = record.request_id === null ? null : text(record.request_id);
  if (requestId !== null && (requestId.length < 1 || requestId.length > 128)) throw new ComposerOperationDecodeError("Invalid request reference");
  const saved = exactRecord(record.saved_revisions, ["attribution", "count", "latest_state_id", "latest_version"]);
  if (saved.attribution !== "fenced" && saved.attribution !== "unstarted" && saved.attribution !== "unavailable") throw new ComposerOperationDecodeError("Unknown revision attribution");
  const count = saved.count === null ? null : integer(saved.count);
  const latestId = saved.latest_state_id === null ? null : canonicalUuid(saved.latest_state_id);
  const version = saved.latest_version === null ? null : integer(saved.latest_version, 1);
  if (saved.attribution === "unavailable") {
    if (count !== null || latestId !== null || version !== null) throw new ComposerOperationDecodeError("Unavailable revisions must remain unknown");
  } else if (count === null || (saved.attribution === "unstarted" && count !== 0) || (count === 0 ? latestId !== null || version !== null : latestId === null || version === null)) {
    throw new ComposerOperationDecodeError("Inconsistent saved revision identity");
  }
  return {
    session_id: sessionId, operation_id: canonicalUuid(record.operation_id), kind: operationKind(record.kind),
    user_message_id: record.user_message_id === null ? null : canonicalUuid(record.user_message_id), settled_at: timestamp(record.settled_at),
    failure_code: code, error_type: category, diagnostic_id: diagnostic, request_id: requestId,
    saved_revisions: { attribution: saved.attribution, count, latest_state_id: latestId, latest_version: version },
  };
}
export function decodeComposerOperationFailurePage(value: unknown, sessionId: string, operationId?: string): ComposerOperationFailurePage {
  const record = exactRecord(value, ["session_id", "items", "next_cursor"]);
  if (canonicalUuid(record.session_id) !== sessionId) throw new ComposerOperationDecodeError("Failure page scope mismatch");
  if (!Array.isArray(record.items) || record.items.length > 100) throw new ComposerOperationDecodeError("Unbounded failure page");
  const items = record.items.map((item) => failureOutcome(item, sessionId));
  if (new Set(items.map((item) => item.operation_id)).size !== items.length || (operationId !== undefined && items.some((item) => item.operation_id !== operationId))) throw new ComposerOperationDecodeError("Failure page operation mismatch");
  const nextCursor = record.next_cursor === null ? null : text(record.next_cursor);
  if (nextCursor !== null && (nextCursor.length < 1 || nextCursor.length > 512)) throw new ComposerOperationDecodeError("Invalid failure cursor");
  return { session_id: sessionId, items, next_cursor: nextCursor };
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
export function decodeError(value: unknown, sessionId: string): ComposerOperationError {
  const record = exactRecord(value, ["http_status", "body", "error_type", "failure_code", "diagnostic_id"]);
  const status = integer(record.http_status, 400);
  if (status > 599) throw new ComposerOperationDecodeError("Invalid HTTP error status");
  if (typeof record.body !== "object" || record.body === null || Array.isArray(record.body)) throw new ComposerOperationDecodeError("Invalid error body");
  validateComposerRecoveryBody(record.body, sessionId);
  const code = failureCode(record.failure_code);
  const diagnostic_id = record.diagnostic_id === null ? null : canonicalUuid(record.diagnostic_id);
  if ((code === "operation_failed") !== (diagnostic_id !== null)) throw new ComposerOperationDecodeError("Invalid diagnostic binding");
  return { http_status: status, body: record.body as Record<string, unknown>, error_type: record.error_type === null ? null : text(record.error_type), failure_code: code, diagnostic_id };
}
function decodeTerminalMessage(value: unknown, sessionId: string, role: "assistant" | "system"): ChatMessage {
  const message = exactRecord(value, ["id", "operation_id", "session_id", "role", "content", "raw_content", "segments", "tool_calls", "created_at", "composition_state_id", "tool_call_id", "parent_assistant_id", "sequence_no", "rejection"]);
  canonicalUuid(message.id);
  if (message.session_id !== sessionId || message.role !== role) throw new ComposerOperationDecodeError("Final message scope mismatch");
  text(message.content);
  timestamp(message.created_at);
  if (message.operation_id !== undefined && message.operation_id !== null) canonicalUuid(message.operation_id);
  for (const field of ["composition_state_id", "tool_call_id", "parent_assistant_id"]) if (message[field] !== undefined && message[field] !== null) text(message[field]);
  if (message.raw_content !== undefined && message.raw_content !== null) text(message.raw_content);
  if (message.sequence_no !== undefined && message.sequence_no !== null) integer(message.sequence_no);
  if (!Array.isArray(message.segments) || (message.tool_calls !== null && !Array.isArray(message.tool_calls))) throw new ComposerOperationDecodeError("Invalid final lists");
  if (role === "system" && message.tool_calls !== null) throw new ComposerOperationDecodeError("Recovery receipt cannot carry tool calls");
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
  return message as unknown as ChatMessage;
}
export function decodeComposerResult(value: unknown, sessionId: string): MessageWithStateResponse {
  const record = exactRecord(value, ["message", "state", "proposals", "recovery_notice"], ["message", "state", "proposals"]);
  const message = decodeTerminalMessage(record.message, sessionId, "assistant");
  const recoveryNotice = record.recovery_notice == null ? null : decodeTerminalMessage(record.recovery_notice, sessionId, "system");
  if (recoveryNotice !== null && (recoveryNotice.id === message.id || recoveryNotice.operation_id !== message.operation_id)) throw new ComposerOperationDecodeError("Recovery receipt binding mismatch");
  if (!Array.isArray(record.proposals)) throw new ComposerOperationDecodeError("Invalid final lists");
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
  if (recoveryNotice !== null && state !== null && recoveryNotice.composition_state_id !== state.id) throw new ComposerOperationDecodeError("Recovery receipt state mismatch");
  return { message, state, proposals: record.proposals as MessageWithStateResponse["proposals"], ...(record.recovery_notice === undefined ? {} : { recovery_notice: recoveryNotice }) };
}
export function pollAfter(value: unknown): number { const result = integer(value, 100); if (result > 60000) throw new ComposerOperationDecodeError("Invalid poll interval"); return result; }

const STATUS_KEYS = ["operation_id", "kind", "status", "phase", "status_revision", "created_at", "observed_at", "poll_after_ms", "cancel_requested", "segment"] as const;
const PHASES: readonly ComposerOperationPhase[] = ["queued", "executing", "continuation_queued", "reconciling", "retry_waiting", "settled"];
const ACTIVE_PHASES: readonly ComposerOperationPhase[] = ["queued", "executing", "continuation_queued", "reconciling", "retry_waiting"];

function phase(value: unknown): ComposerOperationPhase {
  const found = PHASES.find((item) => item === value);
  if (found === undefined) throw new ComposerOperationDecodeError("Unknown operation phase");
  return found;
}
function segment(value: unknown): ComposerOperationSegment | null {
  if (value === null) return null;
  const record = exactRecord(value, ["sequence", "generation", "state", "deadline_at"]);
  const state = record.state === "queued" || record.state === "running" || record.state === "interrupted" ? record.state : null;
  if (state === null) throw new ComposerOperationDecodeError("Unknown segment state");
  const deadline_at = record.deadline_at === null ? null : timestamp(record.deadline_at);
  if ((state === "running") !== (deadline_at !== null)) throw new ComposerOperationDecodeError("Only a running segment has a deadline");
  return Object.freeze({ sequence: integer(record.sequence, 1), generation: integer(record.generation, 1), state, deadline_at });
}
function statusFields(record: Record<string, unknown>, operationId: string): ComposerOperationStatus {
  const operation_id = canonicalUuid(record.operation_id);
  if (operation_id !== operationId) throw new ComposerOperationDecodeError("Operation scope mismatch");
  const status = operationStatus(record.status);
  const decodedPhase = phase(record.phase);
  if (typeof record.cancel_requested !== "boolean") throw new ComposerOperationDecodeError("Invalid cancel flag");
  const live = status === "queued" || status === "running";
  const decodedSegment = segment(record.segment);
  if (live !== ACTIVE_PHASES.includes(decodedPhase)) throw new ComposerOperationDecodeError("Operation phase does not match status");
  if (live !== (decodedSegment !== null)) throw new ComposerOperationDecodeError("Live segment mismatch");
  if (status === "completed" && record.cancel_requested) throw new ComposerOperationDecodeError("Completed cancellation conflict");
  return Object.freeze({
    operation_id, kind: operationKind(record.kind), status, phase: decodedPhase,
    status_revision: integer(record.status_revision), created_at: timestamp(record.created_at), observed_at: timestamp(record.observed_at),
    poll_after_ms: pollAfter(record.poll_after_ms), cancel_requested: record.cancel_requested, segment: decodedSegment,
  });
}
export function decodeComposerOperationStatus(value: unknown, operationId: string): ComposerOperationStatus {
  return statusFields(exactRecord(value, STATUS_KEYS), operationId);
}
export function decodeComposerOperationSnapshot(value: unknown, sessionId: string, operationId: string): ComposerOperationSnapshot {
  const record = exactRecord(value, [...STATUS_KEYS, "result", "error"]);
  const fields = statusFields(record, operationId);
  const live = fields.status === "queued" || fields.status === "running";
  if ((live && (record.result !== null || record.error !== null)) || (fields.status === "completed" && (record.result === null || record.error !== null)) || (fields.status === "failed" && (record.error === null || record.result !== null))) {
    throw new ComposerOperationDecodeError("Invalid terminal payload");
  }
  const result = record.result === null ? null : decodeComposerResult(record.result, sessionId);
  if (result?.recovery_notice != null && (result.message.operation_id !== fields.operation_id || result.recovery_notice.operation_id !== fields.operation_id)) throw new ComposerOperationDecodeError("Recovery receipt operation mismatch");
  return Object.freeze({ ...fields, result, error: record.error === null ? null : decodeError(record.error, sessionId) });
}
export function decodeComposerActiveOperation(value: unknown): ComposerActiveOperation {
  const record = exactRecord(value, ["operation_id", "kind", "status"]);
  if (record.status !== "queued" && record.status !== "running") throw new ComposerOperationDecodeError("Active operation is not live");
  return Object.freeze({ operation_id: canonicalUuid(record.operation_id), kind: operationKind(record.kind), status: record.status });
}
// The public progress taxonomy (ComposerProgressPhase/ComposerProgressReason in
// types/index.ts, mirrored from contracts/composer_progress.py).
const PROGRESS_PHASES: readonly ComposerProgressPhase[] = ["idle", "starting", "calling_model", "using_tools", "validating", "saving", "complete", "failed", "cancelled"];
const PROGRESS_REASONS: readonly ComposerProgressReason[] = ["convergence_composition_budget", "convergence_discovery_budget", "convergence_wall_clock_timeout", "tool_call_cap_exceeded", "provider_auth_failed", "provider_unavailable", "plugin_crash", "runtime_preflight_failed", "planner_repair_exhausted", "service_setup_failed", "mutation_authority_revoked", "admission_refused", "context_unsupported", "accounting_unavailable", "client_cancelled", "composer_idle", "composer_complete"];
function boundedText(value: unknown, max: number): string {
  const result = text(value);
  if ([...result].length < 1 || [...result].length > max) throw new ComposerOperationDecodeError("Unbounded progress text");
  return result;
}
function progress(value: unknown): ComposerOperationProgress {
  const record = exactRecord(value, ["request_id", "phase", "headline", "evidence", "likely_next", "reason", "updated_at", "segment_sequence", "segment_generation"]);
  const progressPhase = PROGRESS_PHASES.find((item) => item === record.phase);
  const reason = record.reason === null ? null : PROGRESS_REASONS.find((item) => item === record.reason);
  if (progressPhase === undefined || reason === undefined || ((progressPhase === "failed" || progressPhase === "cancelled") && reason === null)) throw new ComposerOperationDecodeError("Unknown progress vocabulary");
  if (!Array.isArray(record.evidence) || record.evidence.length > 4) throw new ComposerOperationDecodeError("Unbounded progress evidence");
  return {
    request_id: record.request_id === null ? null : boundedText(record.request_id, 128),
    phase: progressPhase,
    headline: boundedText(record.headline, 180),
    evidence: record.evidence.map((item) => boundedText(item, 180)),
    likely_next: record.likely_next === null ? null : boundedText(record.likely_next, 180),
    reason,
    updated_at: timestamp(record.updated_at),
    segment_sequence: integer(record.segment_sequence, 1),
    segment_generation: integer(record.segment_generation, 1),
  };
}
export function decodeComposerOperationFrame(value: { id: string | null; data: unknown }, sessionId: string, operationId: string): ComposerOperationFrame {
  const event = typeof value.data === "object" && value.data !== null && "event" in value.data ? (value.data as { event: unknown }).event : undefined;
  const base = ["session_id", "operation_id", "sequence", "event"];
  const record = exactRecord(value.data, event === "heartbeat" || event === "reconnect" ? base : [...base, "payload"]);
  if (canonicalUuid(record.session_id) !== sessionId || canonicalUuid(record.operation_id) !== operationId) throw new ComposerOperationDecodeError("Stream scope mismatch");
  const common = { session_id: sessionId, operation_id: operationId, sequence: integer(record.sequence), id: value.id };
  const carriesId = event === "status" || event === "terminal";
  if (carriesId !== (value.id !== null)) throw new ComposerOperationDecodeError("Only status and terminal frames carry an id");
  if (event === "heartbeat" || event === "reconnect") return { ...common, event };
  if (event === "status") {
    const payload = decodeComposerOperationStatus(record.payload, operationId);
    if (String(payload.status_revision) !== value.id) throw new ComposerOperationDecodeError("SSE id does not match the revision");
    return { ...common, event, payload };
  }
  if (event === "terminal") {
    const payload = exactRecord(record.payload, ["status", "phase", "status_revision"]);
    if ((payload.status !== "completed" && payload.status !== "failed") || payload.phase !== "settled") throw new ComposerOperationDecodeError("Invalid terminal frame");
    const status_revision = integer(payload.status_revision);
    if (String(status_revision) !== value.id) throw new ComposerOperationDecodeError("SSE id does not match the revision");
    return { ...common, event, payload: { status: payload.status, phase: "settled", status_revision } };
  }
  if (event === "progress") return { ...common, event, payload: progress(record.payload) };
  throw new ComposerOperationDecodeError("Unknown stream event");
}

/** Bounded incremental SSE framing: `id`, `event` and `data` fields. UTF-8 is admitted before any JSON is parsed. */
export class ComposerSseDecoder {
  private readonly utf8 = new TextDecoder("utf-8", { fatal: true });
  private line = "";
  private lines: string[] = [];
  private bytes = 0;
  private pendingCR = false;
  private readonly encode = new TextEncoder();
  constructor(private readonly maxBytes = 64 * 1024) {}
  push(chunk: Uint8Array): { id: string | null; data: unknown }[] {
    const frames: { id: string | null; data: unknown }[] = [];
    for (let offset = 0; offset < chunk.byteLength; offset += 4096) frames.push(...this.consume(this.utf8.decode(chunk.subarray(offset, offset + 4096), { stream: true })));
    return frames;
  }
  finish(): { id: string | null; data: unknown }[] {
    const frames = this.consume(this.utf8.decode());
    if (this.pendingCR) { this.pendingCR = false; this.endLine(frames); }
    if (this.line !== "" || this.lines.length !== 0 || this.bytes !== 0) throw new ComposerOperationDecodeError("Truncated SSE frame");
    return frames;
  }
  private consume(input: string): { id: string | null; data: unknown }[] {
    const frames: { id: string | null; data: unknown }[] = [];
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
  private endLine(frames: { id: string | null; data: unknown }[]): void {
    if (this.line !== "") { this.lines.push(this.line); this.line = ""; return; }
    const data: string[] = [];
    let event: string | undefined;
    let id: string | undefined;
    for (const line of this.lines) {
      if (line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon < 0 ? line : line.slice(0, colon);
      let value = colon < 0 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "data") data.push(value);
      else if (field === "event" && event === undefined) event = value;
      else if (field === "id" && id === undefined) {
        if (!/^[0-9]{1,19}$/.test(value)) throw new ComposerOperationDecodeError("Invalid SSE id");
        id = value;
      } else throw new ComposerOperationDecodeError("Unknown or duplicate SSE field");
    }
    if (data.length > 0) {
      let value: unknown;
      try { value = JSON.parse(data.join("\n")); } catch { throw new ComposerOperationDecodeError("Invalid SSE JSON"); }
      if (event !== undefined && (typeof value !== "object" || value === null || !("event" in value) || value.event !== event)) throw new ComposerOperationDecodeError("SSE event mismatch");
      frames.push({ id: id ?? null, data: value });
    } else if (event !== undefined || id !== undefined) throw new ComposerOperationDecodeError("SSE frame missing data");
    this.lines = []; this.bytes = 0;
  }
}
