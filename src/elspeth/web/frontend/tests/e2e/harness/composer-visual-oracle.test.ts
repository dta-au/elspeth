import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const visualSpec = readFileSync(
  join(process.cwd(), "tests/e2e/composer-workspace.visual.spec.ts"),
  "utf8",
);

describe("Composer visual regression oracle", () => {
  it("waits for exact terminal validation and audit labels", () => {
    expect(visualSpec).not.toContain("not.toHaveAccessibleName");
    expect(visualSpec).toContain('"Validation: Passed"');
    expect(visualSpec).toContain('"Audit: Ready"');
    expect(visualSpec).toContain('"Validation: 24 errors"');
    expect(visualSpec).toContain('"Audit: 2 issues"');
  });

  it("masks only the fractional edge label instead of accepting broad drift", () => {
    expect(visualSpec).not.toContain("maxDiffPixels");
    expect(visualSpec).toMatch(
      /const fractionalEdgeLabel[^;]*\.react-flow__edge-text[^;]*success/s,
    );
    expect(visualSpec).toContain("mask: [fractionalEdgeLabel]");
  });
});
