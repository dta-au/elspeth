import { useCallback, useEffect, useRef, useState } from "react";
import * as admin from "@/api/identityAdmin";
import * as workflow from "@/api/workflow";
import { Button, Input } from "@/components/ui";
import type { ActivationRole, HumanProvisionProvider, IdentityAccessState, IdentityView } from "@/types/identityAdmin";
import type { IdentityQuota } from "@/types/workflow";
import { formatBytes } from "@/utils/bytes";
import { QuotaEditor } from "./QuotaEditor";

const STATES: IdentityAccessState[] = ["pending", "active", "disabled"];
const PROVIDERS: HumanProvisionProvider[] = ["local", "oidc", "entra", "vanguard", "google"];
const ACTIVATION_ROLES: ActivationRole[] = ["user", "approver", "reviewer", "none"];

type RowQuota = { status: "loaded"; value: IdentityQuota; pageKey: string } | { status: "unavailable"; pageKey: string };

function QuotaCells({ result }: { result: RowQuota | undefined }): JSX.Element {
  if (result === undefined) return <>Loading quota…</>;
  if (result.status === "unavailable") return <>Quota and usage unavailable</>;
  const quota = result.value;
  return <>
    <span>Tokens today: {quota.tokens_used_today ?? "unknown"} of {quota.tokens_per_day ?? "no identity cap"}{quota.container_tokens_per_day === null ? "" : ` (container ceiling ${quota.container_tokens_per_day})`}</span>
    <span>Storage: {formatBytes(quota.storage_bytes_used)} of {quota.storage_bytes === null ? "no identity cap" : formatBytes(quota.storage_bytes)}{quota.container_storage_bytes === null ? "" : ` (container ceiling ${formatBytes(quota.container_storage_bytes)})`}</span>
  </>;
}

function QuotaEditorSlot({ identityId, onSaved }: { identityId: string; onSaved: (quota: IdentityQuota) => void }): JSX.Element {
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  return <QuotaEditor identityId={identityId} onSaved={(quota) => { if (mounted.current) onSaved(quota); }} />;
}

interface Props {
  currentIdentityId: string;
}

