import { useCallback, useEffect, useState } from "react";
import * as admin from "@/api/identityAdmin";
import { Button, Input } from "@/components/ui";
import type { IdentityRole, RoleView } from "@/types/identityAdmin";

const ROLES: IdentityRole[] = ["admin", "approver", "reviewer", "user", "curator", "auditor", "oversight"];

export function RolesEditor(): JSX.Element {
  const [filterDraft, setFilterDraft] = useState("");
  const [filter, setFilter] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const [rows, setRows] = useState<RoleView[] | null>(null);
  const [identityId, setIdentityId] = useState("");
  const [role, setRole] = useState<IdentityRole>("user");
  const [expiry, setExpiry] = useState("");
  const [note, setNote] = useState("");
  const [confirmRevoke, setConfirmRevoke] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    const result = await admin.listRoles(filter, offset);
    setRows(result.roles);
  }, [filter, offset]);

  useEffect(() => {
    let active = true;
    void admin.listRoles(filter, offset).then(
      (result) => { if (active) { setRows(result.roles); setError(null); } },
      (err: unknown) => { if (active) setError(admin.adminErrorMessage(err, "Could not load roles")); },
    );
    return () => { active = false; };
  }, [filter, offset]);

  async function run(action: () => Promise<unknown>): Promise<void> {
    setBusy(true);
    setError(null);
    try { await action(); setConfirmRevoke(null); await reload(); }
    catch (err) { setError(admin.adminErrorMessage(err, "Role change failed")); }
    finally { setBusy(false); }
  }

  return <section aria-label="Roles" className="identity-admin-section">
    {error !== null && <p role="alert" className="composer-preferences-error">{error}</p>}
    <form className="identity-admin-toolbar" onSubmit={(event) => { event.preventDefault(); setFilter(filterDraft.trim() || null); setOffset(0); setRows(null); }}>
      <Input label="Filter by identity ID" value={filterDraft} disabled={busy} onChange={(event) => setFilterDraft(event.target.value)} />
      <Button type="submit" compact disabled={busy}>Apply filter</Button>
      <span>Page {Math.floor(offset / admin.ADMIN_PAGE_SIZE) + 1}</span>
      <Button compact disabled={busy || offset === 0} onClick={() => setOffset(Math.max(0, offset - admin.ADMIN_PAGE_SIZE))}>Previous</Button>
      <Button compact disabled={busy || rows === null || rows.length < admin.ADMIN_PAGE_SIZE} onClick={() => setOffset(offset + admin.ADMIN_PAGE_SIZE)}>Next</Button>
    </form>
    {rows === null ? <p>Loading roles…</p> : rows.length === 0 ? <p>No active roles on this page.</p> : <div className="identity-admin-table-scroll"><table className="identity-admin-table"><thead><tr><th scope="col">Identity ID</th><th scope="col">Role</th><th scope="col">Scope</th><th scope="col">Expiry</th><th scope="col">Action</th></tr></thead><tbody>{rows.map((row) => <tr key={row.role_id}><td><code>{row.identity_id}</code></td><td>{row.role}</td><td>{row.scope ?? "Deployment"}</td><td>{row.expires_at ?? "—"}</td><td>{confirmRevoke === row.role_id ? <Button compact variant="danger" disabled={busy} onClick={() => void run(() => admin.revokeRole(row.role_id))}>Confirm revoke</Button> : <Button compact disabled={busy} onClick={() => setConfirmRevoke(row.role_id)}>Revoke</Button>}</td></tr>)}</tbody></table></div>}
    <form className="identity-admin-form" onSubmit={(event) => {
      event.preventDefault();
      void run(async () => {
        await admin.grantRole({ identity_id: identityId.trim(), role, ...(expiry ? { expires_at: new Date(expiry).toISOString() } : {}), ...(note.trim() ? { note: note.trim() } : {}) });
        setIdentityId(""); setExpiry(""); setNote("");
      });
    }}>
      <h3>Grant deployment-wide role</h3><div className="identity-admin-fields">
        <Input label="Identity ID" value={identityId} maxLength={64} required onChange={(event) => setIdentityId(event.target.value)} />
        <label className="identity-admin-field">Role<select className="input" value={role} onChange={(event) => setRole(event.target.value as IdentityRole)}>{ROLES.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
        <Input label="Expires at (optional)" type="datetime-local" value={expiry} onChange={(event) => setExpiry(event.target.value)} />
        <Input label="Note (optional)" value={note} maxLength={512} onChange={(event) => setNote(event.target.value)} />
      </div><Button type="submit" variant="primary" disabled={busy || identityId.trim() === ""}>Grant role</Button>
    </form>
  </section>;
}
