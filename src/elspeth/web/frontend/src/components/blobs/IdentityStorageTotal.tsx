import { useEffect, useState } from "react";
import * as workflow from "@/api/workflow";
import type { IdentityQuota } from "@/types/workflow";
import { effectiveStorageLimit, formatBytes } from "@/utils/bytes";

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
  const limit = effectiveStorageLimit(quota.storage_bytes, quota.container_storage_bytes);
  return <span className="blob-manager-identity-total">
    {limit === null
      ? `Your files use ${formatBytes(quota.storage_bytes_used)}`
      : `Your files use ${formatBytes(quota.storage_bytes_used)} of ${formatBytes(limit)}`}
  </span>;
}
