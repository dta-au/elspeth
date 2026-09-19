import type { StorageQuotaRefusal } from "@/types/index";

/** Human-readable bytes for quota status and admission refusals. */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

export function effectiveStorageLimit(identityCap: number | null, containerCeiling: number | null): number | null {
  if (identityCap === null) return containerCeiling;
  if (containerCeiling === null) return identityCap;
  return Math.min(identityCap, containerCeiling);
}

export function storageQuotaMessage(refusal: StorageQuotaRefusal): string {
  const limit = effectiveStorageLimit(refusal.cap, refusal.ceiling);
  const amount = `${formatBytes(refusal.usage)} used${limit === null ? "" : ` of ${formatBytes(limit)}`}`;
  return `Storage quota reached: ${amount}. Delete files you no longer need, then try again.`;
}
