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

  it("re-joins a hyphen-mangled trailing version number with dots", () => {
    // "anthropic/claude-opus-4-7" rendered as "Claude Opus 4 7" -- the
    // version's own hyphens read as ordinary word breaks. Two or more
    // trailing whole-digit segments are a version, not words.
    expect(modelDisplayName("anthropic/claude-opus-4-7")).toBe("Claude Opus 4.7");
    // Boundary from the version side: a two-digit part without a leading zero
    // is still a version part, and three parts still re-join.
    expect(modelDisplayName("vendor/model-1-10")).toBe("Model 1.10");
    expect(modelDisplayName("vendor/model-3-5-1")).toBe("Model 3.5.1");
  });

  it("returns a hyphenated date stamp RAW instead of dotting it into a fake version", () => {
    // RED before the version-shape test: the trailing-digit re-join dotted
    // every date stamp, so the run-confirm consent text read "GPT 4o
    // 2024.08.06" and "GPT 4.0613" -- a date dressed up as a version number.
    // A part of three or more digits (a year, an MMDD stamp) or a two-digit
    // part with a leading zero (an MM or DD field) is a date, not a version,
    // and an honest identifier beats a fake name.
    expect(modelDisplayName("gpt-4o-2024-08-06")).toBe("gpt-4o-2024-08-06");
    expect(modelDisplayName("openai/gpt-4-0613")).toBe("gpt-4-0613");
    expect(modelDisplayName("o1-2024-12-17")).toBe("o1-2024-12-17");
    expect(modelDisplayName("azure/gpt-4.1-2025-04-14")).toBe("gpt-4.1-2025-04-14");
    expect(modelDisplayName("gemini/gemini-2.5-flash-lite-preview-06-17")).toBe(
      "gemini-2.5-flash-lite-preview-06-17",
    );
    // Length arm, pinned on its own: every part here is version-shaped, so
    // only the two-or-three-part cap keeps four parts from reading "1.2.3.4".
    expect(modelDisplayName("vendor/model-1-2-3-4")).toBe("model-1-2-3-4");
  });

  it("leaves an id that already carries its own dotted version untouched", () => {
    // "4.6" is ONE hyphen segment, not two, so the whole-digit trailing-run
    // test never fires here -- this is the same fixture as above, pinned
    // from the other side so a broadened digit test cannot re-split it.
    expect(modelDisplayName("openrouter/anthropic/claude-sonnet-4.6")).toBe("Claude Sonnet 4.6");
  });

  it("returns raw on each predicate independently", () => {
    // One case per arm, so a dropped predicate fails here rather than
    // surviving because a sibling arm happened to catch the same fixture.
    expect(modelDisplayName("vendor/foo.bar")).toBe("foo.bar"); // letter-adjacent dot
    expect(modelDisplayName("vendor/model:v1")).toBe("model:v1"); // colon
    expect(modelDisplayName("vendor/model-20240307")).toBe("model-20240307"); // 6+ digit run
  });
});
