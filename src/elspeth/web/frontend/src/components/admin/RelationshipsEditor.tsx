import { useCallback, useEffect, useState } from "react";
import * as admin from "@/api/identityAdmin";
import { Button, Input } from "@/components/ui";
import type { RelationshipView } from "@/types/identityAdmin";

export function RelationshipsEditor(): JSX.Element {
  const [filterDraft, setFilterDraft] = useState("");
  const [filter, setFilter] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const [rows, setRows] = useState<RelationshipView[] | null>(null);
  const [fromId, setFromId] = useState("");
  const [toId, setToId] = useState("");
  const [note, setNote] = useState("");
  const [confirmRevoke, setConfirmRevoke] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    const result = await admin.listRelationships(filter, offset);
    setRows(result.relationships);
  }, [filter, offset]);

  useEffect(() => {
    let active = true;
    void admin.listRelationships(filter, offset).then(
      (result) => { if (active) { setRows(result.relationships); setError(null); } },
      (err: unknown) => { if (active) setError(admin.adminErrorMessage(err, "Could not load relationships")); },
    );
    return () => { active = false; };
  }, [filter, offset]);

  async function run(action: () => Promise<unknown>): Promise<void> {
    setBusy(true);
    setError(null);
    try { await action(); setConfirmRevoke(null); await reload(); }
    catch (err) { setError(admin.adminErrorMessage(err, "Relationship change failed")); }
    finally { setBusy(false); }
  }

  return <section aria-label="Relationships" className="identity-admin-section">
    {error !== null && <p role="alert" className="composer-preferences-error">{error}</p>}
    <form className="identity-admin-toolbar" onSubmit={(event) => { event.preventDefault(); setFilter(filterDraft.trim() || null); setOffset(0); setRows(null); }}>
      <Input label="Filter by identity ID" value={filterDraft} disabled={busy} onChange={(event) => setFilterDraft(event.target.value)} />
      <Button type="submit" compact disabled={busy}>Apply filter</Button>
      <span>Page {Math.floor(offset / admin.ADMIN_PAGE_SIZE) + 1}</span>
      <Button compact disabled={busy || offset === 0} onClick={() => setOffset(Math.max(0, offset - admin.ADMIN_PAGE_SIZE))}>Previous</Button>
      <Button compact disabled={busy || rows === null || rows.length < admin.ADMIN_PAGE_SIZE} onClick={() => setOffset(offset + admin.ADMIN_PAGE_SIZE)}>Next</Button>
    </form>
    {rows === null ? <p>Loading relationships…</p> : rows.length === 0 ? <p>No active relationships on this page.</p> : <div className="identity-admin-table-scroll"><table className="identity-admin-table"><thead><tr><th scope="col">Approver</th><th scope="col">Member</th><th scope="col">Type</th><th scope="col">Action</th></tr></thead><tbody>{rows.map((row) => <tr key={row.relationship_id}><td><code>{row.from_identity_id}</code></td><td><code>{row.to_identity_id}</code></td><td>{row.relationship_type}</td><td>{confirmRevoke === row.relationship_id ? <Button compact variant="danger" disabled={busy} onClick={() => void run(() => admin.revokeRelationship(row.relationship_id))}>Confirm revoke</Button> : <Button compact disabled={busy} onClick={() => setConfirmRevoke(row.relationship_id)}>Revoke</Button>}</td></tr>)}</tbody></table></div>}
    <form className="identity-admin-form" onSubmit={(event) => {
      event.preventDefault();
      void run(async () => {
        await admin.assertRelationship({ from_identity_id: fromId.trim(), to_identity_id: toId.trim(), relationship_type: "approver", ...(note.trim() ? { note: note.trim() } : {}) });
        setFromId(""); setToId(""); setNote("");
      });
    }}>
      <h3>Assert approver relationship</h3><p>The approver oversees the member in this container’s organisation tree.</p>
      <div className="identity-admin-fields">
        <Input label="Approver identity ID" value={fromId} maxLength={64} required onChange={(event) => setFromId(event.target.value)} />
        <Input label="Member identity ID" value={toId} maxLength={64} required onChange={(event) => setToId(event.target.value)} />
        <Input label="Note (optional)" value={note} maxLength={512} onChange={(event) => setNote(event.target.value)} />
      </div><Button type="submit" variant="primary" disabled={busy || fromId.trim() === "" || toId.trim() === "" || fromId.trim() === toId.trim()}>Assert relationship</Button>
    </form>
  </section>;
}
