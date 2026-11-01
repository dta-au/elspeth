/** Durable transport double for existing result/public-envelope parity tests.
 *
 * Admission returns immediately. The test's delayed provider result belongs to
 * the authoritative GET, so a pending response is not a socket-owned operation.
 * The stream answers 503, so the observer reads the durable GET.
 * Transport/custody fault tests use explicit API doubles instead.
 */
import type { Mock } from "vitest";
import type { MessageWithStateResponse } from "@/types/index";
import type { ComposerOperationSnapshot, SubmittedCustody } from "@/types/composerOperations";
import { completedSnapshot, failedSnapshot, liveSnapshot, streamUnavailable } from "./composerOperationWire";

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
    const kind = { kind: descriptor.kind };
    const turn: Promise<MessageWithStateResponse> = descriptor.kind === "compose_message"
      ? api.sendMessage(descriptor.sessionId, descriptor.body.content, descriptor.operationId, descriptor.body.state_id)
      : api.recompose(descriptor.sessionId, descriptor.body.expected_user_message_id, undefined, descriptor.body.state_id);
    const outcome: Promise<ComposerOperationSnapshot> = Promise.resolve(turn).then(
      (result) => completedSnapshot(descriptor.operationId, result, kind),
      (failure: unknown) => {
        if (!(typeof failure === "object" && failure !== null && "status" in failure && typeof failure.status === "number")) throw failure;
        const body = { ...failure };
        return failedSnapshot(descriptor.operationId, {
          http_status: failure.status, body, failure_code: "http_error", diagnostic_id: null,
          error_type: "error_type" in failure && typeof failure.error_type === "string" ? failure.error_type : null,
        }, kind);
      },
    );
    // The admitted operation owns this result even if navigation detaches its
    // GET observer before the provider double settles. Observe rejection
    // immediately; subsequent GET still receives the original rejected promise.
    void outcome.catch(() => undefined);
    outcomes.set(descriptor.operationId, outcome);
    return liveSnapshot(descriptor.operationId, kind);
  });
  api.fetchComposerOperation.mockImplementation(async (_sessionId: string, operationId: string) => {
    const result = outcomes.get(operationId);
    if (result === undefined) return { kind: "operation_missing" };
    return result;
  });
  api.fetchComposerOperationStream.mockImplementation(async () => streamUnavailable());
  api.cancelComposerOperation.mockImplementation(async (_sessionId: string, operationId: string) => {
    const result = outcomes.get(operationId);
    if (result === undefined) throw new Error("Unknown compose double operation");
    return result;
  });
}
