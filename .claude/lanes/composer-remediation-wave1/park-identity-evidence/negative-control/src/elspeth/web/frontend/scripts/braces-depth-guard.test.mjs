import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
// Resolve through the real consumers so a nested, unpatched copy cannot hide
// behind a separately installed test dependency.
const stylelintRequire = createRequire(require.resolve("stylelint"));
const openapiRequire = createRequire(require.resolve("openapi-typescript"));
const fastGlobRequire = createRequire(openapiRequire.resolve("fast-glob"));
const consumerRequires = [stylelintRequire, fastGlobRequire];
const glob = fastGlobRequire("fast-glob");

function nested(open, close, depth) {
  return open.repeat(depth) + "x" + close.repeat(depth);
}

function ast(depth) {
  let node = { type: "text", value: "x" };
  for (let level = 0; level < depth; level++) {
    const parent = { type: "paren", nodes: [node] };
    node.parent = parent;
    node = parent;
  }
  const root = { type: "root", nodes: [node] };
  node.parent = root;
  return root;
}

for (const [index, consumerRequire] of consumerRequires.entries()) {
  const micromatchRequire = createRequire(consumerRequire.resolve("micromatch"));
  const braces = micromatchRequire("braces");
  const label = index === 0 ? "stylelint" : "openapi-typescript/fast-glob";

  for (const method of ["parse", "compile", "expand", "stringify"]) {
    test(`${label}: ${method} bounds brace and parenthesis nesting`, () => {
      for (const [open, close] of [["{", "}"], ["(", ")"], ["{(", ")}"]]) {
        assert.doesNotThrow(() => braces[method](nested(open, close, 50)));
        assert.throws(() => braces[method](nested(open, close, 101)), {
          name: "SyntaxError",
          message: /exceeds max depth \(100\)/,
        });
        assert.throws(() => braces[method](nested(open, close, 101), { maxDepth: Infinity }), {
          name: "SyntaxError",
          message: /exceeds max depth \(100\)/,
        });
      }
      assert.doesNotThrow(() => braces[method](nested("{", "}", 100)));
      assert.doesNotThrow(() => braces[method](nested("(", ")", 100)));
    });
  }

  for (const method of ["compile", "expand", "stringify"]) {
    test(`${label}: ${method} bounds caller-supplied AST depth`, () => {
      assert.doesNotThrow(() => braces[method](ast(100)));
      assert.throws(() => braces[method](ast(101)), {
        name: "RangeError",
        message: /exceeds max depth \(100\)/,
      });
    });
  }

  test(`${label}: ordinary brace expansion and glob matching remain intact`, () => {
    const micromatch = consumerRequire("micromatch");
    assert.deepEqual(braces.expand("src/{components,styles}/file.{css,scss}"), [
      "src/components/file.css", "src/components/file.scss",
      "src/styles/file.css", "src/styles/file.scss",
    ]);
    assert.deepEqual(braces.expand("file-{01..03}.css"), ["file-01.css", "file-02.css", "file-03.css"]);
    assert.deepEqual(micromatch(["src/a.css", "src/b.ts", "src/nested/c.scss"], "src/**/*.{css,scss}"), [
      "src/a.css", "src/nested/c.scss",
    ]);
    assert.equal(micromatch.isMatch("src/a.css", "src/{a,b}.css"), true);
    assert.equal(micromatch.matcher("src/{a,b}.css")("src/c.css"), false);
  });
}

test("fast-glob discovers the same CSS files with and without brace expansion", () => {
  const plain = glob.sync("src/**/*.css").sort();
  assert.ok(plain.length > 0);
  assert.deepEqual(glob.sync("src/**/*.{css,nonexistent}").sort(), plain);
});

test("fast-glob rejects excessive nesting before recursive expansion", () => {
  assert.throws(() => glob.sync(nested("{", "}", 101)), {
    name: "SyntaxError",
    message: /exceeds max depth \(100\)/,
  });
});
