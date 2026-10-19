import type { StorageQuotaRefusal } from "@/types/index";
import { formatQuotaBytes } from "./quota";

/** Human-readable bytes for quota status and admission refusals. */
export function formatBytes(bytes: number | string): string {
  if (typeof bytes === "string") return formatQuotaBytes(bytes);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

export function storageQuotaMessage(refusal: StorageQuotaRefusal): string {
  const amount = `${formatBytes(refusal.usage)} used of ${formatBytes(refusal.limit)}`;
  if (refusal.scope === "container") {
    return `Shared storage quota reached: ${amount} across this deployment. Free shared storage or ask an administrator to increase the shared limit, then try again.`;
  }
  return `Personal storage quota reached: ${amount}. Delete files you no longer need or ask an administrator to increase your limit, then try again.`;
}
