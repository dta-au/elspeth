import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

import { describe, expect, it } from "vitest";

// Whole-tree class-name gate (elspeth-729872658a).
//
// tokenReferences.test.ts gates every var(--x) against the stylesheets, so an
// undefined CUSTOM PROPERTY cannot ship. Nothing gated the same mistake one
// level up: a className the TSX emits that no stylesheet defines. That
// failure is silent by construction — the element simply inherits whatever
// its ancestors set — and the 2026-08-14 professionalisation review found
// FIFTEEN such holes shipped through the gap, three of them P1 (a
// role="dialog" drawer with UA disc bullets; the shared-inspect page with no
// scroll owner). Five review lanes found instances independently without any
// realising it was one pattern.
//
// This is the tree-wide version of the gate the directory-scoped prototypes
// established. Those gates stay: catalogClassNames.test.ts,
// executionClassNames.test.ts and recoveryStyles.test.ts carry
// directory-specific documentation and assert their allowlisted names have
// NO rule, which keeps the entries below honest — adding a rule for one of
// their names fails BOTH gates until both records are updated.
//
// The allowlist is for genuinely rule-less names, and every entry states WHY
// the element needs no rule. "It looks fine" is not a reason — if the
// element needs a treatment, give it one in its area stylesheet. The
// adjudication standard, distilled from the 2026-08-15 whole-tree pass that
// fixed 19 real holes before this gate landed:
//
//   - A class whose element gets its chrome from a DEFINED co-class (.btn,
//     .btn-compact, .guided-turn, .alert-banner-action, .link-button,
//     .message-row…) is a hook/identity token and needs no rule. Watch
//     Button variant="bare": it emits ONLY the caller's className, so a bare
//     Button whose classes are all undefined is a RAW UA-DEFAULT BUTTON —
//     that shape shipped twice and is never allowlistable.
//   - A class used as a SELECTOR by useFocusTrap's third argument or by
//     tests is a behaviour hook; the consumer is the evidence.
//   - A state/route marker whose states deliberately share the base
//     treatment is fine ONLY when the base treatment actually exists
//     (defined co-class or inline carrier) — a marker on an unstyled
//     element is a defect, not a marker.
//   - base.css declares NO element reset for button, blockquote, ol, ul,
//     h3, header or p. A bare one of those with no defined class renders at
//     raw UA defaults; inheritance from a styled ancestor does NOT reach
//     margins, markers or form chrome.
const srcRoot = "src";
const barrelDir = join("src", "styles");

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

/** Every class name any barrel stylesheet writes a selector for. */
const definedClasses = new Set(
  Array.from(appCss.matchAll(/\.([A-Za-z_][\w-]*)/g)).map((match) => match[1]),
);

/**
 * Blank comments while preserving newlines (the primitiveCensus.test.ts
 * idiom): doc comments legitimately NAME class tokens ("see
 * .chat-panel-guided-log"), and a census that flagged prose would teach
 * people to stop writing it. Same known-and-accepted limits as
 * primitiveCensus: an unterminated block-open inside a string could
 * over-strip, and the failure mode is a visible false finding, not a silent
 * pass.
 */
