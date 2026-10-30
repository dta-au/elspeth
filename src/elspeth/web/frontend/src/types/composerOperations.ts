import type { ComposerProgressSnapshot, MessageWithStateResponse } from "./index";

export type OperationKind = "compose_message" | "compose_recompose";
export type OperationStatus = "queued" | "running" | "completed" | "failed";
export type PrincipalScope = Readonly<{ principalId: string; authProvider: string }>;
export type SendMessageRequest = Readonly<{ operation_id: string; content: string; state_id: string | null }>;
export type RecomposeRequest = Readonly<{ operation_id: string; expected_user_message_id: string; state_id: string | null }>;
/** Composer-Operation-Version. A missing value means 1 (pre-upgrade custody, old servers). */
export type ComposerProtocolVersion = 1 | 2;
export type SubmittedCustody = Readonly<{
  mode: "submitted"; scope: PrincipalScope; sessionId: string; operationId: string; createdAt: number; protocolVersion?: ComposerProtocolVersion;
}> & (Readonly<{ kind: "compose_message"; body: SendMessageRequest }> | Readonly<{ kind: "compose_recompose"; body: RecomposeRequest }>);
export type ObserverCustody = Readonly<{
  mode: "observer"; scope: PrincipalScope; sessionId: string; operationId: string; kind: OperationKind; createdAt: number; protocolVersion?: ComposerProtocolVersion;
}>;
export type OperationCustody = SubmittedCustody | ObserverCustody;
export type SessionCustody = Readonly<{ foreground: OperationCustody | null; unresolvedSubmissions: ReadonlyMap<string, SubmittedCustody> }>;
/** protocolVersion is the server's echoed Composer-Operation-Version (missing echo: 1). */
export interface ComposerOperationAck { operation_id: string; kind: OperationKind; status: OperationStatus; poll_after_ms: number; protocolVersion?: ComposerProtocolVersion }
export interface ComposerOperationError { http_status: number; body: Record<string, unknown>; error_type: string | null; failure_code: "http_error" | "operation_failed" | "worker_lost" | "request_cancelled" | "deadline_expired"; diagnostic_id: string | null }
export type ComposerOperationPublicFailureType = "request_cancelled" | "composer_operation_deadline_expired" | "composer_operation_worker_lost" | "operation_failed" | "audit_integrity_error" | "database_unavailable" | "storage_unavailable" | "stale_compose_state" | "fingerprint_key_missing" | "secret_decryption_failed" | "composer_mutation_authority_revoked";
export interface ComposerOperationSavedRevisions {
  attribution: "fenced" | "unstarted" | "unavailable";
  count: number | null;
  latest_state_id: string | null;
  latest_version: number | null;
}
export interface ComposerOperationFailureOutcome {
  session_id: string; operation_id: string; kind: OperationKind; user_message_id: string | null;
  settled_at: string; failure_code: ComposerOperationError["failure_code"];
  error_type: ComposerOperationPublicFailureType | null; diagnostic_id: string | null; request_id: string | null;
  saved_revisions: ComposerOperationSavedRevisions;
}
/** An authenticated observed terminal may precede retained-history enrichment. */
export type ComposerOperationFailureNotice = Omit<ComposerOperationFailureOutcome, "settled_at"> & { settled_at: string | null };
export interface ComposerOperationFailurePage {
  session_id: string; items: ComposerOperationFailureOutcome[]; next_cursor: string | null;
}
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

// --- Composer-Operation-Version 2 (Continuation) --------------------------
export type ComposerPhaseV2 = "queued" | "executing" | "continuation_queued" | "reconciling" | "retry_waiting" | "settled";
export type ComposerSegmentV2 = Readonly<{ sequence: number; generation: number; state: "queued" | "running" | "interrupted"; deadline_at: string | null }>;
export type ComposerOperationStatusV2 = Readonly<{
  protocol_version: 2; operation_id: string; kind: OperationKind; status: OperationStatus; phase: ComposerPhaseV2;
  status_revision: number; created_at: string; observed_at: string; poll_after_ms: number; cancel_requested: boolean;
  segment: ComposerSegmentV2 | null;
}>;
export type ComposerOperationSnapshotV2 = ComposerOperationStatusV2 & Readonly<{ result: MessageWithStateResponse | null; error: ComposerOperationError | null }>;
export type ComposerProgressV2 = Omit<ComposerProgressSnapshot, "session_id" | "inflight_requests" | "operation_id" | "session_operation_id" | "session_operation_epoch" | "request_token"> & { segment_sequence: number; segment_generation: number };
export type ComposerOperationFrameV2 = {
  schema_version: "composer-operation-stream.v2"; session_id: string; operation_id: string; sequence: number; id: string | null;
} & (
  | { event: "heartbeat" }
  | { event: "reconnect" }
  | { event: "status"; payload: ComposerOperationStatusV2 }
  | { event: "terminal"; payload: { status: "completed" | "failed"; phase: "settled"; status_revision: number } }
  | { event: "progress"; payload: ComposerProgressV2 }
);
export type ComposerActiveOperation = Readonly<{ operation_id: string; kind: OperationKind; lifecycle_version: ComposerProtocolVersion; status: "queued" | "running" }>;
