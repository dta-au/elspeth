/** Quota quantities cross JSON as decimal strings to preserve signed-64-bit caps. */
export function parseQuotaCap(value: string): string | null {
  if (!/^[0-9]{1,19}$/.test(value)) return null;
  const quantity = BigInt(value);
  return quantity >= 1n && quantity <= 9223372036854775807n ? quantity.toString() : null;
}

/** Keep the original exact quantity visible beside a convenient byte unit. */
export function formatQuotaBytes(value: string): string {
  const quantity = BigInt(value);
  if (quantity < 1024n) return `${value} B`;
  const units = [[1073741824n, "GB"], [1048576n, "MB"], [1024n, "KB"]] as const;
  for (const [size, label] of units) {
    if (quantity >= size) {
      const tenths = (quantity * 10n + size / 2n) / size;
      return `${tenths / 10n}.${tenths % 10n} ${label}`;
    }
  }
  return `${value} B`;
}
import type { IdentityQuota } from "@/types/workflow";

function quantity(value: unknown, nullable: true): string | null;
function quantity(value: unknown, nullable: false): string;
function quantity(value: unknown, nullable: boolean): string | null {
  if (value === null && nullable) return null;
  if (typeof value === "string" && /^(0|[1-9][0-9]*)$/.test(value)) return value;
  // Interoperate with old servers only where their JSON number is exact.
  if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) return String(value);
  throw new Error("Invalid quota quantity received from server");
}

export function parseIdentityQuota(value: unknown): IdentityQuota {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error("Invalid quota response");
  const row = value as Record<string, unknown>;
  if (typeof row.identity_id !== "string" || typeof row.can_edit !== "boolean"
    || (row.policy_state !== "active" && row.policy_state !== "never_issued" && row.policy_state !== "suspended")) throw new Error("Invalid quota response");
  return {
    identity_id: row.identity_id,
    policy_state: row.policy_state,
    can_edit: row.can_edit,
    tokens_per_day: quantity(row.tokens_per_day, true),
    storage_bytes: quantity(row.storage_bytes, true),
    container_tokens_per_day: quantity(row.container_tokens_per_day, true),
    container_storage_bytes: quantity(row.container_storage_bytes, true),
    tokens_used_today: quantity(row.tokens_used_today, true),
    storage_bytes_used: quantity(row.storage_bytes_used, false),
    container_tokens_used_today: quantity(row.container_tokens_used_today, true),
    container_storage_bytes_used: quantity(row.container_storage_bytes_used, false),
  };
}
