import { readFileSync } from "node:fs";
import { join } from "node:path";

import ts from "typescript";
import { describe, expect, it } from "vitest";

const visualSpec = readFileSync(
  join(process.cwd(), "tests/e2e/composer-workspace.visual.spec.ts"),
  "utf8",
);

function expectNarrowPopulatedScreenshotMask(source: string): void {
  expect(source).not.toMatch(/maxDiffPixels|maxDiffPixelRatio/);
  expect(source).toMatch(
    /const fractionalEdgeLabel[^;]*\.react-flow__edge-text[^;]*success/s,
  );
  const spec = ts.createSourceFile(
    "composer-workspace.visual.spec.ts",
    source,
    ts.ScriptTarget.Latest,
    true,
  );
  const screenshots: ts.CallExpression[] = [];
  const visit = (node: ts.Node): void => {
    if (
      ts.isCallExpression(node) &&
      ts.isPropertyAccessExpression(node.expression) &&
      node.expression.name.text === "toHaveScreenshot" &&
      node.arguments[0] !== undefined &&
      ts.isTemplateExpression(node.arguments[0]) &&
      node.arguments[0].head.text === "populated-freeform-"
    ) {
      screenshots.push(node);
    }
    ts.forEachChild(node, visit);
  };
  visit(spec);
  expect(screenshots).toHaveLength(1);
  const screenshot = screenshots[0];
  const options = screenshot.arguments[1];
  if (options === undefined || !ts.isObjectLiteralExpression(options)) {
    throw new Error("Populated screenshot requires explicit mask options");
  }
  const properties = options.properties.map((property) => {
    if (!ts.isPropertyAssignment(property) || !ts.isIdentifier(property.name)) {
      throw new Error("Populated screenshot options must be explicit properties");
    }
    return property;
  });
  const masks = properties.filter(
    (property) => property.name.getText(spec) === "mask",
  );
  expect(masks).toHaveLength(1);
  const mask = masks[0].initializer;
  if (!ts.isArrayLiteralExpression(mask)) {
    throw new Error("Populated screenshot requires an explicit mask list");
  }
  expect(mask.elements.map((element) => element.getText(spec))).toEqual([
    "fractionalEdgeLabel",
    "checksumValue",
  ]);

  // The fixture's digest varies, so its glyphs may be masked only after
  // the rendered value has been compared with the current server state.
  let block: ts.Node = screenshot.parent;
  while (!ts.isBlock(block) && block.parent !== undefined) block = block.parent;
  if (!ts.isBlock(block)) {
    throw new Error(
      "Populated screenshot requires a checksum assertion in its block",
    );
  }
  const checksumDeclarations = block.statements
    .filter(ts.isVariableStatement)
    .flatMap((statement) => [...statement.declarationList.declarations])
    .filter((declaration) => declaration.name.getText(spec) === "checksumValue");
  expect(checksumDeclarations).toHaveLength(1);
  const checksum = checksumDeclarations[0];
  expect(checksum.getStart(spec)).toBeLessThan(screenshot.getStart(spec));
  expect(checksum.initializer?.getText(spec)).toMatch(
    /^await assertCurrentChecksum\(page,\s*sessionId\)$/,
  );
}

describe("Composer visual regression oracle", () => {
  it("waits for exact terminal validation and audit labels", () => {
    expect(visualSpec).not.toContain("not.toHaveAccessibleName");
    expect(visualSpec).toContain('"Checks: Ready"');
    expect(visualSpec).toContain('"Checks: 25 issues"');
  });

  it("masks only the fractional edge label and independently checked checksum glyphs", () => {
    expectNarrowPopulatedScreenshotMask(visualSpec);
  });

  it.each([
    [
      "missing fractional edge mask",
      "mask: [fractionalEdgeLabel, checksumValue]",
      "mask: [checksumValue]",
    ],
    [
      "broader graph mask",
      "mask: [fractionalEdgeLabel, checksumValue]",
      'mask: [fractionalEdgeLabel, checksumValue, page.locator(".react-flow")]',
    ],
    [
      "pixel tolerance",
      "mask: [fractionalEdgeLabel, checksumValue]",
      "maxDiffPixels: 1000, mask: [fractionalEdgeLabel, checksumValue]",
    ],
    [
      "ratio tolerance",
      "mask: [fractionalEdgeLabel, checksumValue]",
      "maxDiffPixelRatio: 0.1, mask: [fractionalEdgeLabel, checksumValue]",
    ],
    [
      "unchecked checksum",
      "const checksumValue = await assertCurrentChecksum(page, sessionId);",
      'const checksumValue = page.locator(".graph-checksum-value");',
    ],
  ])("rejects %s", (_name, original, replacement) => {
    expect(visualSpec).toContain(original);
    const mutated = visualSpec.replace(original, replacement);
    expect(mutated).not.toBe(visualSpec);
    expect(() => expectNarrowPopulatedScreenshotMask(mutated)).toThrow();
  });
});