function blankComments(source: string): string {
  const blank = (text: string) => text.replace(/[^\n]/g, " ");
  return source
    .replace(/\/\*[\s\S]*?\*\//g, blank)
    .replace(/(^|\s)\/\/[^\n]*/gm, (match, lead: string) => lead + blank(match.slice(lead.length)));
}

/**
 * Class names a component applies. Handles the three forms the tree uses: a
 * plain string, a `{...}` expression holding string literals, and a template
 * literal with `${…}` interpolations.
 *
 * Interpolation is handled by POSITION, not by stripping: in
 * `audit-icon-${meta.tone}` the fragment "audit-icon-" is a PREFIX completed
 * at runtime, not a class name, so a token abutting an interpolation on
 * either side is dropped rather than reported as undefined.
 *
 * COMPARISON OPERANDS ARE NOT CLASS NAMES: in
 * `className={copyState === "failed" ? "save-for-review-error" : undefined}`
 * the literal "failed" is the condition's operand. The catalog prototype's
 * extractor read every literal in the span and manufactured a phantom name
 * for exactly this shape (twice tree-wide), so literals adjacent to a
 * comparison operator on either side are excluded here.
 */
function classNamesIn(source: string): Set<string> {
  const found = new Set<string>();

  function addLiteral(raw: string, isTemplate: boolean): void {
    const segments = isTemplate ? raw.split(/\$\{[^}]*\}/g) : [raw];
    segments.forEach((segment, index) => {
      const tokens = segment.split(/\s+/).filter((token) => token.length > 0);
      const abutsBefore = index > 0 && !/^\s/.test(segment);
      const abutsAfter = index < segments.length - 1 && !/\s$/.test(segment);
      const start = abutsBefore ? 1 : 0;
      const end = abutsAfter ? tokens.length - 1 : tokens.length;
      for (const token of tokens.slice(start, end)) {
        if (/^[A-Za-z_][\w-]*$/.test(token)) found.add(token);
      }
    });
  }

  for (const match of source.matchAll(/className\s*=\s*/g)) {
    const i = (match.index ?? 0) + match[0].length;
    let span = source.slice(i);
    if (source[i] === "{") {
      let depth = 0;
      let end = i;
      for (; end < source.length; end += 1) {
        if (source[end] === "{") depth += 1;
        else if (source[end] === "}") {
          depth -= 1;
          if (depth === 0) break;
        }
      }
      span = source.slice(i + 1, end);
    } else {
      const quote = source[i];
      const end = source.indexOf(quote, i + 1);
      span = source.slice(i, end + 1);
    }
    for (const literal of span.matchAll(/"([^"]*)"|'([^']*)'|`([^`]*)`/g)) {
      const before = span.slice(0, literal.index ?? 0);
      const after = span.slice((literal.index ?? 0) + literal[0].length);
      // A literal compared with ===/!==/==/!= (either side) is a condition
      // operand, not a class name.
      if (/[=!]==?\s*$/.test(before) || /^\s*[=!]==?/.test(after)) continue;
      if (literal[3] !== undefined) addLiteral(literal[3], true);
      else addLiteral(literal[1] ?? literal[2] ?? "", false);
    }
  }
  return found;
}

function walkProductTsxFiles(dir: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      found.push(...walkProductTsxFiles(path));
    } else if (entry.endsWith(".tsx") && !entry.endsWith(".test.tsx")) {
      found.push(path);
    }
  }
  return found;
}

/**
 * Class names that are applied but carry NO rule, each with the reason it
 * needs none. Adjudicated name-by-name in the 2026-08-15 whole-tree pass
 * (dossier: every name's element, co-classes and consumers were read; 19
 * names failed this standard and got rules or defined co-classes instead of
 * entries here). Keyed by class name so a stale entry is visible.
 *
 * Six entries are ALSO recorded by their directory gates
 * (catalogClassNames / executionClassNames / recoveryStyles.test.ts) — keep
 * both records in step when one changes.
 */
