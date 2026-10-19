import { type JSX, useEffect, useState } from "react";
import * as workflow from "@/api/workflow";
import { Button, Input } from "@/components/ui";
import { workflowErrorMessage } from "@/stores/mailboxStore";
import type { IdentityQuota, QuotaDimension } from "@/types/workflow";
import { formatQuotaBytes, parseQuotaCap } from "@/utils/quota";

/** Edit one quota dimension; the server preserves the other inside its audit transaction. */
export function QuotaEditor({ identityId, personName, onSaved }: { identityId: string; personName: string; capsEnabled: boolean; onSaved?: (quota: IdentityQuota) => void }): JSX.Element {
  const [quota, setQuota] = useState<IdentityQuota | null>(null);
  const [dimension, setDimension] = useState<QuotaDimension>("tokens");
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true;
    setQuota(null);
    setError(null);
    setSaved(false);
    void workflow.fetchIdentityQuota(identityId).then(
      (result) => { if (live) setQuota(result); },
      (failure: unknown) => { if (live) setError(workflowErrorMessage(failure)); },
    );
    return () => { live = false; };
  }, [identityId, attempt]);

  const cap = parseQuotaCap(value);

  async function save(): Promise<void> {
    if (cap === null || busy || quota === null) return;
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const result = await workflow.setIdentityQuota(identityId, dimension, cap);
      onSaved?.(result);
      setQuota(result);
      setValue("");
      setSaved(true);
    } catch (failure) {
      setError(workflowErrorMessage(failure));
    } finally {
      setBusy(false);
    }
  }

  async function suspend(): Promise<void> {
    if (busy || quota?.policy_state !== "active") return;
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const result = await workflow.revokeIdentityQuota(identityId);
      setQuota(result);
      onSaved?.(result);
      setSaved(true);
    } catch (failure) {
      setError(workflowErrorMessage(failure));
    } finally {
      setBusy(false);
    }
  }

  return <section className="identity-admin-form" aria-label={`Usage and limits for ${personName}`}>
    <h4>Usage and limits for {personName}</h4>
    <p className="people-grant-purpose">A personal cap applies to {personName} alone. A container ceiling is shared by everyone in this deployment and applies even when a personal cap is higher or absent.</p>
    {quota === null && error === null && <p>Loading quota…</p>}
    {quota !== null && <>
      <p>Tokens per day: {quota.policy_state === "suspended" ? "suspended" : quota.tokens_per_day ?? "no cap"} ({quota.tokens_used_today ?? "unknown"} used today; {quota.container_tokens_per_day === null ? "no container ceiling" : `container ceiling ${quota.container_tokens_per_day}`})</p>
      <p title={`Personal storage cap: ${quota.storage_bytes ?? "none"} bytes; shared cap: ${quota.container_storage_bytes ?? "none"} bytes`}>Storage: {quota.policy_state === "suspended" ? "suspended" : quota.storage_bytes === null ? "no cap" : formatQuotaBytes(quota.storage_bytes)} ({formatQuotaBytes(quota.storage_bytes_used)} used; {quota.container_storage_bytes === null ? "no container ceiling" : `container ceiling ${formatQuotaBytes(quota.container_storage_bytes)}`})</p>
      <p>Shared usage: {quota.container_tokens_used_today ?? "unknown"} tokens today; {formatQuotaBytes(quota.container_storage_bytes_used)} storage.</p>
      {quota.policy_state === "suspended" && <p role="status">Quota policy suspended: provider calls and storage growth are blocked. Save a cap to restore this policy; the other dimension retains its previous value.</p>}
      {quota.can_edit && quota.policy_state === "never_issued" && <p className="people-grant-purpose">{personName} has no personal caps yet. Setting one also gives the other a starting value, taken from this deployment's default limits.</p>}
    </>}
    {error !== null && <p role="alert" className="composer-preferences-error">{error}</p>}
    {quota === null && error !== null && <div><Button compact onClick={() => setAttempt((value) => value + 1)}>Retry</Button></div>}
    {saved && <p role="status">Quota updated.</p>}
    {quota !== null && !quota.can_edit && <p className="people-grant-purpose">Personal caps are not enabled on this deployment. An operator enables first issuance by setting default limits for tokens per day and storage in the server configuration.</p>}
    {quota?.can_edit && <form className="identity-admin-fields" onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <label className="identity-admin-field">Dimension
        <select className="input" value={dimension} disabled={busy || quota === null} onChange={(event) => { setDimension(event.target.value as QuotaDimension); setValue(""); }}>
          <option value="tokens">Tokens per day</option>
          <option value="storage">Storage bytes</option>
        </select>
      </label>
      <Input label="New cap" type="text" inputMode="numeric" value={value} disabled={busy || quota === null} onChange={(event) => setValue(event.target.value)} />
      <Button type="submit" variant="primary" disabled={busy || quota === null || cap === null}>Save cap</Button>
    </form>}
    {quota?.policy_state === "active" && <Button disabled={busy} onClick={() => { void suspend(); }}>Suspend quota policy</Button>}
  </section>;
}
