import { describe, expect, it } from "vitest";
import { modelDisplayName } from "./modelDisplayName";

describe("modelDisplayName", () => {
  it("takes the leaf of a provider path and title-cases hyphenated words", () => {
    expect(modelDisplayName("openrouter/anthropic/claude-sonnet-4.6")).toBe("Claude Sonnet 4.6");
    expect(modelDisplayName("anthropic/claude-sonnet-5")).toBe("Claude Sonnet 5");
  });
  it("upper-cases GPT through the shared acronym set", () => {
    expect(modelDisplayName("gpt-5.5")).toBe("GPT 5.5");
  });
  it("returns a bare id unchanged apart from casing", () => {
    expect(modelDisplayName("sonnet")).toBe("Sonnet");
  });
});
