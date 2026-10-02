import type { JSX } from "react";
import type { WorkflowAuditView } from "@/types/workflow";

/** Four record kinds, newest first within each. The server has already scoped
 *  every row to the identities this approver may see and applied its own row
 *  cap, so nothing here filters — sorting and formatting only. A second filter
 *  in the client would be a second scope authority, which is exactly what the
 *  endpoint exists to be. */

function whenLabel(value: string): string {
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? value : new Date(parsed).toISOString().replace("T", " ").replace(/\.\d+Z$/, "Z");
}

const byNewest = <T,>(rows: readonly T[], at: (row: T) => string): T[] =>
  [...rows].sort((a, b) => Date.parse(at(b)) - Date.parse(at(a)));

function Section({ title, count, children }: { title: string; count: number; children: JSX.Element }): JSX.Element {
  return <section className="workflow-audit-section">
    <h3>{title} <small>({count})</small></h3>
    {count === 0 ? <p>None in scope.</p> : children}
  </section>;
}

export function AuditViewList({ view }: { view: WorkflowAuditView }): JSX.Element {
  const nothing = view.runs.length === 0 && view.approvals.length === 0
    && view.attestations.length === 0 && view.auth_events.length === 0;

  return <div className="workflow-audit">
    <p className="workflow-audit-scope">
      {view.identity_ids.length === 0
        ? "No identities are in your audit scope."
        : `Scope: ${view.identity_ids.length} ${view.identity_ids.length === 1 ? "identity" : "identities"}.`}
      {view.truncated && " Older rows are not shown — this view is capped."}
    </p>

    {nothing && view.identity_ids.length > 0 && <p role="status">No recorded activity for the identities in your scope.</p>}

    <Section title="Runs" count={view.runs.length}>
      <ul className="workflow-mailbox-list">
        {byNewest(view.runs, (run) => run.recorded_at).map((run) => (
          <li key={run.run_id}>
            <strong>{run.status}</strong>
            <span>Started by {run.initiated_by_identity_id}</span>
            <small>{run.run_id} · {whenLabel(run.started_at)}{run.completed_at === null ? " · not finished" : ` → ${whenLabel(run.completed_at)}`}</small>
          </li>
        ))}
      </ul>
    </Section>

    <Section title="Approvals" count={view.approvals.length}>
      <ul className="workflow-mailbox-list">
        {byNewest(view.approvals, (row) => row.requested_at).map((row) => (
          <li key={row.approval_id}>
            <strong>{row.decision ?? "Awaiting a decision"}</strong>
            <span>Requested by {row.requested_by_identity_id}, addressed to {row.approver_identity_id}</span>
            <small>{row.session_id} · {row.state_id} · requested {whenLabel(row.requested_at)}{row.decided_at === null ? "" : ` · decided ${whenLabel(row.decided_at)}`}</small>
          </li>
        ))}
      </ul>
    </Section>

    <Section title="Review attestations" count={view.attestations.length}>
      <ul className="workflow-mailbox-list">
        {byNewest(view.attestations, (row) => row.attested_at).map((row) => (
          <li key={row.attestation_id}>
            <strong>{row.verdict}</strong>
            <span>{row.reviewer_identity_id} on work by {row.author_identity_id}</span>
            <small>{row.session_id} · {row.state_id} · {whenLabel(row.attested_at)}</small>
          </li>
        ))}
      </ul>
    </Section>

    <Section title="Authentication events" count={view.auth_events.length}>
      <ul className="workflow-mailbox-list">
        {byNewest(view.auth_events, (row) => row.occurred_at).map((row) => (
          <li key={row.event_id}>
            <strong>{row.event_type} — {row.outcome}</strong>
            <span>{row.identity_id}</span>
            <small>{whenLabel(row.occurred_at)}</small>
          </li>
        ))}
      </ul>
    </Section>
  </div>;
}
