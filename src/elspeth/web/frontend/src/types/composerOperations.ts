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
export interface ComposerOperationError { http_status: number; body: Record<string, unknown>; error_type: string | null; failure_code: "http_error" | "operation_failed" | "worker_lost" | "request_cancelled" | "deadline_expired"; diagnostic_id: string | null }
export type ComposerOperationPublicFailureType = "request_cancelled" | "composer_operation_deadline_expired" | "composer_operation_worker_lost" | "operation_failed" | "audit_integrity_error" | "database_unavailable" | "executor_unavailable" | "storage_unavailable" | "stale_compose_state" | "fingerprint_key_missing" | "secret_decryption_failed" | "composer_mutation_authority_revoked" | "composer_admission_refused" | "paid_result_overflow";
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
export type MissingOperation = { kind: "session_missing" } | { kind: "operation_missing" };

// --- Composer operation (Continuation) projection -------------------------
export type ComposerOperationPhase = "queued" | "executing" | "continuation_queued" | "reconciling" | "retry_waiting" | "settled";
export type ComposerOperationSegment = Readonly<{ sequence: number; generation: number; state: "queued" | "running" | "interrupted"; deadline_at: string | null }>;
export type ComposerOperationStatus = Readonly<{
  operation_id: string; kind: OperationKind; status: OperationStatus; phase: ComposerOperationPhase;
  status_revision: number; created_at: string; observed_at: string; poll_after_ms: number; cancel_requested: boolean;
  segment: ComposerOperationSegment | null;
}>;
export type ComposerOperationSnapshot = ComposerOperationStatus & Readonly<{ result: MessageWithStateResponse | null; error: ComposerOperationError | null }>;
export type ComposerOperationProgress = Omit<ComposerProgressSnapshot, "session_id" | "inflight_requests" | "operation_id" | "session_operation_id" | "session_operation_epoch" | "request_token"> & { segment_sequence: number; segment_generation: number };
export type ComposerOperationFrame = {
  session_id: string; operation_id: string; sequence: number; id: string | null;
} & (
  | { event: "heartbeat" }
  | { event: "reconnect" }
  | { event: "status"; payload: ComposerOperationStatus }
  | { event: "terminal"; payload: { status: "completed" | "failed"; phase: "settled"; status_revision: number } }
  | { event: "progress"; payload: ComposerOperationProgress }
);
export type ComposerActiveOperation = Readonly<{ operation_id: string; kind: OperationKind; status: "queued" | "running" }>;
