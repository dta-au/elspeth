import { expect, test } from "@playwright/test";

// Exercise Mermaid's real external KaTeX import with the browser's MathML and
// SVG layout. jsdom dependency tests cannot establish rendering compatibility.
for (const forceLegacyMathML of [false, true]) {
  test(`Mermaid math renders with legacy output ${forceLegacyMathML}`, async ({ page }) => {
    await page.goto("/");
    const result = await page.evaluate(async (legacy) => {
      const modulePath = "/node_modules/mermaid/dist/mermaid.core.mjs";
      const { default: mermaid } = await import(modulePath);
      mermaid.initialize({ startOnLoad: false, securityLevel: "strict", forceLegacyMathML: legacy });
      const { svg } = await mermaid.render("release-math-control", 'flowchart TD\n A["$$x^2+\\frac{1}{2}$$"] --> B["ordinary"]');
      const container = document.createElement("div");
      container.innerHTML = svg;
      document.body.append(container);
      const math = container.querySelector("math");
      const bounds = math?.getBoundingClientRect();
      return {
        math: math !== null,
        fraction: container.querySelector("mfrac") !== null,
        legacyHtml: container.querySelector(".katex-html") !== null,
        width: bounds?.width ?? 0,
        height: bounds?.height ?? 0,
        ordinary: container.textContent?.includes("ordinary") ?? false,
      };
    }, forceLegacyMathML);
    expect(result.math).toBe(true);
    expect(result.fraction).toBe(true);
    expect(result.legacyHtml).toBe(forceLegacyMathML);
    expect(result.ordinary).toBe(true);
    if (!forceLegacyMathML) {
      expect(result.width).toBeGreaterThan(0);
      expect(result.height).toBeGreaterThan(0);
    }
  });
}
