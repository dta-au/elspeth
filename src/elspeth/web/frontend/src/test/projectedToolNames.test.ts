import { execFileSync } from "node:child_process";
import { expect, it } from "vitest";

it("validates the TypeScript projector ownership bridge", () => {
  expect(() =>
    execFileSync(process.execPath, ["--test", "scripts/projected-tool-names.test.mjs"], {
      encoding: "utf8",
    }),
  ).not.toThrow();
});
