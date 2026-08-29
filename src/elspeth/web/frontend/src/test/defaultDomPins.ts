// ============================================================================
// expectNoIdentifiersInDefaultDom — the ONE executable form of the Wave 1/2
// acceptance rule: with show_advanced off, no UUID, no 32+-hex hash, and no
// snake_case identifier reaches visible text or an aria-label. Identifier
// surfaces by design are excluded here, once, so every task's pin agrees on
// what "visible" means: <code>, the mono tool-name secondary, the tool
// tooltip's identifier heading, and the tooltip trigger's "What does X do?"
// aria-label. `title` attributes are not inspected — they are where raw
// identifiers are allowed to live.
// ============================================================================

import { expect } from "vitest";

const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
const HEX32_RE = /\b[0-9a-f]{32,}\b/i;
const SNAKE_RE = /\b[a-z]+_[a-z_]+\b/;

const IDENTIFIER_SURFACES = [
  "code",
  ".tool-call-ribbon-name",
  ".tool-call-info-bubble-name",
] as const;

export function expectNoIdentifiersInDefaultDom(
  container: HTMLElement,
  options: { allowSelectors?: readonly string[] } = {},
): void {
  const clone = container.cloneNode(true) as HTMLElement;
  for (const selector of [...IDENTIFIER_SURFACES, ...(options.allowSelectors ?? [])]) {
    clone.querySelectorAll(selector).forEach((el) => el.remove());
  }
  const text = clone.textContent ?? "";
  expect(text).not.toMatch(UUID_RE);
  expect(text).not.toMatch(HEX32_RE);
  expect(text).not.toMatch(SNAKE_RE);
  for (const el of container.querySelectorAll("[aria-label]")) {
    const label = el.getAttribute("aria-label") ?? "";
    if (/^What does .* do\?$/.test(label)) continue; // ToolCallInfo trigger
    expect(label).not.toMatch(UUID_RE);
    expect(label).not.toMatch(HEX32_RE);
    expect(label).not.toMatch(SNAKE_RE);
  }
}
