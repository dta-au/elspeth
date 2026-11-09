import { type JSX, useEffect, useState } from "react";
import { Button } from "@/components/ui";
import { fetchWorkflowInspect } from "@/api/workflow";
import { useMailboxStore, workflowErrorMessage } from "@/stores/mailboxStore";
import type { WorkflowInspect } from "@/types/workflow";
import type { InboxItem } from "./InboxList";

function requestIdentity(item: InboxItem): { id: string; sessionId: string; stateId: string; requester: string; note: string | null } {
  const row = item.kind === "approval" ? item.approval : item.review;
  return {
    id: item.kind === "approval" ? item.approval.approval_id : item.review.request_id,
    sessionId: row.session_id,
    stateId: row.state_id,
    requester: row.requested_by_identity_id,
    note: row.request_note,
  };
}

export function InspectPane({ item, onBack, onDone }: { item: InboxItem; onBack: () => void; onDone: () => void }): JSX.Element {
  const target = requestIdentity(item);
  const targetKey = `${item.kind}:${target.id}:${target.sessionId}:${target.stateId}`;
  const [loaded, setLoaded] = useState<{ key: string; view: WorkflowInspect } | null>(null);
  const [failure, setFailure] = useState<{ key: string; message: string } | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const storeError = useMailboxStore((state) => state.error);
  const decide = useMailboxStore((state) => state.decide);
  const attest = useMailboxStore((state) => state.attest);

  useEffect(() => {
    let active = true;
    setNote("");
    setFailure(null);
    void fetchWorkflowInspect(target.sessionId, target.stateId).then((view) => {
      if (!active) return;
      if (view.session_id !== target.sessionId || view.state_id !== target.stateId) {
        setFailure({ key: targetKey, message: "The loaded pipeline is not the version this request names." });
        return;
      }
      setLoaded({ key: targetKey, view });
    }).catch((error: unknown) => {
      if (!active) return;
      const status = typeof error === "object" && error !== null && "status" in error ? error.status : null;
      setFailure({ key: targetKey, message: status === 404
        ? "This request is no longer open for inspection."
        : workflowErrorMessage(error) });
    });
    return () => { active = false; };
  }, [targetKey, target.sessionId, target.stateId]);

  const current = loaded?.key === targetKey && failure?.key !== targetKey ? loaded.view : null;
  const currentFailure = failure?.key === targetKey ? failure.message : null;
  const canDecide = current !== null && !busy;

  async function submit(decision: "approved" | "rejected" | "signed_off" | "changes_requested") {
    if (!canDecide || (decision === "rejected" || decision === "changes_requested") && note.trim() === "") return;
    setBusy(true);
    const result = item.kind === "approval"
      ? await decide(target.id, decision as "approved" | "rejected", note)
      : await attest(target.id, decision as "signed_off" | "changes_requested", note);
    setBusy(false);
    if (result === true || result === "already_decided") onDone();
  }

  return <section className="workflow-inspect" aria-label="Frozen workflow inspection">
    <Button onClick={onBack}>Back to inbox</Button>
    <h3>{item.kind === "approval" ? "Approval" : "Review"} requested by {target.requester}</h3>
    <p>Request note: {target.note ?? "No note"}</p>
    <p className="workflow-inspect-reference">Session {target.sessionId} · State {target.stateId}</p>
    {currentFailure !== null && <p role="alert">{currentFailure}</p>}
    {current === null && currentFailure === null && <p>Loading the frozen pipeline</p>}
    {current !== null && <div className="workflow-inspect-content">
      <h4>Frozen pipeline YAML</h4>
      <pre data-testid="mailbox-inspect-yaml"><code>{current.yaml}</code></pre>
      <small>Inspected read {current.access_log_id}</small>
    </div>}
    {storeError !== null && <p role="alert">{storeError}</p>}
    <label className="workflow-field">Note
      <textarea value={note} onChange={(event) => setNote(event.target.value)} maxLength={4096} rows={3} />
    </label>
    <div className="workflow-actions">
      {item.kind === "approval" ? <>
        <Button variant="primary" disabled={!canDecide} onClick={() => void submit("approved")}>Approve</Button>
        <Button variant="danger" disabled={!canDecide || note.trim() === ""} onClick={() => void submit("rejected")}>Reject</Button>
      </> : <>
        <Button variant="primary" disabled={!canDecide} onClick={() => void submit("signed_off")}>Sign off</Button>
        <Button variant="danger" disabled={!canDecide || note.trim() === ""} onClick={() => void submit("changes_requested")}>Request changes</Button>
      </>}
    </div>
  </section>;
}
