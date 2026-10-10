import type { ComposerProgressSnapshot, MessageWithStateResponse } from "./index";

export type OperationKind = "compose_message" | "compose_recompose";
export type OperationStatus = "queued" | "running" | "completed" | "failed";
export type PrincipalScope = Readonly<{ principalId: string; authProvider: string }>;
export type SendMessageRequest = Readonly<{ operation_id: string; content: string; state_id: string | null }>;
export type RecomposeRequest = Readonly<{ operation_id: string; expected_user_message_id: string; state_id: string | null }>;
export type SubmittedCustody = Readonly<{
  mode: "submitted"; scope: PrincipalScope; sessionId: string; operationId: string; createdAt: number;
}> & (Readonly<{ kind: "compose_message"; body: SendMessageRequest }> | Readonly<{ kind: "compose_recompose"; body: RecomposeRequest }>);
export type ObserverCustody = Readonly<{
  mode: "observer"; scope: PrincipalScope; sessionId: string; operationId: string; kind: OperationKind; createdAt: number;
}>;
export type OperationCustody = SubmittedCustody | ObserverCustody;
export type SessionCustody = Readonly<{ foreground: OperationCustody | null; unresolvedSubmissions: ReadonlyMap<string, SubmittedCustody> }>;
export interface ComposerOperationAck { operation_id: string; kind: OperationKind; status: OperationStatus; poll_after_ms: number }
export interface ComposerOperationError { http_status: number; body: Record<string, unknown>; error_type: string | null; failure_code: "http_error" | "operation_failed" | "worker_lost" | "request_cancelled" | "deadline_expired"; diagnostic_id: string | null }
export interface ComposerOperationSnapshot extends ComposerOperationAck {
  cancel_requested: boolean; deadline_at: string; deadline_remaining_ms: number;
  result: MessageWithStateResponse | null; error: ComposerOperationError | null;
}
export type MissingOperation = { kind: "session_missing" } | { kind: "operation_missing" };
export type ComposerOperationFrame = {
  schema_version: "composer-operation-stream.v1"; session_id: string; operation_id: string; sequence: number;
} & (
  | { event: "heartbeat" }
  | { event: "status"; payload: { status: "queued" | "running"; cancel_requested: boolean; deadline_remaining_ms: number } }
  | { event: "terminal"; payload: { status: "completed" | "failed" } }
  | { event: "progress"; payload: Omit<ComposerProgressSnapshot, "session_id" | "inflight_requests"> & { session_operation_id: string; session_operation_epoch: number; request_token: string } }
);