const RULE_LESS_BY_DESIGN: Record<string, string> = {
  // --- Button/primitive hooks: chrome carried by defined co-classes ------
  "ack-card-accept-btn":
    "Button variant=primary hook — .btn/.btn-primary carry the chrome; " +
    "AcknowledgementCard.test.tsx asserts the token as an identity hook.",
  "ack-card-amend-btn":
    "Button default-variant hook — .btn carries the chrome; the token adds " +
    "no treatment of its own.",
  "ack-card-cancel-btn":
    "Button default-variant hook inside the defined .ack-card-amend-actions " +
    "row; .btn carries the chrome.",
  "ack-card-submit-btn":
    "Button variant=primary hook — .btn/.btn-primary carry the chrome; the " +
    "token adds no treatment.",
  "guided-explain-btn":
    "Compact-Button hook; .btn-compact carries all chrome and " +
    "ChatPanel.test.tsx queries the token as a selector.",
  "guided-schema-edit-toggle":
    "Bare Button whose chrome is the defined co-class .guided-turn-secondary " +
    "— the house idiom for secondary guided controls; token is a hook.",
  "inline-source-fallback-prompt-accept":
    "Compact primary Button composing .btn-compact + .btn-primary (documented " +
    "in-file); the token is a per-instance hook.",
  "inline-source-disambiguation-turn-confirm":
    "Button variant=primary hook — .btn/.btn-primary carry the chrome; also " +
    "the F-19 focus-on-mount target via a React ref, not a selector.",
  "inline-source-disambiguation-turn-single":
    "Button default-variant hook — .btn carries the chrome.",
  "inline-source-disambiguation-turn-edit":
    "Button default-variant hook — .btn carries the chrome.",
  "inline-source-disambiguation-turn-not-source":
    "Bare Button styled by the defined .link-button co-class (the F-10 " +
    "link-style escape); the bespoke token is an identity hook.",
  "inline-source-created-turn-edit":
    "Bare Button styled by the defined .link-button co-class; the bespoke " +
    "token is an identity hook.",
  "app-notice-more":
    "Bare Button styled by the defined .alert-banner-action co-class (tone " +
    "variants recolour it with the banner); token is the popover trigger hook.",
  "app-notice-primary-action":
    "Bubbling close-on-activate wrapper span; the caller-supplied node " +
    "inside is the interactive control and every producer styles it with " +
    ".alert-banner-action, so the wrapper needs no treatment.",
  "app-notice-item-action":
    "Same close-on-activate wrapper shape as app-notice-primary-action, in " +
    "the popover list; the styled control is the caller-supplied node inside.",
  "completion-bar-save-for-review":
    "Button default-variant hook — .btn carries the chrome and the " +
    ".completion-bar > * rule forces the width; consumed as a data-testid.",
  // --- Focus-trap / selector hooks ---------------------------------------
  "confirm-dialog-confirm-btn":
    "useFocusTrap initial-focus selector (ConfirmDialog.tsx); three defined " +
    "co-classes (.btn, .btn-primary|danger, .confirm-dialog-btn) carry every " +
    "visual property.",
  "save-for-review-close":
    "useFocusTrap initial-focus selector (SaveForReviewDialog.tsx), pinned " +
    "by its test; the Button primitive's .btn supplies all chrome.",
  "recovery-panel-apply":
    "useFocusTrap selector; .btn/.btn-primary carry the chrome. Also " +
    "recorded in recoveryStyles.test.ts SELECTOR_ONLY_CLASSES — keep both.",
  "recovery-panel-discard":
    "Selector-only sibling of recovery-panel-apply; .btn/.btn-danger carry " +
    "the chrome. Also recorded in recoveryStyles.test.ts — keep both.",
  // --- Identity tokens whose element is styled by a defined co-class -----
  "blob-manager":
    "Base token beside the defined .blob-manager-container on the same " +
    "element; blobs.css writes every functional rule against the children.",
  "blob-row":
    "Base token beside the defined .blob-row-container, which the surface " +
    "tests pin as the treatment's real home.",
  "message-bubble":
    "Legacy identity token; the row is styled by the defined .message-row " +
    "plus role modifier, and the inner box by the defined .bubble family.",
  "message-bubble--system":
    "Role marker whose visual job is done by the defined sibling token " +
    ".message-row--system on the same element.",
  "interpretation-review-confirmation":
    "Identity token on the resolve-confirmation bubble; the defined " +
    ".message-row/.message-row--assistant + .bubble co-classes carry the " +
    "treatment and tests query the data-testid.",
  "guided-inspect-turn":
    "Turn-identity hook; the shared defined .guided-turn co-class carries " +
    "the card treatment (pattern across the guided turn family).",
  "guided-single-select":
    "Turn-identity hook; .guided-turn carries the card treatment.",
  "guided-multi-select":
    "Turn-identity hook; .guided-turn carries the card treatment.",
  "guided-schema-form":
    "Turn-identity hook; .guided-turn carries the card treatment.",
  "guided-component-review":
    "Turn-identity hook; .guided-turn carries the card and every child in " +
    "the subtree has its own guided.css rule.",
  "composing-indicator":
    "Identity hook beside the defined .composing-row; also the base the " +
    "defined .composing-indicator--terminal compound builds on, and " +
    "ChatPanel.test.tsx queries it as a selector.",
  // "app-root--shared-inspect" was here as a route marker "kept so a future
  // shared-inspect stylesheet can scope to it". That future arrived: it now
  // carries a real rule (header.css) cancelling the shell's top reserve, which
  // the shared-inspect route has nothing to reserve for — it renders neither
  // the header nor either overlay strip.
  "audit-readiness--shared":
    "Read-only variant marker beside the defined .audit-readiness; the " +
    "shared panel deliberately renders identically to the composer's.",
  // --- State markers deliberately sharing the base treatment -------------
  "shared-inspect-view":
    "Treatment carried by the inline MAIN_STYLE constant pending the " +
    "documented lift-and-shift (elspeth-2ff1b0b4ad fixed the P1 scroll " +
    "owner inline; the class stays so extraction is mechanical). This entry " +
    "stops being true the moment a shared-inspect stylesheet lands.",
  "shared-inspect-view--error":
    "Render-state marker; every branch carries the same inline MAIN_STYLE " +
    "and the error content is distinguished semantically (role=alert).",
  "shared-inspect-view--loaded":
    "Render-state marker on the success branch; the three branches " +
    "deliberately share one inline-carried frame.",
  "shared-inspect-banner":
    "Treatment carried by the inline BANNER_STYLE constant, same " +
    "lift-and-shift deferral as shared-inspect-view (elspeth-2ff1b0b4ad).",
  "side-rail-execute-reason--advisory":
    "Deliberate no-op modifier: sidebar.css documents that it exists for " +
    "future differentiation and intentionally shares the defined base rule " +
    "today (no colour-only signal).",
  "pipeline-validation-summary--neutral":
    "Tone modifier whose siblings only tint a glyph descendant; the neutral " +
    "branch renders no glyph, so there is nothing for a rule to tint. Base " +
    ".pipeline-validation-summary supplies the shared treatment.",
  "wire-stage__blockers-list--issues":
    "Constant (unconditional) modifier with no sibling variant anywhere; " +
    "the defined base class carries the list treatment. Vestigial — delete " +
    "the token rather than write a rule if it ever gets in the way.",
  "audit-icon-unknown":
    "Forward-compatibility modifier rendering in .audit-icon's neutral base " +
    "treatment: an unrecognised backend flag must not be coloured as though " +
    "its tone were known. Also recorded in catalogClassNames.test.ts.",
  // --- Text/layout members that inherit their whole treatment ------------
  "audit-icon-label":
    "Inner text span filling the .audit-icon chip; every visual property " +
    "comes from the parent. Also recorded in catalogClassNames.test.ts.",
  "audit-readiness-row-label-text":
    "Inner span inside the defined .audit-readiness-row-label, which " +
    "supplies the label's type, colour and spacing.",
  "ack-card-decision":
    "Decision summary line inside the defined .ack-card surface; the " +
    "plain-prose member of a set whose <code> sibling (.ack-card-model) is " +
    "differentiated for a stated reason.",
  "ack-card-error-body":
    "Body text inside the defined .ack-card-error box; the heading sibling " +
    "is defined because it needs a weight change, the body keeps the box's " +
    "base register.",
  "ack-stack-error-body":
    "Mirror of ack-card-error-body one level up, inside the defined " +
    ".ack-stack-error box.",
  "interpretation-review-confirmation-user-term":
    "Semantic <em> supplies the intended emphasis for the quoted user term; " +
    "the class adds no treatment of its own.",
  "wire-review-route":
    "Grouping span around the row's primary from→to text, which correctly " +
    "renders in the list's base register; row rhythm comes from the defined " +
    ".guided-wire-review li rule.",
  "discard-summary-reasons":
    "Span inside the defined .discard-summary-warning grid, which supplies " +
    "spacing, tint and type. Also recorded in executionClassNames.test.ts.",
  "guided-proposal-revision-scope":
    "Wrapping <label> whose text and nested <select> inherit the composer's " +
    "form register; the control carries the visible chrome.",
  // --- Layout-neutral containers and grouping elements -------------------
  "run-diagnostics":
    "Disclosure container; collapsed state is the [hidden] attribute and " +
    "treatment lives on the defined .run-diagnostics-panel child. Also " +
    "recorded in executionClassNames.test.ts.",
  "inline-source-created-turn-header":
    "Layout-neutral <header> wrapper; the child .inline-source-created-turn-" +
    "facts rule supplies the whole layout.",
  "inline-source-disambiguation-turn-header":
    "Layout-neutral <header> wrapper; its children (-title, -explainer, " +
    "-input) carry the content treatment.",
  "inline-source-disambiguation-turn-escape":
    "Single-child presentational wrapper around the escape Button, " +
    "documented in-file as presentational.",
  "inline-source-disambiguation-turn-row":
    "List item inheriting marker, indent and rhythm from the defined parent " +
    "-rows rule — the list level is the treatment's correct home.",
  "chat-panel-guided-log":
    "Live-region/focus container addressed by a React ref; scroll ownership " +
    "belongs to .guided-authoring-scroll and each appended turn carries its " +
    "own .guided-turn card.",
  "composing-status-summary":
    "role=status live-region boundary nested in the defined " +
    ".composing-working-view; its .composing-label child carries the text " +
    "treatment.",
  "guided-readonly-graph__edges":
    "SVG <g> grouping/z-order container with no box model; every edge child " +
    "carries its own defined class.",
  "guided-readonly-graph__nodes":
    "SVG <g> grouping container; node children carry defined classes and " +
    "positioning is per-node transform attributes, not CSS.",
};

