import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, it } from "vitest";
import { REDACTED_BLOB_SOURCE_PATH } from "./redactionPlaceholders";

it("matches the server's blob source path placeholder", () => {
  const source = readFileSync(join(process.cwd(), "../composer/redaction.py"), "utf8");
  const match = /^REDACTED_BLOB_SOURCE_PATH = "([^"]*)"$/m.exec(source);
  expect(match).not.toBeNull();
  expect(REDACTED_BLOB_SOURCE_PATH).toBe(match?.[1]);
});
