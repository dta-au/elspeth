import { describe, expect, it } from "vitest";

import { expectNoIdentifiersInDefaultDom } from "./defaultDomPins";

/** Build a detached container so these cases need no React render. */
function mount(html: string): HTMLElement {
  const container = document.createElement("div");
  container.innerHTML = html;
  document.body.appendChild(container);
  return container;
}

describe("expectNoIdentifiersInDefaultDom — allowAriaLabelSelectors", () => {
  it("skips a snake_case aria-label on an element inside a matching selector, and on its descendants", () => {
    // closest() is what implements "and their descendants": the <button> does
    // not match `.import-yaml-actions` itself, its parent does.
    const container = mount(
      `<div class="import-yaml-actions">
         <button aria-label="Remove disabled component legacy_sink">Remove</button>
       </div>`,
    );
    expect(() =>
      expectNoIdentifiersInDefaultDom(container, {
        allowAriaLabelSelectors: [".import-yaml-actions"],
      }),
    ).not.toThrow();
  });

  it("STILL FAILS on the same aria-label outside the exempted subtree", () => {
    // The mutation kill for `if (ariaExempt.length > 0) continue;` and for
    // `?? ["*"]`: an exemption must be scoped, never global. Both mutations
    // make this case pass, which is why it is written.
    const container = mount(
      `<div class="import-yaml-actions"><button aria-label="Remove legacy_sink">Remove</button></div>
       <nav aria-label="Jump to legacy_sink"></nav>`,
    );
    expect(() =>
      expectNoIdentifiersInDefaultDom(container, {
        allowAriaLabelSelectors: [".import-yaml-actions"],
      }),
    ).toThrow();
  });

  it("STILL FAILS on snake_case VISIBLE TEXT inside the exempted subtree", () => {
    // The mutation kill for an exemption that leaks into the text scan. The
    // helper's contract is "the aria-label loop only"; this is the assertion
    // that makes that sentence executable.
    const container = mount(
      `<div class="import-yaml-actions">
         <button aria-label="Remove legacy_sink">legacy_sink</button>
       </div>`,
    );
    expect(() =>
      expectNoIdentifiersInDefaultDom(container, {
        allowAriaLabelSelectors: [".import-yaml-actions"],
      }),
    ).toThrow();
  });
});

describe("expectNoIdentifiersInDefaultDom — allowAriaLabelSelfSelectors", () => {
  it("skips a snake_case aria-label ON the matching element", () => {
    const container = mount(
      `<article class="pipeline-spec-card" aria-label="Node extract_invoice">
         <h4>Extract Invoice</h4>
       </article>`,
    );
    expect(() =>
      expectNoIdentifiersInDefaultDom(container, {
        allowAriaLabelSelfSelectors: ["article.pipeline-spec-card"],
      }),
    ).not.toThrow();
  });

  it("STILL FAILS on an aria-labelled DESCENDANT of a self-exempted element", () => {
    // THE mutation kill for the new matcher, and the whole reason it exists.
    // Implement it with `closest` instead of `matches` and this passes — which
    // is exactly the defect a first fix round shipped while claiming it had
    // narrowed the exemption to "two exact elements".
    const container = mount(
      `<article class="pipeline-spec-card" aria-label="Node extract_invoice">
         <button aria-label="Edit extract_invoice">Edit</button>
       </article>`,
    );
    expect(() =>
      expectNoIdentifiersInDefaultDom(container, {
        allowAriaLabelSelfSelectors: ["article.pipeline-spec-card"],
      }),
    ).toThrow();
  });
});

describe("expectNoIdentifiersInDefaultDom — SNAKE_RE admits digits", () => {
  it("FAILS on a digit-bearing id in visible text", () => {
    // RED before the widening: SNAKE_RE was /\b[a-z]+_[a-z_]+\b/, which admits
    // no digits, so this case PASSED — the pin certified a DOM that was
    // leaking an identifier. `llm_2` is not an exotic shape: it is what the
    // composer's own planner generates when a pipeline uses a plugin twice.
    const container = mount(`<p>Sends rows to llm_2 for classification.</p>`);
    expect(() => expectNoIdentifiersInDefaultDom(container)).toThrow();
  });

  it("FAILS on the digit-infixed and digit-suffixed shapes the planner emits", () => {
    for (const id of ["invest_cs1_done", "step_1_extract", "classify_2"]) {
      const container = mount(`<p>${id}</p>`);
      expect(() => expectNoIdentifiersInDefaultDom(container), id).toThrow();
    }
  });

  it("does NOT fire on prose or hyphenated tokens", () => {
    // The widening's cost is false positives on reader-register text, so the
    // negative side is pinned too: English prose carries no underscores, and
    // hyphens are word breaks, not identifier joins. A regex that matched
    // these would make every reader-register assertion in the suite unusable.
    const container = mount(
      `<p>Reads source data: Source (CSV). Claude Sonnet 4.6 via openrouter/anthropic.
        A well-formed run-confirm dialog, 2 of 3 steps, state-of-the-art.</p>`,
    );
    expect(() => expectNoIdentifiersInDefaultDom(container)).not.toThrow();
  });
});
