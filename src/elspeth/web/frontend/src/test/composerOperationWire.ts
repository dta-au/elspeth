/** Composer operation wire builders for tests: one valid shape per record. */
import type { MessageWithStateResponse } from "@/types/index";
import type { ComposerOperationError, ComposerOperationSnapshot, ComposerOperationStatus } from "@/types/composerOperations";

const at = "2030-01-01T00:00:00Z";
let revision = 0;

/** A live (running) status; `overrides` may make it any valid state. */
export function operationStatus(operationId: string, overrides: Partial<ComposerOperationStatus> = {}): ComposerOperationStatus {
  revision += 1;
  return {
    operation_id: operationId, kind: "compose_message", status: "running", phase: "executing", status_revision: revision,
    created_at: at, observed_at: at, poll_after_ms: 100, cancel_requested: false,
    segment: { sequence: 1, generation: 1, state: "queued", deadline_at: null }, ...overrides,
  };
}
export function liveSnapshot(operationId: string, overrides: Partial<ComposerOperationStatus> = {}): ComposerOperationSnapshot {
  return { ...operationStatus(operationId, overrides), result: null, error: null };
}
export function completedSnapshot(operationId: string, result: MessageWithStateResponse, overrides: Partial<ComposerOperationStatus> = {}): ComposerOperationSnapshot {
  return { ...operationStatus(operationId, { status: "completed", phase: "settled", segment: null, ...overrides }), result, error: null };
}
export function failedSnapshot(operationId: string, error: ComposerOperationError, overrides: Partial<ComposerOperationStatus> = {}): ComposerOperationSnapshot {
  return { ...operationStatus(operationId, { status: "failed", phase: "settled", segment: null, ...overrides }), result: null, error };
}

/** One SSE frame on the wire; status and terminal frames carry `id:`. */
export function sseFrame(sessionId: string, operationId: string, event: string, sequence: number, payload?: unknown, eventId?: number): string {
  const data = { session_id: sessionId, operation_id: operationId, sequence, event, ...(payload === undefined ? {} : { payload }) };
  return `${eventId === undefined ? "" : `id: ${eventId}\n`}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}
export function sseResponse(...frames: string[]): Response {
  return new Response(new ReadableStream({ start(controller) { for (const item of frames) controller.enqueue(new TextEncoder().encode(item)); controller.close(); } }), { headers: { "Content-Type": "text/event-stream" } });
}
/** A stream the server cannot serve now: the observer reads the durable GET. */
export function streamUnavailable(): Response { return new Response(null, { status: 503 }); }