export function IdentitiesTable({ currentIdentityId }: Props): JSX.Element {
  const [state, setState] = useState<IdentityAccessState>("pending");
  const [offset, setOffset] = useState(0);
  const pageKey = `${state}:${offset}`;
  const [rows, setRows] = useState<IdentityView[] | null>(null);
  const [adminCount, setAdminCount] = useState<number | null>(null);
  const [quotaById, setQuotaById] = useState<Record<string, RowQuota>>({});
  const quotaRequestSequence = useRef<Record<string, number>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [target, setTarget] = useState<IdentityView | null>(null);
  const [quotaTargetId, setQuotaTargetId] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const actionStatusRef = useRef<HTMLParagraphElement>(null);
  const [note, setNote] = useState("");
  const [role, setRole] = useState<ActivationRole>("user");
  const [provider, setProvider] = useState<HumanProvisionProvider>("oidc");
  const [subject, setSubject] = useState("");
  const [organisation, setOrganisation] = useState("");
  const [provisionRole, setProvisionRole] = useState<ActivationRole>("user");
  const [provisionNote, setProvisionNote] = useState("");

  const reload = useCallback(async () => {
    const result = await admin.listIdentities(state, offset);
    setRows(result.identities);
    setAdminCount(result.active_human_admin_count);
    setTarget((current) => current !== null && result.identities.some((row) => row.identity_id === current.identity_id) ? current : null);
    setQuotaTargetId((current) => current !== null && result.identities.some((row) => row.identity_id === current && row.access_state === "active") ? current : null);
  }, [state, offset]);

  useEffect(() => {
    let active = true;
    void admin.listIdentities(state, offset).then(
      (result) => {
        if (!active) return;
        setRows(result.identities);
        setAdminCount(result.active_human_admin_count);
        setTarget((current) => current !== null && result.identities.some((row) => row.identity_id === current.identity_id) ? current : null);
        setQuotaTargetId((current) => current !== null && result.identities.some((row) => row.identity_id === current && row.access_state === "active") ? current : null);
        setError(null);
      },
      (err: unknown) => { if (active) setError(admin.adminErrorMessage(err, "Could not load identities")); },
    );
    return () => { active = false; };
  }, [state, offset]);

  useEffect(() => {
    let live = true;
    const requestPageKey = `${state}:${offset}`;
    setQuotaById({});
    if (state === "active" && rows !== null) {
      const activeRows = rows.filter((identity) => identity.access_state === "active");
      const workers = Math.min(6, activeRows.length);
      async function loadWorker(worker: number): Promise<void> {
        for (let index = worker; index < activeRows.length; index += workers) {
          if (!live) return;
          const identity = activeRows[index];
          const requestSequence = (quotaRequestSequence.current[identity.identity_id] ?? 0) + 1;
          quotaRequestSequence.current[identity.identity_id] = requestSequence;
          try {
            const quota = await workflow.fetchIdentityQuota(identity.identity_id);
            if (!live || quotaRequestSequence.current[identity.identity_id] !== requestSequence) continue;
            setQuotaById((current) => ({ ...current, [identity.identity_id]: quota.identity_id === identity.identity_id
              ? { status: "loaded", value: quota, pageKey: requestPageKey } : { status: "unavailable", pageKey: requestPageKey } }));
          } catch {
            if (live && quotaRequestSequence.current[identity.identity_id] === requestSequence) {
              setQuotaById((current) => ({ ...current, [identity.identity_id]: { status: "unavailable", pageKey: requestPageKey } }));
            }
          }
        }
      }
      for (let worker = 0; worker < workers; worker += 1) void loadWorker(worker);
    }
    return () => { live = false; };
  }, [state, offset, rows]);

  useEffect(() => {
    if (actionMessage !== null) actionStatusRef.current?.focus();
  }, [actionMessage]);

  async function run(action: () => Promise<unknown>): Promise<void> {
    setBusy(true);
    setError(null);
    setActionMessage(null);
    try {
      await action();
      setTarget(null);
      setNote("");
      await reload();
      setActionMessage("Identity change completed.");
    } catch (err) {
      setError(admin.adminErrorMessage(err, "Identity change failed"));
    } finally {
      setBusy(false);
    }
  }

  const action = target?.access_state === "pending" ? "Activate" : target?.access_state === "disabled" ? "Enable" : "Disable";

  return (
    <section aria-label="Identities" className="identity-admin-section">
      {adminCount === 1 && <p role="status" className="identity-admin-advisory">This container has one active human administrator. Add another before changing that administrator’s access.</p>}
      {actionMessage !== null && <p ref={actionStatusRef} role="status" tabIndex={-1}>{actionMessage}</p>}
      {error !== null && <p role="alert" className="composer-preferences-error">{error}</p>}
      <div className="identity-admin-toolbar">
        <label className="identity-admin-field">Access state
          <select className="input" value={state} disabled={busy} onChange={(event) => { setState(event.target.value as IdentityAccessState); setOffset(0); setRows(null); setTarget(null); setQuotaTargetId(null); }}>
            {STATES.map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
        </label>
        <span>Page {Math.floor(offset / admin.ADMIN_PAGE_SIZE) + 1}</span>
        <Button compact disabled={busy || offset === 0} onClick={() => { setTarget(null); setQuotaTargetId(null); setRows(null); setOffset(Math.max(0, offset - admin.ADMIN_PAGE_SIZE)); }}>Previous</Button>
        <Button compact disabled={busy || rows === null || rows.length < admin.ADMIN_PAGE_SIZE} onClick={() => { setTarget(null); setQuotaTargetId(null); setRows(null); setOffset(offset + admin.ADMIN_PAGE_SIZE); }}>Next</Button>
      </div>
      {rows === null ? <p>Loading identities…</p> : rows.length === 0 ? <p>No {state} identities on this page.</p> : (
        <div className="identity-admin-table-scroll"><table className="identity-admin-table">
          <thead><tr><th scope="col">Identity</th><th scope="col">Organisation</th><th scope="col">Provider</th><th scope="col">Quota and usage</th><th scope="col">Action</th></tr></thead>
          <tbody>{rows.map((identity) => (
            <tr key={identity.identity_id}>
              <td><code>{identity.access_state === "pending" ? identity.subject : identity.username ?? identity.subject}</code></td>
              <td>{identity.organisation_id ?? "—"}</td>
              <td>{identity.provider}</td>
              <td><div className="identity-admin-field">{identity.access_state === "active" ? <QuotaCells result={quotaById[identity.identity_id]?.pageKey === pageKey ? quotaById[identity.identity_id] : undefined} /> : "—"}</div></td>
              <td className="identity-admin-row-actions">
                <Button compact disabled={busy || (identity.access_state === "active" && identity.identity_id === currentIdentityId)} onClick={() => { setTarget(identity); setQuotaTargetId(null); setNote(""); setActionMessage(null); }}>{identity.access_state === "pending" ? "Activate" : identity.access_state === "disabled" ? "Enable" : "Disable"}</Button>
                {identity.access_state === "active" && <Button compact disabled={busy} onClick={() => { setTarget(null); setQuotaTargetId(identity.identity_id); setActionMessage(null); }}>Quota</Button>}
              </td>
            </tr>
          ))}</tbody>
        </table></div>
      )}
      {target !== null && <form className="identity-admin-form" onSubmit={(event) => {
        event.preventDefault();
        const identityId = target.identity_id;
        void run(() => target.access_state === "pending" ? admin.activateIdentity(identityId, role, note.trim()) : target.access_state === "disabled" ? admin.enableIdentity(identityId, note.trim()) : admin.disableIdentity(identityId, note.trim()));
      }}>
        <h3>{action} {target.access_state === "pending" ? target.subject : target.username ?? target.subject}</h3>
        {target.access_state === "pending" && <label className="identity-admin-field">Initial role
          <select className="input" value={role} onChange={(event) => setRole(event.target.value as ActivationRole)}>{ACTIVATION_ROLES.map((value) => <option key={value} value={value}>{value}</option>)}</select>
        </label>}
        <Input label={target.access_state === "active" ? "Reason" : "Note"} value={note} maxLength={512} required onChange={(event) => setNote(event.target.value)} />
        <div className="identity-admin-actions"><Button type="submit" variant={target.access_state === "active" ? "danger" : "primary"} disabled={busy || note.trim() === ""}>Confirm {action.toLowerCase()}</Button><Button disabled={busy} onClick={() => { setTarget(null); setActionMessage("Identity action cancelled."); }}>Cancel</Button></div>
      </form>}
      {quotaTargetId !== null && <QuotaEditorSlot key={quotaTargetId} identityId={quotaTargetId} onSaved={(quota: IdentityQuota) => {
        if (quota.identity_id === quotaTargetId) {
          quotaRequestSequence.current[quota.identity_id] = (quotaRequestSequence.current[quota.identity_id] ?? 0) + 1;
          setQuotaById((current) => ({ ...current, [quota.identity_id]: { status: "loaded", value: quota, pageKey } }));
        }
      }} />}
      <form className="identity-admin-form" onSubmit={(event) => {
        event.preventDefault();
        void run(async () => {
          await admin.preProvisionIdentity({ provider, subject: subject.trim(), ...(organisation.trim() ? { organisation_id: organisation.trim() } : {}), role: provisionRole, note: provisionNote.trim() });
          setSubject(""); setOrganisation(""); setProvisionNote("");
        });
      }}>
        <h3>Pre-provision identity</h3>
        <div className="identity-admin-fields">
          <label className="identity-admin-field">Provider<select className="input" value={provider} onChange={(event) => setProvider(event.target.value as HumanProvisionProvider)}>{PROVIDERS.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
          <Input label="Subject" value={subject} maxLength={512} required onChange={(event) => setSubject(event.target.value)} />
          <Input label="Organisation (optional)" value={organisation} maxLength={512} onChange={(event) => setOrganisation(event.target.value)} />
          <label className="identity-admin-field">Initial role<select className="input" value={provisionRole} onChange={(event) => setProvisionRole(event.target.value as ActivationRole)}>{ACTIVATION_ROLES.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
          <Input label="Provisioning note" value={provisionNote} maxLength={512} required onChange={(event) => setProvisionNote(event.target.value)} />
        </div>
        <Button type="submit" variant="primary" disabled={busy || subject.trim() === "" || provisionNote.trim() === ""}>Pre-provision</Button>
      </form>
    </section>
  );
}
