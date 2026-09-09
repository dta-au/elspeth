// ============================================================================
// pluginDisplayName — display-name derivation for catalog plugin ids
// (elspeth-5ee1f76e39: display name primary, raw id demoted to metadata).
// ============================================================================

import { describe, expect, it } from "vitest";
import {
  isInternalPlugin,
  pluginDisplayName,
  titleCaseLabel,
} from "./pluginDisplayName";

describe("pluginDisplayName", () => {
  it("uses curated overrides where plain title-casing would mislead", () => {
    expect(pluginDisplayName("azure_blob")).toBe("Azure Blob Storage");
    expect(pluginDisplayName("dataverse")).toBe("Microsoft Dataverse");
    expect(pluginDisplayName("chroma_sink")).toBe("Chroma Vector Store");
    expect(pluginDisplayName("batch_top_k")).toBe("Batch Top-K");
  });

  // "null" no longer needs a curated display name: CatalogDrawer filters
  // internal-machinery plugins out of the catalog (elspeth-06566208b3), and
  // pluginDisplayName has no consumer outside it. isInternalPlugin below is
  // now load-bearing as that filter's predicate.
  // (isInternalPlugin, that filter's predicate, is covered below.)

  it("humanises underscore ids to Title Case", () => {
    expect(pluginDisplayName("azure_document_intelligence")).toBe(
      "Azure Document Intelligence",
    );
    expect(pluginDisplayName("field_mapper")).toBe("Field Mapper");
    expect(pluginDisplayName("web_scrape")).toBe("Web Scrape");
  });

  it("upper-cases known acronyms in the fallback path", () => {
    expect(pluginDisplayName("csv")).toBe("CSV");
    expect(pluginDisplayName("json_explode")).toBe("JSON Explode");
    expect(pluginDisplayName("llm")).toBe("LLM");
    expect(pluginDisplayName("rag_retrieval")).toBe("RAG Retrieval");
  });

  // elspeth-d2de348437: chat/interpretationStepLabel.ts now routes through
  // this module (its local titleCase is deleted); tutorial/TutorialTurn4Run.tsx
  // still carries its own acronym-less caser and should consume titleCaseLabel
  // when its lane lands. This pins the precondition both depend on — the
  // curated set really does carry URL and JSON — so the acronyms cannot be
  // dropped from under that consolidation.
  it("carries the acronyms the other title-casers need", () => {
    expect(pluginDisplayName("url")).toBe("URL");
    expect(pluginDisplayName("url_fetch")).toBe("URL Fetch");
    expect(pluginDisplayName("json_explode")).toBe("JSON Explode");
  });

  it("is not confused by Object.prototype key names", () => {
    // A plugin hypothetically named "constructor" must humanise, not
    // resolve to Object.prototype.constructor.
    expect(pluginDisplayName("constructor")).toBe("Constructor");
  });
});

describe("titleCaseLabel", () => {
  it("upper-cases curated acronyms in free-form labels", () => {
    expect(titleCaseLabel("url")).toBe("URL");
    expect(titleCaseLabel("fetch_url")).toBe("Fetch URL");
    expect(titleCaseLabel("json summary")).toBe("JSON Summary");
  });

  it("never applies curated plugin overrides — they are plugin-id vocabulary, not label vocabulary", () => {
    expect(titleCaseLabel("azure_blob")).toBe("Azure Blob");
    expect(titleCaseLabel("dataverse")).toBe("Dataverse");
  });
});

describe("isInternalPlugin", () => {
  it("flags the resume-only null source", () => {
    expect(isInternalPlugin("null")).toBe(true);
  });

  it("does not flag ordinary plugins", () => {
    expect(isInternalPlugin("csv")).toBe(false);
    expect(isInternalPlugin("azure_blob")).toBe(false);
  });
});
