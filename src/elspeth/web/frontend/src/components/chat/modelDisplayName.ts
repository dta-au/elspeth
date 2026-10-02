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

// One part of a hyphen-mangled version number: 0, or 1-99 with no leading
// zero. Anything longer or zero-led is a date field, not a version part.
const VERSION_PART_RE = /^(?:0|[1-9][0-9]?)$/;

export function modelDisplayName(modelId: string): string {
  const leaf = modelId.slice(modelId.lastIndexOf("/") + 1);
  // Only the PHRASING is guarded, never the path stripping: a provider path is
  // still reduced to its leaf, so a chip cannot start leaking "openrouter".
  if (NON_WORDISH_LEAF_RE.test(leaf)) return leaf;

  // Some providers hyphenate every part of a version number instead of
  // writing the conventional dotted form ("claude-opus-4-7" where OpenRouter
  // and Anthropic elsewhere write "claude-sonnet-4.6"). Left alone, the
  // trailing digits read as disconnected words: "Claude Opus 4 7". Re-join a
  // trailing run of two or more whole-digit hyphen segments with dots before
  // title-casing the rest. An id that already carries its own dotted version
  // ("claude-sonnet-4.6") has that version as ONE hyphen segment ("4.6"),
  // which fails the whole-digit test and is left untouched, so this never
  // re-splits or re-joins a version the id already wrote correctly. A mixed
  // segment ("4o") also fails the test and is left as an ordinary word.
  //
  // Only a VERSION-SHAPED run is re-joined: two or three parts, each 0 or a
  // one-/two-digit number without a leading zero. A hyphenated date stamp is
  // also a trailing whole-digit run ("gpt-4o-2024-08-06", "gpt-4-0613",
  // "gemini-2.5-flash-lite-preview-06-17"), and dotting it would present a
  // date as a version number ("GPT 4o 2024.08.06") on the run-confirm consent
  // surface. A run of two or more parts that is not version-shaped -- a part
  // of three or more digits (a year, an MMDD stamp) or a zero-led two-digit
  // part (an MM or DD field) -- is an identifier, so the leaf is returned RAW,
  // the same ruling NON_WORDISH_LEAF_RE makes for a yyyymmdd stamp.
  const parts = leaf.split("-");
  let versionCut = parts.length;
  while (versionCut > 0 && /^[0-9]+$/.test(parts[versionCut - 1])) {
    versionCut -= 1;
  }
  const versionParts = parts.slice(versionCut);
  const wordParts = parts.slice(0, versionCut);
  if (versionParts.length < 2) {
    // No hyphen-mangled version to repair -- the original single-pass
    // behaviour. The `-` -> ` ` pre-split is DELIBERATE and local.
    // titleCaseLabel splits on /[_\s]+/ (pluginDisplayName.ts), so hyphens
    // are not word breaks there and titleCaseLabel("claude-sonnet-4.6")
    // yields "Claude-sonnet-4.6". Widening that regex to /[_\s-]+/ would
    // change every hyphenated author-chosen node id across the frontend,
    // which is a blast radius this wave has not measured. Casing itself
    // still goes through the ONE title-caser, so this is a second
    // word-SPLITTING rule, not a second title-caser.
    return titleCaseLabel(leaf.replace(/-/g, " "));
  }
  if (versionParts.length > 3 || !versionParts.every((part) => VERSION_PART_RE.test(part))) {
    return leaf;
  }
  const version = versionParts.join(".");
  if (wordParts.length === 0) return version;
  return `${titleCaseLabel(wordParts.join(" "))} ${version}`;
}
