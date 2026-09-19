import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import * as workflow from "@/api/workflow";
import { useMailboxStore, workflowErrorMessage } from "@/stores/mailboxStore";
import type { ApproverDirectory } from "@/types/workflow";
import "./workflow.css";

export function WorkflowRequestDialog({ kind, sessionId, stateId, onClose }: {
  kind: "approval" | "review";
  sessionId: string;
  stateId: string;
  onClose: () => void;
}): JSX.Element {
  const modalRef = useRef<HTMLDivElement>(null);
  const [directory, setDirectory] = useState<ApproverDirectory | null>(null);
  const [approverId, setApproverId] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const loadSent = useMailboxStore((state) => state.loadSent);
  const refreshSummary = useMailboxStore((state) => state.refreshSummary);
  useFocusTrap(modalRef, true);

  useEffect(() => {
    let active = true;
    if (kind === "approval") {
      void workflow.fetchApproverDirectory().then((value) => {
        if (!active) return;
        setDirectory(value);
        setApproverId(value.suggested_identity_ids.find((id) => value.approvers.some((entry) => entry.identity_id === id)) ?? value.approvers[0]?.identity_id ?? "");
      }).catch((failure: unknown) => { if (active) setError(workflowErrorMessage(failure)); });
    }
    function onKeyDown(event: KeyboardEvent) { if (event.key === "Escape") onClose(); }
    document.addEventListener("keydown", onKeyDown);
    return () => { active = false; document.removeEventListener("keydown", onKeyDown); };
  }, [kind, onClose]);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || (kind === "approval" && approverId === "")) return;
    setBusy(true);
    setError(null);
    try {
      if (kind === "approval") await workflow.requestApproval(sessionId, { state_id: stateId, approver_identity_id: approverId, note });
      else await workflow.requestReview(sessionId, { state_id: stateId, note });
      await Promise.all([loadSent(), refreshSummary()]);
      onClose();
    } catch (failure) {
      setError(workflowErrorMessage(failure));
    } finally {
      setBusy(false);
    }
  }

  return <>
    <div role="presentation" className="app-dialog-backdrop" onClick={onClose} />
    <div ref={modalRef} role="dialog" aria-modal="true" aria-labelledby="workflow-request-title" className="app-dialog settings-dialog workflow-request-dialog">
      <div className="secrets-panel-header">
        <h2 id="workflow-request-title" className="secrets-panel-title">Request {kind}</h2>
        <Button variant="bare" className="dialog-close" aria-label="Close request" onClick={onClose}>×</Button>
      </div>
      <form className="secrets-panel-body" onSubmit={(event) => void submit(event)}>
        <p>Request for the current frozen state {stateId}.</p>
        {kind === "approval" && <label className="workflow-field">Approver
          <select value={approverId} onChange={(event) => setApproverId(event.target.value)} disabled={busy || directory === null}>
            {directory?.approvers.map((entry) => <option key={entry.identity_id} value={entry.identity_id}>{entry.username} ({entry.identity_id})</option>)}
          </select>
        </label>}
        {kind === "approval" && directory?.approvers.length === 0 && <p role="status">No active approver is available.</p>}
        <label className="workflow-field">Note
          <textarea value={note} onChange={(event) => setNote(event.target.value)} maxLength={4096} rows={3} disabled={busy} />
        </label>
        {error !== null && <p role="alert">{error}</p>}
        <div className="workflow-actions">
          <Button type="submit" variant="primary" disabled={busy || kind === "approval" && approverId === ""}>Send request</Button>
          <Button onClick={onClose}>Cancel</Button>
        </div>
      </form>
    </div>
  </>;
}
