/** Display storage effects in the same language as the proposal summary. */
export function proposalEffectLabel(domain: string): string {
  return domain === "blob_store" ? "Session files" : domain;
}
