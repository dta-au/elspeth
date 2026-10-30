// Composer-Operation-Version 2 (Continuation) decoding. The v1 decoder is
// unchanged and keeps rejecting SSE `id:` lines; v2 streams carry them.
import { ComposerOperationDecodeError, canonicalUuid, decodeComposerResult, decodeError, exactRecord, integer, operationKind, operationStatus, pollAfter, text, timestamp } from "./composerOperationDecoder";
import type { ComposerActiveOperation, ComposerOperationFrameV2, ComposerOperationSnapshotV2, ComposerOperationStatusV2, ComposerPhaseV2, ComposerProgressV2, ComposerSegmentV2 } from "@/types/composerOperations";

const STATUS_KEYS = ["protocol_version", "operation_id", "kind", "status", "phase", "status_revision", "created_at", "observed_at", "poll_after_ms", "cancel_requested", "segment"] as const;
const PHASES: readonly ComposerPhaseV2[] = ["queued", "executing", "continuation_queued", "reconciling", "retry_waiting", "settled"];
const ACTIVE_PHASES: readonly ComposerPhaseV2[] = ["queued", "executing", "continuation_queued", "reconciling", "retry_waiting"];

function phase(value: unknown): ComposerPhaseV2 {
  const found = PHASES.find((item) => item === value);
  if (found === undefined) throw new ComposerOperationDecodeError("Unknown v2 phase");
  return found;
}
function segment(value: unknown): ComposerSegmentV2 | null {
  if (value === null) return null;
  const record = exactRecord(value, ["sequence", "generation", "state", "deadline_at"]);
  const state = record.state === "queued" || record.state === "running" || record.state === "interrupted" ? record.state : null;
  if (state === null) throw new ComposerOperationDecodeError("Unknown v2 segment state");
  const deadline_at = record.deadline_at === null ? null : timestamp(record.deadline_at);
  if ((state === "running") !== (deadline_at !== null)) throw new ComposerOperationDecodeError("Only a running segment has a deadline");
  return Object.freeze({ sequence: integer(record.sequence, 1), generation: integer(record.generation, 1), state, deadline_at });
}
function statusFields(record: Record<string, unknown>, operationId: string): ComposerOperationStatusV2 {
  if (record.protocol_version !== 2) throw new ComposerOperationDecodeError("Not a v2 projection");
  const operation_id = canonicalUuid(record.operation_id);
  if (operation_id !== operationId) throw new ComposerOperationDecodeError("Operation scope mismatch");
  const status = operationStatus(record.status);
  const decodedPhase = phase(record.phase);
  if (typeof record.cancel_requested !== "boolean") throw new ComposerOperationDecodeError("Invalid cancel flag");
  const live = status === "queued" || status === "running";
  const decodedSegment = segment(record.segment);
  if (live !== ACTIVE_PHASES.includes(decodedPhase)) throw new ComposerOperationDecodeError("v2 phase does not match status");
  if (live !== (decodedSegment !== null)) throw new ComposerOperationDecodeError("v2 live segment mismatch");
  if (status === "completed" && record.cancel_requested) throw new ComposerOperationDecodeError("Completed cancellation conflict");
  return Object.freeze({
    protocol_version: 2, operation_id, kind: operationKind(record.kind), status, phase: decodedPhase,
    status_revision: integer(record.status_revision), created_at: timestamp(record.created_at), observed_at: timestamp(record.observed_at),
    poll_after_ms: pollAfter(record.poll_after_ms), cancel_requested: record.cancel_requested, segment: decodedSegment,
  });
}
export function decodeComposerOperationStatusV2(value: unknown, operationId: string): ComposerOperationStatusV2 {
  return statusFields(exactRecord(value, STATUS_KEYS), operationId);
}
export function decodeComposerOperationSnapshotV2(value: unknown, sessionId: string, operationId: string): ComposerOperationSnapshotV2 {
  const record = exactRecord(value, [...STATUS_KEYS, "result", "error"]);
  const fields = statusFields(record, operationId);
  const live = fields.status === "queued" || fields.status === "running";
  if ((live && (record.result !== null || record.error !== null)) || (fields.status === "completed" && (record.result === null || record.error !== null)) || (fields.status === "failed" && (record.error === null || record.result !== null))) {
    throw new ComposerOperationDecodeError("Invalid v2 terminal payload");
  }
  return Object.freeze({
    ...fields,
    result: record.result === null ? null : decodeComposerResult(record.result, sessionId),
    error: record.error === null ? null : decodeError(record.error, sessionId),
  });
}
export function decodeComposerActiveOperation(value: unknown): ComposerActiveOperation {
  const record = exactRecord(value, ["operation_id", "kind", "lifecycle_version", "status"]);
  if (record.lifecycle_version !== 1 && record.lifecycle_version !== 2) throw new ComposerOperationDecodeError("Unknown lifecycle");
  if (record.status !== "queued" && record.status !== "running") throw new ComposerOperationDecodeError("Active operation is not live");
  return Object.freeze({ operation_id: canonicalUuid(record.operation_id), kind: operationKind(record.kind), lifecycle_version: record.lifecycle_version, status: record.status });
}
function progress(value: unknown): ComposerProgressV2 {
  const record = exactRecord(value, ["request_id", "phase", "headline", "evidence", "likely_next", "reason", "updated_at", "segment_sequence", "segment_generation"]);
  if (!Array.isArray(record.evidence) || record.evidence.length > 4) throw new ComposerOperationDecodeError("Invalid progress evidence");
  return {
    request_id: record.request_id === null ? null : text(record.request_id),
    phase: text(record.phase) as ComposerProgressV2["phase"],
    headline: text(record.headline),
    evidence: record.evidence.map(text),
    likely_next: record.likely_next === null ? null : text(record.likely_next),
    reason: record.reason === null ? null : (text(record.reason) as ComposerProgressV2["reason"]),
    updated_at: timestamp(record.updated_at),
    segment_sequence: integer(record.segment_sequence, 1),
    segment_generation: integer(record.segment_generation, 1),
  };
}
export function decodeComposerOperationFrameV2(value: { id: string | null; data: unknown }, sessionId: string, operationId: string): ComposerOperationFrameV2 {
  const event = typeof value.data === "object" && value.data !== null && "event" in value.data ? (value.data as { event: unknown }).event : undefined;
  const base = ["schema_version", "session_id", "operation_id", "sequence", "event"];
  const record = exactRecord(value.data, event === "heartbeat" || event === "reconnect" ? base : [...base, "payload"]);
  if (record.schema_version !== "composer-operation-stream.v2" || canonicalUuid(record.session_id) !== sessionId || canonicalUuid(record.operation_id) !== operationId) {
    throw new ComposerOperationDecodeError("Stream scope mismatch");
  }
  const common = { schema_version: "composer-operation-stream.v2" as const, session_id: sessionId, operation_id: operationId, sequence: integer(record.sequence), id: value.id };
  const carriesId = event === "status" || event === "terminal";
  if (carriesId !== (value.id !== null)) throw new ComposerOperationDecodeError("Only status and terminal frames carry an id");
  if (event === "heartbeat" || event === "reconnect") return { ...common, event };
  if (event === "status") {
    const payload = decodeComposerOperationStatusV2(record.payload, operationId);
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

/** Bounded incremental SSE framing for v2: `id`, `event` and `data` fields. */
export class ComposerSseDecoderV2 {
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
