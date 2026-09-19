import { useEffect, useState } from "react";
import * as workflow from "@/api/workflow";
import { Button, Input } from "@/components/ui";
import { workflowErrorMessage } from "@/stores/mailboxStore";
import type { IdentityQuota, QuotaDimension } from "@/types/workflow";
import { formatBytes } from "@/utils/bytes";

const MAX_QUOTA_VALUE = 2_147_483_647;

function parseCap(value: string): number | null {
  if (!/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 1 && parsed <= MAX_QUOTA_VALUE ? parsed : null;
}

/** Edit one quota dimension; the server preserves the other inside its audit transaction. */
export function QuotaEditor({ identityId }: { identityId: string }): JSX.Element {
  const [quota, setQuota] = useState<IdentityQuota | null>(null);
  const [dimension, setDimension] = useState<QuotaDimension>("tokens");
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);

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
  }, [identityId]);

  const cap = parseCap(value);

  async function save(): Promise<void> {
    if (cap === null || busy || quota === null) return;
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const result = await workflow.setIdentityQuota(identityId, dimension, cap);
      setQuota(result);
      setValue("");
      setSaved(true);
    } catch (failure) {
      setError(workflowErrorMessage(failure));
    } finally {
      setBusy(false);
    }
  }

  return <section className="identity-admin-form" aria-label="Quota">
    <h3>Quota for {identityId}</h3>
    {quota === null && error === null && <p>Loading quota…</p>}
    {quota !== null && <>
      <p>Tokens per day: {quota.tokens_per_day ?? "no cap"} ({quota.tokens_used_today ?? "unknown"} used today; {quota.container_tokens_per_day === null ? "no container ceiling" : `container ceiling ${quota.container_tokens_per_day}`})</p>
      <p>Storage: {quota.storage_bytes === null ? "no cap" : formatBytes(quota.storage_bytes)} ({formatBytes(quota.storage_bytes_used)} used; {quota.container_storage_bytes === null ? "no container ceiling" : `container ceiling ${formatBytes(quota.container_storage_bytes)}`})</p>
    </>}
    {error !== null && <p role="alert" className="composer-preferences-error">{error}</p>}
    {saved && <p role="status">Quota updated.</p>}
    <form className="identity-admin-fields" onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <label className="identity-admin-field">Dimension
        <select className="input" value={dimension} disabled={busy || quota === null} onChange={(event) => setDimension(event.target.value as QuotaDimension)}>
          <option value="tokens">Tokens per day</option>
          <option value="storage">Storage bytes</option>
        </select>
      </label>
      <Input label="New cap" type="text" inputMode="numeric" value={value} disabled={busy || quota === null} onChange={(event) => setValue(event.target.value)} />
      <Button type="submit" variant="primary" disabled={busy || quota === null || cap === null}>Save cap</Button>
    </form>
  </section>;
}
