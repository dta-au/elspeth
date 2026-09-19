import { describe, expect, it } from "vitest";

import { applySuggestionPrompt } from "./suggestionPrompts";

describe("applySuggestionPrompt", () => {
  it("builds the exact canonical Apply prompt the side rail has always sent", () => {
    // Pinned byte-for-byte (a58479c19): SideRailValidationBanner's test
    // asserts this string, and the decision panel must send the SAME prompt
    // so the two surfaces are one path into the planner, not two.
    expect(
      applySuggestionPrompt({
        component: "csv_source",
        message: "Consider increasing batch size",
        severity: "low",
      }),
    ).toBe(
      "Please apply this suggestion to the pipeline:\n\n**csv_source:** Consider increasing batch size",
    );
  });
});
