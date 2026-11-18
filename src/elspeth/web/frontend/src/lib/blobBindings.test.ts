import { describe, expect, it } from "vitest";
import { componentOptionsById, inlineContentMarker, optionBlobReferences } from "./blobBindings";
import type { CompositionState } from "@/types/api";

const ID = "F976FD8B-4432-4F8F-BBC3-2D8A9F2114E0";

describe("blob bindings", () => {
  it("accepts widened inline markers and rejects other shapes", () => {
    expect(inlineContentMarker({ blob_ref: ID, mode: "inline_content", sha256: "abc", encoding: "utf-8" })).toEqual({ blobId: ID, sha256: "abc" });
    for (const value of [{ blob_ref: ID, mode: "bind_source", sha256: "abc" }, { blob_ref: ID, mode: "inline_content" }, { blob_ref: 42, mode: "inline_content", sha256: "abc" }, []]) {
      expect(inlineContentMarker(value)).toBeNull();
    }
  });

  it("walks every backend binding key through objects and nested arrays", () => {
    const marker = { blob_ref: ID, mode: "inline_content", sha256: "abc" };
    expect(optionBlobReferences({
      prompt_template: marker,
      blob_ref: ID,
      mode: "bind_source",
      queries: { a: { template: marker } },
      query_list: [{ template: marker }],
      files: { notes: { content: marker } },
      tables: { source: { reference_content: marker } },
      nested: [[{ x_blob_id: ID }]],
      blobs: [{ blob_id: ID }],
      path: `blob:${ID}`,
      file: `blob:${ID}`,
      other: `blob:${ID}`,
      invalid_blob_id: 5,
    })).toEqual([
      { blobId: ID.toLowerCase(), fieldPath: "prompt_template" },
      { blobId: ID.toLowerCase(), fieldPath: "blob_ref" },
      { blobId: ID.toLowerCase(), fieldPath: "queries.a.template" },
      { blobId: ID.toLowerCase(), fieldPath: "query_list.0.template" },
      { blobId: ID.toLowerCase(), fieldPath: "files.notes.content" },
      { blobId: ID.toLowerCase(), fieldPath: "tables.source.reference_content" },
      { blobId: ID.toLowerCase(), fieldPath: "nested.0.0" },
      { blobId: ID.toLowerCase(), fieldPath: "blobs.0" },
      { blobId: ID.toLowerCase(), fieldPath: "path" },
      { blobId: ID.toLowerCase(), fieldPath: "file" },
    ]);
  });

  it("resolves sources, nodes and sinks, and rejects removed ids", () => {
    const state = {
      sources: { source: { options: { a: 1 } }, upload: { options: { b: 2 } } },
      nodes: [{ id: "step", options: { c: 3 } }],
      outputs: [{ name: "out", options: { d: 4 } }],
    } as unknown as CompositionState;
    expect(componentOptionsById(state, "source")).toEqual({ a: 1 });
    expect(componentOptionsById(state, "source:upload")).toEqual({ b: 2 });
    expect(componentOptionsById(state, "step")).toEqual({ c: 3 });
    expect(componentOptionsById(state, "out")).toEqual({ d: 4 });
    expect(componentOptionsById(state, "removed")).toBeNull();
  });
});
