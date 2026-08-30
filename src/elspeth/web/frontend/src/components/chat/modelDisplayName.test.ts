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

  it("returns a non-wordish leaf RAW rather than dressing it up as a name", () => {
    // RED before the guard: this yielded "Anthropic.claude 3 Haiku 20240307
    // V1:0" — the dot survived and a date stamp and version suffix were
    // title-cased as if they were words. Neither clean prose nor a recoverable
    // id, and it reached the run-confirm consent dialog, where a garbled model
    // name undermines exactly the trust the phrasing is there to build.
    // An honest identifier beats a fake name — the same ruling
    // diagnosticPhrases.ts makes for an unknown enum.
    expect(modelDisplayName("bedrock/anthropic.claude-3-haiku-20240307-v1:0")).toBe(
      "anthropic.claude-3-haiku-20240307-v1:0",
    );
  });

  it("phrases a wordish leaf even when it carries a version number", () => {
    // The guard must not swallow the common case: `4.6` is a digit-adjacent
    // dot, not a letter-adjacent one, so the OpenRouter form still phrases.
    // This is the boundary the three predicates draw, pinned from both sides.
    expect(modelDisplayName("openrouter/anthropic/claude-sonnet-4.6")).toBe("Claude Sonnet 4.6");
    expect(modelDisplayName("azure/gpt-4o")).toBe("GPT 4o");
  });

  it("returns raw on each predicate independently", () => {
    // One case per arm, so a dropped predicate fails here rather than
    // surviving because a sibling arm happened to catch the same fixture.
    expect(modelDisplayName("vendor/foo.bar")).toBe("foo.bar"); // letter-adjacent dot
    expect(modelDisplayName("vendor/model:v1")).toBe("model:v1"); // colon
    expect(modelDisplayName("vendor/model-20240307")).toBe("model-20240307"); // 6+ digit run
  });
});
