import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
// Resolve through each real consumer, including any nested dependency copies.
const mermaidRequire = createRequire(require.resolve("mermaid"));
const katex = mermaidRequire("katex");
const stylelintRequire = createRequire(require.resolve("stylelint"));
const selectorParser = stylelintRequire("postcss-selector-parser");
const sourceMapRequires = ["postcss", "css-tree"].map(
  (consumer) => createRequire(require.resolve(consumer)),
);
const rendererOptions = { throwOnError: true, displayMode: true };

for (const output of ["mathml", "htmlAndMathml"]) {
  test(`Mermaid KaTeX preserves ordinary ${output} output`, () => {
    const rendered = katex.renderToString("x^2+\\frac{1}{2}", { ...rendererOptions, output });
    assert.match(rendered, /<mfrac>/);
    assert.match(rendered, /class="katex"/);
    if (output === "htmlAndMathml") assert.match(rendered, /class="katex-html"/);
  });

  test(`Mermaid KaTeX ignores inherited trust in ${output} options`, () => {
    const options = Object.assign(Object.create({ trust: true }), rendererOptions, { output });
    const rendered = katex.renderToString("\\href{https://example.invalid/options}{x}", options);
    assert.doesNotMatch(rendered, /href="https:\/\/example\.invalid/);
    // An explicitly owned trust option remains a supported public API.
    const trusted = katex.renderToString("\\href{https://example.invalid/owned}{x}", {
      ...rendererOptions, output, trust: true,
    });
    assert.match(trusted, /href="https:\/\/example\.invalid\/owned"/);
  });
}

test("Mermaid KaTeX ignores Object.prototype trust pollution", () => {
  const previous = Object.getOwnPropertyDescriptor(Object.prototype, "trust");
  try {
    Object.defineProperty(Object.prototype, "trust", { configurable: true, writable: true, value: true });
    const rendered = katex.renderToString("\\href{https://example.invalid/prototype}{x}", {
      ...rendererOptions, output: "mathml",
    });
    assert.doesNotMatch(rendered, /href="https:\/\/example\.invalid/);
  } finally {
    if (previous) Object.defineProperty(Object.prototype, "trust", previous);
    else delete Object.prototype.trust;
  }
});

test("Stylelint selector parser preserves flat selectors and ordinary CSS", () => {
  const flat = ".a".repeat(10_000);
  assert.equal(selectorParser().processSync(flat), flat);
  assert.equal(selectorParser().processSync(".panel > #title:hover, [data-state='open']"), ".panel > #title:hover, [data-state='open']");
});

for (const consumerRequire of sourceMapRequires) {
  const { SourceMapConsumer } = consumerRequire("source-map-js");
  const section = (line, column = 0) => ({
    offset: { line, column },
    map: { version: 3, sources: ["input.js"], names: [], mappings: "AAAA" },
  });
  test(`source-map consumer ${consumerRequire.resolve("source-map-js")} bounds indexed offsets`, () => {
    for (const line of [-1, 0.5, Infinity, Number.MAX_SAFE_INTEGER]) {
      assert.throws(() => new SourceMapConsumer({ version: 3, sections: [section(line)] }), /Section offset/);
    }
    assert.throws(() => new SourceMapConsumer({ version: 3, sections: [section(0, -1)] }), /Section offset/);
    assert.throws(() => new SourceMapConsumer({ version: 3, sections: [{
      offset: { line: 6_000_000, column: 0 },
      map: { version: 3, sections: [section(6_000_000)] },
    }] }), /including offsets of nested sections/);
    const normal = new SourceMapConsumer({ version: 3, sections: [section(2)] });
    assert.deepEqual(normal.generatedPositionFor({ source: "input.js", line: 1, column: 0 }), { line: 3, column: 0 });
  });
}
