// ============================================================================
// expectNoIdentifiersInDefaultDom — the ONE executable form of the Wave 1/2
// acceptance rule: with show_advanced off, no UUID, no 32+-hex hash, and no
// snake_case identifier reaches visible text or an aria-label. Identifier
// surfaces by design are excluded here, once, so every task's pin agrees on
// what "visible" means: <code>, the mono tool-name secondary, the tool
// tooltip's identifier heading, and the tooltip trigger's "What does X do?"
// aria-label. `title` attributes are not inspected — they are where raw
// identifiers are allowed to live.
//
// The two aria options exempt accessible names that carry an author-chosen id
// by design.
// `allowAriaLabelSelfSelectors` matches with `matches()` — that element
// ONLY; a labelled descendant is still scanned. Prefer it.
// `allowAriaLabelSelectors` matches with `closest()` — the element AND its
// whole subtree. Use it only when the labels sit on CHILDREN of the
// selector; it silently exempts every label added inside that subtree later.
// Every component test that renders user-facing prose calls this helper.
// Direct coverage of the helper itself lives in `defaultDomPins.test.ts`;
// extend it when you extend the options.
// ============================================================================

import { expect } from "vitest";

const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
const HEX32_RE = /\b[0-9a-f]{32,}\b/i;
// Digits are part of the shape, not an edge case: the composer auto-suffixes a
// duplicate plugin (`llm_2`, `classify_2`) and authors write `step_1_extract`,
// so the pre-2026-08-31 form /\b[a-z]+_[a-z_]+\b/ was blind to the MOST
// common production id and every call was a false certificate for it. Still
// requires an underscore, and still starts on a letter, so hyphenated prose
// ("well-formed") and bare numbers ("2 of 3") do not match.
const SNAKE_RE = /\b[a-z][a-z0-9]*_[a-z0-9_]*[a-z0-9]\b/;

const IDENTIFIER_SURFACES = [
  "code",
  ".tool-call-ribbon-name",
  ".tool-call-info-bubble-name",
] as const;

// `textContent` CONCATENATES adjacent elements' text with no separator, which
// silently defeats the \b anchors below: a row rendering "Explain" beside
// "rate_colours" reads as "Explainrate_colours", where SNAKE_RE cannot match
// because the token no longer starts on a word boundary. Measured 2026-08-30 —
// it let a bare node id through this very pin. Join text NODES instead, so
// every DOM-adjacent run is anchored on its own.
function visibleText(root: HTMLElement): string {
  const parts: string[] = [];
  const walker = root.ownerDocument.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) parts.push(walker.currentNode.nodeValue ?? "");
  return parts.join("\n");
}

export function expectNoIdentifiersInDefaultDom(
  container: HTMLElement,
  options: {
    allowSelectors?: readonly string[];
    /** SUBTREE exemption (`closest`): the matched element AND its descendants
     *  are skipped by the aria-label loop. Use ONLY when the labels sit on
     *  CHILDREN of the selector — e.g. `.import-yaml-actions`, a container of
     *  two aria-labelled buttons. A container selector silently exempts every
     *  label added inside it later, so prefer the self-only option below.
     *  Exempts the aria-label loop only; visible text is still scanned
     *  (elspeth-13b69b5846). */
    allowAriaLabelSelectors?: readonly string[];
    /** SELF-ONLY exemption (`matches`): that element alone is skipped, and an
     *  aria-labelled DESCENDANT is still scanned. Use whenever the label is on
     *  the element the selector names — the Spec-tab `<article>` and the
     *  OptionRows region both are. This is the default choice; reach for
     *  `allowAriaLabelSelectors` only when children carry the labels. */
    allowAriaLabelSelfSelectors?: readonly string[];
  } = {},
): void {
  const clone = container.cloneNode(true) as HTMLElement;
  for (const selector of [...IDENTIFIER_SURFACES, ...(options.allowSelectors ?? [])]) {
    clone.querySelectorAll(selector).forEach((el) => el.remove());
  }
  const text = visibleText(clone);
  expect(text).not.toMatch(UUID_RE);
  expect(text).not.toMatch(HEX32_RE);
  expect(text).not.toMatch(SNAKE_RE);
  const ariaSubtreeExempt = options.allowAriaLabelSelectors ?? [];
  const ariaSelfExempt = options.allowAriaLabelSelfSelectors ?? [];
  for (const el of container.querySelectorAll("[aria-label]")) {
    const label = el.getAttribute("aria-label") ?? "";
    if (/^What does .* do\?$/.test(label)) continue; // ToolCallInfo trigger
    // SELF-only: `matches`, so a labelled DESCENDANT is still scanned.
    if (ariaSelfExempt.some((selector) => el.matches(selector))) continue;
    // SUBTREE: `closest`, for a container whose CHILDREN carry the labels.
    if (ariaSubtreeExempt.some((selector) => el.closest(selector) !== null)) continue;
    expect(label).not.toMatch(UUID_RE);
    expect(label).not.toMatch(HEX32_RE);
    expect(label).not.toMatch(SNAKE_RE);
  }
}
