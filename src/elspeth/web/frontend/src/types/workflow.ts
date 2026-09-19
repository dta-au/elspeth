import type { IdentityRole } from "./identityAdmin";

export type ApprovalDecision = "approved" | "rejected" | "revoked" | "superseded";

export interface ApprovalView {
  approval_id: string;
  session_id: string;
  state_id: string;
  binding: Record<string, string>;
  requested_by_identity_id: string;
  approver_identity_id: string;
  requested_at: string;
  decided_at: string | null;
  decision: ApprovalDecision | null;
  request_note: string | null;
  decision_seen_at: string | null;
  decided_by_identity_id: string | null;
  decision_note: string | null;
  revoked_by_identity_id: string | null;
  revocation_actor_kind: string | null;
  revocation_event_id: string | null;
}

export type ReviewVerdict = "signed_off" | "changes_requested" | "withdrawn";

export interface ReviewRequestView {
  request_id: string;
  session_id: string;
  state_id: string;
  requested_by_identity_id: string;
  reviewer_identity_id: string | null;
  requested_at: string;
  cancelled_at: string | null;
  request_note: string | null;
  open: boolean;
}

export interface ReviewAttestationView {
  attestation_id: string;
  session_id: string;
  state_id: string;
  payload_digest: string;
  reviewer_identity_id: string;
  author_identity_id: string;
  attested_at: string;
  verdict: ReviewVerdict;
  note: string | null;
}

export interface MailboxSummary {
  governance: "on" | "off";
  roles: IdentityRole[];
  approvals_to_decide: number;
  reviews_to_attest: number;
  decisions_unseen: number;
}

export interface MailboxInbox {
  approvals: ApprovalView[];
  reviews: ReviewRequestView[];
}

export interface ReviewSentView {
  request: ReviewRequestView;
  attestations: ReviewAttestationView[];
}

export interface MailboxSent {
  approvals: ApprovalView[];
  reviews: ReviewSentView[];
}

export interface ApproverDirectory {
  approvers: { identity_id: string; username: string }[];
  suggested_identity_ids: string[];
}

export interface WorkflowInspect {
  session_id: string;
  state_id: string;
  access_log_id: string;
  composition_snapshot: unknown;
  yaml: string;
  attestations: ReviewAttestationView[];
}

export type QuotaDimension = "tokens" | "storage";

export interface IdentityQuota {
  identity_id: string;
  tokens_per_day: number | null;
  storage_bytes: number | null;
  container_tokens_per_day: number | null;
  container_storage_bytes: number | null;
  tokens_used_today: number | null;
  storage_bytes_used: number;
}
