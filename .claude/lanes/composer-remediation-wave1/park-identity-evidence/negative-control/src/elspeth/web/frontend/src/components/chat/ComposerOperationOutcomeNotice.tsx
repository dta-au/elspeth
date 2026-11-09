import type { ComposerOperationFailureNotice } from "@/types/composerOperations";

const OUTCOME_LABELS = {
  request_cancelled: "Composition stopped",
  deadline_expired: "Composition timed out",
  worker_lost: "Server stopped while composing",
  operation_failed: "Composition failed",
  http_error: "Composition could not finish",
} as const;

/** A retained operation fact, distinct from an assistant reply or audit chat row. */
export function ComposerOperationOutcomeNotice({ outcome }: { outcome: ComposerOperationFailureNotice }) {
  const saved = outcome.saved_revisions;
  const reference = outcome.diagnostic_id ?? outcome.request_id ?? outcome.operation_id;
  return (
    <aside className="trusted-system-notice" role="note" aria-label="Composer operation outcome" data-operation-id={outcome.operation_id}>
      <strong>{OUTCOME_LABELS[outcome.failure_code]}</strong>
      {outcome.settled_at !== null && <p><time dateTime={outcome.settled_at}>{new Date(outcome.settled_at).toLocaleString()}</time></p>}
      {saved.attribution === "unavailable" ? (
        <p>Saved progress has not been confirmed. Review the current draft before deciding what to do next.</p>
      ) : saved.attribution === "unstarted" ? (
        <p>This request stopped before composing began. No draft revisions were saved by this operation.</p>
      ) : saved.count === 0 ? (
        <p>No draft revisions were saved by this operation. Earlier draft changes remain available.</p>
      ) : (
        <p>This operation saved {saved.count} draft revisions, last saved version {saved.latest_version}. Later changes may be newer.</p>
      )}
      <p>Support reference: <code>{reference}</code></p>
    </aside>
  );
}
