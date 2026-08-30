// ============================================================================
// modelDisplayName — reader-register label for a composer/LLM model id
// (elspeth-d74ab492dd). Model ids are provider paths
// ("openrouter/anthropic/claude-sonnet-4.6"); the leaf segment is the model,
// the hyphens are word breaks. Casing goes through the ONE title-caser
// (catalog/pluginDisplayName.ts) so "gpt" reads "GPT" here as everywhere.
// Presentation only — the raw id stays in `title` and on the wire.
// ============================================================================

import { titleCaseLabel } from "@/components/catalog/pluginDisplayName";

export function modelDisplayName(modelId: string): string {
  const leaf = modelId.slice(modelId.lastIndexOf("/") + 1);
  // The `-` -> ` ` pre-split is DELIBERATE and local. titleCaseLabel splits on
  // /[_\s]+/ (pluginDisplayName.ts), so hyphens are not word breaks there and
  // titleCaseLabel("claude-sonnet-4.6") yields "Claude-sonnet-4.6". Widening
  // that regex to /[_\s-]+/ would change every hyphenated author-chosen node id
  // across the frontend, which is a blast radius this wave has not measured.
  // Casing itself still goes through the ONE title-caser, so this is a second
  // word-SPLITTING rule, not a second title-caser.
  return titleCaseLabel(leaf.replace(/-/g, " "));
}