const componentSources = walkProductTsxFiles(srcRoot).map((path) => ({
  file: relative(srcRoot, path),
  source: blankComments(readFileSync(path, "utf8")),
}));

describe("every emitted class name is backed by a stylesheet (elspeth-729872658a)", () => {
  it("extracts a non-trivial corpus (the gate itself is not inert)", () => {
    // An extractor that silently matched nothing would certify any tree —
    // the exact shape the 2026-08-14 review kept finding in other gates.
    expect(componentSources.length).toBeGreaterThan(80);
    const total = componentSources.reduce(
      (n, { source }) => n + classNamesIn(source).size,
      0,
    );
    expect(total).toBeGreaterThan(500);
    expect(definedClasses.size).toBeGreaterThan(800);
    expect(definedClasses.has("btn")).toBe(true);
    expect(definedClasses.has("class-that-no-stylesheet-defines")).toBe(false);
  });

  it("does not read comparison operands or comments as class names", () => {
    // Pins the two extractor behaviours that separate this gate from the
    // catalog prototype: ternary-condition operands ("failed"/"guided"
    // shipped as phantom findings of the prototype extractor) and doc
    // comments naming real class tokens.
    const ternary = classNamesIn(
      'const x = <a className={copyState === "failed" ? "real-class" : undefined} />;',
    );
    expect(ternary.has("failed")).toBe(false);
    expect(ternary.has("real-class")).toBe(true);
    const yoda = classNamesIn(
      'const x = <a className={"failed" === copyState ? "real-class" : "other-class"} />;',
    );
    expect(yoda.has("failed")).toBe(false);
    expect(yoda.has("other-class")).toBe(true);
    const commented = blankComments(
      '// className="commented-out-class"\nconst x = <a className="live-class" />;',
    );
    expect(classNamesIn(commented).has("commented-out-class")).toBe(false);
    expect(classNamesIn(commented).has("live-class")).toBe(true);
    // Interpolation-abutting fragments are prefixes, not names.
    const template = classNamesIn(
      "const x = <a className={`audit-icon-${tone} standalone-name`} />;",
    );
    expect(template.has("audit-icon-")).toBe(false);
    expect(template.has("standalone-name")).toBe(true);
  });

  it("defines every class the tree applies (or records why it needs none)", () => {
    const undefinedUses: string[] = [];
    for (const { file, source } of componentSources) {
      for (const name of classNamesIn(source)) {
        if (definedClasses.has(name)) continue;
        if (name in RULE_LESS_BY_DESIGN) continue;
        undefinedUses.push(`${name} (applied by ${file})`);
      }
    }
    expect(
      undefinedUses.sort(),
      "class names no barrel stylesheet defines — the element silently " +
        "inherits its ancestors' type and spacing instead of the treatment " +
        "the name promises. Add a rule to the area stylesheet, or add the " +
        "name to RULE_LESS_BY_DESIGN with the reason it needs none " +
        "(chrome-carrying defined co-class, focus-trap/test selector hook, " +
        "or a documented base-treatment-sharing marker)",
    ).toEqual([]);
  });

  it("keeps the rule-less allowlist honest", () => {
    for (const [name, reason] of Object.entries(RULE_LESS_BY_DESIGN)) {
      const applied = componentSources.some(({ source }) =>
        classNamesIn(source).has(name),
      );
      expect(applied, `${name} is allowlisted but no component applies it`).toBe(
        true,
      );
      expect(
        definedClasses.has(name),
        `${name} now HAS a rule — drop it from the allowlist (and from its ` +
          `directory gate's record, if it has one)`,
      ).toBe(false);
      expect(
        reason.length,
        `${name} needs a real reason, not a placeholder`,
      ).toBeGreaterThan(40);
    }
  });
});
