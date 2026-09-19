import { create } from "zustand";
import * as workflow from "@/api/workflow";
import { useAuthStore } from "./authStore";
import type { ApprovalView, MailboxInbox, MailboxSent, MailboxSummary, ReviewVerdict } from "@/types/workflow";

export const MAILBOX_POLL_INTERVAL_MS = 30_000;

let pollTimer: ReturnType<typeof setInterval> | null = null;
let generation = 0;

function stopTimer(): void {
  if (pollTimer !== null) clearInterval(pollTimer);
  pollTimer = null;
}

export function badgeCount(summary: MailboxSummary | null): number {
  return summary?.governance === "on"
    ? summary.approvals_to_decide + summary.reviews_to_attest + summary.decisions_unseen
    : 0;
}

function instant(value: string): bigint {
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) throw new Error(`Invalid approval timestamp: ${value}`);
  // Date.parse retains milliseconds. Keep the remaining three microsecond
  // digits so two PostgreSQL requests inside one millisecond still order.
  const fraction = /\.([0-9]+)(?:Z|[+-][0-9]{2}:[0-9]{2})$/.exec(value)?.[1] ?? "";
  const microseconds = fraction.padEnd(6, "0").slice(3, 6);
  return BigInt(parsed) * 1000n + BigInt(microseconds || "0");
}

/** Choose the latest request for one exact state, including equal-second SQLite timestamps. */
export function approvalForState(sent: ApprovalView[] | null, sessionId: string, stateId: string): ApprovalView | null {
  if (sent === null) return null;
  let latest: ApprovalView | null = null;
  for (const row of sent) {
    if (row.session_id !== sessionId || row.state_id !== stateId) continue;
    if (latest === null || instant(row.requested_at) > instant(latest.requested_at)
      || (instant(row.requested_at) === instant(latest.requested_at) && row.approval_id > latest.approval_id)) latest = row;
  }
  return latest;
}

export function workflowErrorMessage(error: unknown): string {
  if (typeof error !== "object" || error === null) return "The request failed. Please try again.";
  const value = error as { error_type?: unknown; current_state?: unknown; detail?: unknown };
  if (value.error_type === "approval_already_decided") {
    const state = typeof value.current_state === "string" ? value.current_state : "decided";
    return `This request was already ${state}.`;
  }
  if (value.error_type === "approval_note_required") return "A rejection needs a note explaining why.";
  if (value.error_type === "changes_requested_needs_note") return "Requesting changes needs a note explaining what to change.";
  if (value.error_type === "workflow_governance_off") return "Workflow governance is not enabled on this deployment.";
  return typeof value.detail === "string" ? value.detail : "The request failed. Please try again.";
}

interface MailboxState {
  summary: MailboxSummary | null;
  inbox: MailboxInbox | null;
  sent: MailboxSent | null;
  error: string | null;
  refreshSummary: () => Promise<void>;
  loadInbox: () => Promise<void>;
  loadSent: () => Promise<void>;
  openSent: (approvalId: string) => Promise<void>;
  decide: (approvalId: string, decision: "approved" | "rejected", note: string | null) => Promise<boolean | "already_decided">;
  attest: (requestId: string, verdict: ReviewVerdict, note: string | null) => Promise<boolean>;
  startPolling: () => () => void;
  reset: () => void;
}

export const useMailboxStore = create<MailboxState>((set, get) => ({
  summary: null,
  inbox: null,
  sent: null,
  error: null,

  async refreshSummary() {
    const requestGeneration = generation;
    try {
      const summary = await workflow.fetchMailboxSummary();
      if (generation === requestGeneration) set({ summary });
    } catch {
      // A transient failed poll leaves the last badge intact.
    }
  },

  async loadInbox() {
    const requestGeneration = generation;
    try {
      const inbox = await workflow.fetchMailboxInbox();
      if (generation === requestGeneration) set({ inbox, error: null });
    } catch (error) {
      if (generation === requestGeneration) set({ error: workflowErrorMessage(error) });
    }
  },

  async loadSent() {
    const requestGeneration = generation;
    try {
      const sent = await workflow.fetchMailboxSent();
      if (generation === requestGeneration) set({ sent, error: null });
    } catch (error) {
      if (generation === requestGeneration) set({ error: workflowErrorMessage(error) });
    }
  },

  async openSent(approvalId) {
    const row = get().sent?.approvals.find((candidate) => candidate.approval_id === approvalId);
    if (row === undefined || row.decision === null || row.decision_seen_at !== null) return;
    const requestGeneration = generation;
    try {
      const updated = await workflow.markApprovalSeen(approvalId);
      if (generation !== requestGeneration) return;
      set((state) => ({ sent: state.sent === null ? null : {
        ...state.sent,
        approvals: state.sent.approvals.map((candidate) => candidate.approval_id === approvalId ? updated : candidate),
      } }));
      await get().refreshSummary();
    } catch (error) {
      if (generation === requestGeneration) set({ error: workflowErrorMessage(error) });
    }
  },

  async decide(approvalId, decision, note) {
    const requestGeneration = generation;
    try {
      await workflow.decideApproval(approvalId, decision, note);
      if (generation !== requestGeneration) return false;
      set({ error: null });
      await Promise.all([get().loadInbox(), get().refreshSummary()]);
      return true;
    } catch (error) {
      if (generation !== requestGeneration) return false;
      const message = workflowErrorMessage(error);
      if (typeof error === "object" && error !== null && "error_type" in error && error.error_type === "approval_already_decided") {
        // The 409 is authoritative: the request is no longer open. Remove it
        // before reloading, so a failed refresh cannot leave a stale action.
        set((state) => ({
          error: message,
          inbox: state.inbox === null ? null : {
            ...state.inbox,
            approvals: state.inbox.approvals.filter((row) => row.approval_id !== approvalId),
          },
        }));
        void workflow.fetchMailboxInbox().then((inbox) => {
          if (generation === requestGeneration) set({ inbox });
        }).catch(() => {
          // Keep the decided row removed if the authoritative read fails.
        });
        void workflow.fetchMailboxSummary().then((summary) => {
          if (generation === requestGeneration) set({ summary });
        }).catch(() => {
          // The next badge poll can retry without restoring the stale row.
        });
        return "already_decided";
      }
      set({ error: message });
      return false;
    }
  },

  async attest(requestId, verdict, note) {
    const requestGeneration = generation;
    try {
      await workflow.attestReview(requestId, verdict, note);
      if (generation !== requestGeneration) return false;
      set({ error: null });
      await Promise.all([get().loadInbox(), get().refreshSummary()]);
      return true;
    } catch (error) {
      if (generation === requestGeneration) set({ error: workflowErrorMessage(error) });
      return false;
    }
  },

  startPolling() {
    if (pollTimer === null) {
      void get().refreshSummary();
      pollTimer = setInterval(() => { void get().refreshSummary(); }, MAILBOX_POLL_INTERVAL_MS);
    }
    return stopTimer;
  },

  reset() {
    generation += 1;
    stopTimer();
    set({ summary: null, inbox: null, sent: null, error: null });
  },
}));

// Credential changes synchronously stop the badge timer and invalidate any
// in-flight mailbox response, including a response that arrives after logout.
useAuthStore.subscribe((state, previous) => {
  if (previous.token !== state.token) useMailboxStore.getState().reset();
});
