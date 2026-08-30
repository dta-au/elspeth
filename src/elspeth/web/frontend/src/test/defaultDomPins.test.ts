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
