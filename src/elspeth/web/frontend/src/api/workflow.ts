import { authFetch } from "./authSession";
/** Authenticated workflow requests. Every response uses the shared error parser. */
import { authHeaders, parseResponse } from "./client";
import type {
  ApprovalView,
  ApproverDirectory,
  MailboxInbox,
  MailboxSent,
  MailboxSummary,
  ReviewAttestationView,
  ReviewRequestView,
  ReviewVerdict,
  WorkflowInspect,
  IdentityQuota,
  QuotaDimension,
} from "@/types/workflow";

async function get<T>(url: string): Promise<T> {
  const response = await authFetch(url, { headers: authHeaders(), cache: "no-store" });
  return parseResponse<T>(response);
}

async function post<T>(url: string, body?: object): Promise<T> {
  const response = await authFetch(url, {
    method: "POST",
    headers: authHeaders(body === undefined ? undefined : "application/json"),
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  return parseResponse<T>(response);
}

const segment = encodeURIComponent;
const noteOrNull = (note: string | null): string | null => note?.trim() ? note : null;

export const fetchMailboxSummary = (): Promise<MailboxSummary> => get("/api/workflow/mailbox/summary");
export const fetchMailboxInbox = (): Promise<MailboxInbox> => get("/api/workflow/mailbox/inbox");
export const fetchMailboxSent = (): Promise<MailboxSent> => get("/api/workflow/mailbox/sent");
export const fetchApproverDirectory = (): Promise<ApproverDirectory> => get("/api/workflow/mailbox/approvers");
export const markApprovalSeen = (id: string): Promise<ApprovalView> => post(`/api/workflow/mailbox/${segment(id)}/seen`);

export function requestApproval(sessionId: string, body: { state_id: string; approver_identity_id: string; note: string | null }): Promise<ApprovalView> {
  return post(`/api/sessions/${segment(sessionId)}/approvals`, { ...body, note: noteOrNull(body.note) });
}

export function decideApproval(id: string, decision: "approved" | "rejected", note: string | null): Promise<ApprovalView> {
  return post(`/api/approvals/${segment(id)}/decide`, { decision, note: noteOrNull(note) });
}

export function requestReview(sessionId: string, body: { state_id: string; reviewer_identity_id?: string | null; note: string | null }): Promise<ReviewRequestView> {
  return post(`/api/sessions/${segment(sessionId)}/reviews`, {
    state_id: body.state_id,
    reviewer_identity_id: body.reviewer_identity_id ?? null,
    note: noteOrNull(body.note),
  });
}

export function attestReview(id: string, verdict: ReviewVerdict, note: string | null): Promise<ReviewAttestationView> {
  return post(`/api/reviews/${segment(id)}/attest`, { verdict, note: noteOrNull(note) });
}

export function fetchWorkflowInspect(sessionId: string, stateId: string): Promise<WorkflowInspect> {
  return get(`/api/workflow/inspect/${segment(sessionId)}/${segment(stateId)}`);
}

export const fetchMyQuota = (): Promise<IdentityQuota> => get("/api/workflow/quota/me");

export function fetchIdentityQuota(identityId: string): Promise<IdentityQuota> {
  return get(`/api/workflow/quota/identities/${segment(identityId)}`);
}

export function setIdentityQuota(identityId: string, dimension: QuotaDimension, value: number): Promise<IdentityQuota> {
  return post(`/api/workflow/quota/identities/${segment(identityId)}`, { dimension, value });
}
