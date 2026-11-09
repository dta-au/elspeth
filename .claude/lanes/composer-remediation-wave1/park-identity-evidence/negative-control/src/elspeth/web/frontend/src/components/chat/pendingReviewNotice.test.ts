import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { PENDING_REVIEW_NOTICE } from "./pendingReviewNotice";

// Vitest runs from the frontend root; the producer is its sibling package.
const PRODUCER = "../composer/no_tool_policy.py";

/** The Python string literal(s) assigned to `name`, concatenated. */
function pythonFinalString(source: string, name: string): string {
  const start = source.indexOf(`${name}: Final = (`);
  if (start === -1) throw new Error(`${name} is not assigned in ${PRODUCER}`);
  const end = source.indexOf("\n)", start);
  if (end === -1) throw new Error(`${name} has no closing parenthesis`);
  const body = source.slice(start, end);
  const literals = [...body.matchAll(/^\s+"((?:[^"\\]|\\.)*)"\s*$/gm)].map((match) => match[1]);
  if (literals.length === 0) throw new Error(`${name} holds no plain string literal`);
  return literals.join("");
}

describe("pending-review notice seam", () => {
  const source = readFileSync(PRODUCER, "utf8");

  it("matches the backend's notice byte for byte", () => {
    expect(pythonFinalString(source, "_INTERPRETATION_REVIEW_HANDOFF_NOTICE")).toBe(PENDING_REVIEW_NOTICE);
  });

  it("the extractor reads the named constant, not any string in the file", () => {
    // Control: a different constant in the same file must NOT equal the notice,
    // and a missing name must throw rather than compare against nothing.
    expect(pythonFinalString(source, "_INTERPRETATION_REVIEW_HANDOFF_FINDINGS_FOOTER")).not.toBe(PENDING_REVIEW_NOTICE);
    expect(() => pythonFinalString(source, "_NO_SUCH_NOTICE")).toThrow(/is not assigned/);
  });
});
