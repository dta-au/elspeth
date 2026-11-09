/** Durable transport double for existing result/public-envelope parity tests.
 *
 * Admission returns immediately. The test's delayed legacy result belongs to
 * authoritative GET, so a pending response is not a socket-owned operation.
 * Transport/custody fault tests use explicit API doubles instead.
 */
import type { Mock } from "vitest";
import type { MessageWithStateResponse } from "@/types/index";
import type { ComposerOperationSnapshot, SubmittedCustody } from "@/types/composerOperations";

interface ComposerTransportDouble {
  sendMessage: Mock;
  recompose: Mock;
  submitComposerOperation: Mock;
  fetchComposerOperation: Mock;
  fetchComposerOperationStream: Mock;
  cancelComposerOperation: Mock;
}

export function installComposeTurnDouble(api: ComposerTransportDouble): void {
  const outcomes = new Map<string, Promise<ComposerOperationSnapshot>>();
  api.submitComposerOperation.mockImplementation(async (descriptor: SubmittedCustody) => {
    const ack = { operation_id: descriptor.operationId, kind: descriptor.kind, status: "running" as const, poll_after_ms: 1 };
    const base = { ...ack, cancel_requested: false, deadline_at: "2030-01-01T00:00:00Z", deadline_remaining_ms: 60000 };
    const turn: Promise<MessageWithStateResponse> = descriptor.kind === "compose_message"
      ? api.sendMessage(descriptor.sessionId, descriptor.body.content, descriptor.operationId, descriptor.body.state_id)
      : api.recompose(descriptor.sessionId, descriptor.body.expected_user_message_id, undefined, descriptor.body.state_id);
    const outcome: Promise<ComposerOperationSnapshot> = Promise.resolve(turn).then(
      (result) => ({ ...base, status: "completed", deadline_remaining_ms: 0, result, error: null }),
      (failure: unknown) => {
        if (!(typeof failure === "object" && failure !== null && "status" in failure && typeof failure.status === "number")) throw failure;
        const body = { ...failure };
        return { ...base, status: "failed", deadline_remaining_ms: 0, result: null, error: {
          http_status: failure.status, body, failure_code: "http_error", diagnostic_id: null,
          error_type: "error_type" in failure && typeof failure.error_type === "string" ? failure.error_type : null,
        } };
      },
    );
    // The admitted operation owns this result even if navigation detaches its
    // GET observer before the legacy provider double settles. Observe rejection
    // immediately; subsequent GET still receives the original rejected promise.
    void outcome.catch(() => undefined);
    outcomes.set(descriptor.operationId, outcome);
    return ack;
  });
  api.fetchComposerOperation.mockImplementation(async (_sessionId: string, operationId: string) => {
    const result = outcomes.get(operationId);
    if (result === undefined) return { kind: "operation_missing" };
    return result;
  });
  api.fetchComposerOperationStream.mockImplementation(async () => new Response(null, { status: 503 }));
  api.cancelComposerOperation.mockImplementation(async (_sessionId: string, operationId: string) => {
    const result = outcomes.get(operationId);
    if (result === undefined) throw new Error("Unknown compose double operation");
    return result;
  });
}
