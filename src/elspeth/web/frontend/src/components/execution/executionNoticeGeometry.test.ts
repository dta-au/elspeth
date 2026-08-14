import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// Sibling-mirror invariants for the two stacked app-level notice strips
// (elspeth-1df26d22ae).
//
// RunOutcomeNotice renders DIRECTLY against .app-notice-primary — two
// .alert-banner strips one DOM node apart that the eye reads as one system.
// `.run-outcome-notice` shipped with no rule at all, so one strip was
// height-capped with an ellipsised message and reset action margins while its
// twin was none of those: they visibly stepped against each other, and the
// unstyled strip's two actions drifted 16px apart because the shared
// .alert-banner-action margin (header.css) STACKED on the actions row's own
// gap.
//
// These are deliberately not declaration-existence tests: each one resolves
// the winning declaration for BOTH siblings out of the barrel and asserts
// they are EQUAL — so a future retune of the shared strip geometry stays
// green as long as it moves both strips together, and reddens only when the
// two strips diverge again. (Idiom of chatBubbleGutter.test.ts /
// workspaceChrome.test.ts.)
const barrelDir = "src/styles";
const stylesheetBarrel = readFileSync(join(barrelDir, "index.css"), "utf8");
const appCss = Array.from(
  stylesheetBarrel.matchAll(/@import\s+"(?<path>[^"]+)";/g),
)
  .map((match) => {
    const importPath = match.groups?.path;
    if (importPath === undefined) {
      throw new Error("styles/index.css import regex produced no path");
    }
    return readFileSync(join(barrelDir, importPath), "utf8");
  })
  .join("\n")
  .replace(/\/\*[\s\S]*?\*\//g, "");

interface Rule {
  selectors: string[];
  declarations: string;
}

const rules: Rule[] = Array.from(appCss.matchAll(/([^{}]+)\{([^{}]*)\}/g)).map(
  (match) => ({
    selectors: match[1].split(",").map((selector) => selector.trim()),
    declarations: match[2],
  }),
);

/** Last (cascade-winning at equal specificity) value a verbatim selector
 *  declares for `property`, or null. */
function declaration(selector: string, property: string): string | null {
  let winner: string | null = null;
  for (const rule of rules) {
    if (!rule.selectors.includes(selector)) continue;
    const match = new RegExp(`(?:^|;)\\s*${property}\\s*:([^;]+)`).exec(
      rule.declarations,
    );
    if (match) winner = match[1].trim();
  }
  return winner;
}

describe("run-outcome notice mirrors its app-notice sibling (elspeth-1df26d22ae)", () => {
  const notice = ".run-outcome-notice.alert-banner";
  const sibling = ".app-notice-primary.alert-banner";

  it.each(["height", "max-height", "gap", "padding-block"])(
    "keeps %s equal across the two stacked strips",
    (property) => {
      const noticeValue = declaration(notice, property);
      expect(noticeValue, `${notice} declares no ${property}`).not.toBeNull();
      expect(noticeValue).toBe(declaration(sibling, property));
    },
  );

  it("single-lines and ellipsises the message like the sibling's message", () => {
    for (const property of ["min-width", "overflow", "text-overflow", "white-space"]) {
      const value = declaration(".run-outcome-notice-message", property);
      expect(value, `message declares no ${property}`).not.toBeNull();
      expect(value).toBe(declaration(".app-notice-primary-message", property));
    }
  });

  it("resets the shared action margin so the row's gap is the only spacing mechanism", () => {
    // .alert-banner-action carries margin-left: var(--space-md) (header.css);
    // inside a gap-spaced actions row that margin STACKS with the gap.
    expect(
      declaration(".run-outcome-notice-actions .alert-banner-action", "margin-left"),
    ).toBe(
      declaration(".app-notice-primary-actions .alert-banner-action", "margin-left"),
    );
  });

  // Overlay treatment (elspeth-1c4687ff67 class): both app-level strips must
  // OVERLAY the application rather than participate in flow — a strip in flow
  // before the header displaces the whole viewport by its height on arrival
  // and snaps it back on dismiss. Same sibling-mirror discipline as above:
  // the assertions redden only if the two strips' overlay recipes diverge.
  describe("overlays the app exactly as .app-notice-center does", () => {
    const overlay = ".run-outcome-notice-overlay";
    const sibling = ".app-notice-center";

    it.each(["position", "top", "left", "right", "z-index", "background", "box-shadow"])(
      "keeps %s equal across the two overlay wrappers",
      (property) => {
        const overlayValue = declaration(overlay, property);
        expect(overlayValue, `${overlay} declares no ${property}`).not.toBeNull();
        expect(overlayValue).toBe(declaration(sibling, property));
      },
    );
  });
});
