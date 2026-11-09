import { type JSX, useEffect, useState } from "react";
import * as workflow from "@/api/workflow";
import type { IdentityQuota } from "@/types/workflow";
import { formatQuotaBytes } from "@/utils/quota";

/** Identity-wide storage usage beside the per-session file list. */
export function IdentityStorageTotal({ refreshKey }: { refreshKey: string }): JSX.Element | null {
  const [quota, setQuota] = useState<IdentityQuota | null>(null);

  useEffect(() => {
    let live = true;
    void workflow.fetchMyQuota().then(
      (result) => { if (live) setQuota(result); },
      () => { if (live) setQuota(null); },
    );
    return () => { live = false; };
  }, [refreshKey]);

  if (quota === null) return null;
  const limit = quota.storage_bytes;
  return <span className="blob-manager-identity-total" title="Includes uploads, retained run artifacts, and active output reservations. Archiving does not reclaim storage.">
    {quota.policy_state === "suspended" && "Storage growth suspended. "}
    {limit === null
      ? `Your storage uses ${formatQuotaBytes(quota.storage_bytes_used)}`
      : `Your storage uses ${formatQuotaBytes(quota.storage_bytes_used)} of ${formatQuotaBytes(limit)}`}
    {quota.container_storage_bytes !== null && `; shared storage ${formatQuotaBytes(quota.container_storage_bytes_used)} of ${formatQuotaBytes(quota.container_storage_bytes)}`}
  </span>;
}
