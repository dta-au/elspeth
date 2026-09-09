// ============================================================================
// modelDisplayName — reader-register label for a composer/LLM model id
// (elspeth-d74ab492dd). Model ids are provider paths
// ("openrouter/anthropic/claude-sonnet-4.6"); the leaf segment is the model,
// the hyphens are word breaks. Casing goes through the ONE title-caser
// (catalog/pluginDisplayName.ts) so "gpt" reads "GPT" here as everywhere.
// Presentation only — the raw id stays in `title` and on the wire.
// ============================================================================

import { titleCaseLabel } from "@/components/catalog/pluginDisplayName";

// Shapes that are identifiers rather than words. A leaf matching any of these
// is returned RAW: title-casing it produces neither clean prose nor a
// recoverable id. The motivating case is Bedrock —
// `bedrock/anthropic.claude-3-haiku-20240307-v1:0` yielded "Anthropic.claude 3
// Haiku 20240307 V1:0", with a date stamp and a version suffix title-cased as
// if they were words, on the run-confirm CONSENT surface. An honest identifier
// beats a fake name, which is the ruling diagnosticPhrases.ts already makes for
// an unknown enum ("never dressed up as a sentence").
//
// The dot test is deliberately LETTER-adjacent: `claude-sonnet-4.6` and
// `gpt-5.5` carry a version dot between digits and must keep phrasing, which
// is the common OpenRouter/Anthropic form. Six is the digit-run threshold
// because it clears every version number in use while catching the
// yyyymmdd stamps Bedrock and Azure put in their ids.
const NON_WORDISH_LEAF_RE = /[a-z]\.[a-z]|:|[0-9]{6,}/i;

export function modelDisplayName(modelId: string): string {
  const leaf = modelId.slice(modelId.lastIndexOf("/") + 1);
  // Only the PHRASING is guarded, never the path stripping: a provider path is
  // still reduced to its leaf, so a chip cannot start leaking "openrouter".
  if (NON_WORDISH_LEAF_RE.test(leaf)) return leaf;
  // The `-` -> ` ` pre-split is DELIBERATE and local. titleCaseLabel splits on
  // /[_\s]+/ (pluginDisplayName.ts), so hyphens are not word breaks there and
  // titleCaseLabel("claude-sonnet-4.6") yields "Claude-sonnet-4.6". Widening
  // that regex to /[_\s-]+/ would change every hyphenated author-chosen node id
  // across the frontend, which is a blast radius this wave has not measured.
  // Casing itself still goes through the ONE title-caser, so this is a second
  // word-SPLITTING rule, not a second title-caser.
  return titleCaseLabel(leaf.replace(/-/g, " "));
}
