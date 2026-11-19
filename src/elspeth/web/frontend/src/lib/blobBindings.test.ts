import { describe, expect, it } from "vitest";
import { componentOptionsById, inlineContentMarker, optionBlobReferences, promptFileEditability } from "./blobBindings";
import type { BlobMetadata, CompositionState } from "@/types/api";
import type { InterpretationEvent } from "@/types/interpretation";

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

describe("prompt file editability display mirror", () => {
  const marker = { blob_ref: "file", mode: "inline_content", sha256: "hash", encoding: "utf-8" };
  const blob = { id: "file", status: "ready", mime_type: "text/plain" } as BlobMetadata;
  const stateFor = (options: Record<string, unknown>, otherNodes: Array<{ id: string; plugin: string; options: Record<string, unknown> }> = []) => ({
    sources: {}, outputs: [], nodes: [{ id: "llm", plugin: "llm", options }, ...otherNodes],
  }) as unknown as CompositionState;
  const check = (overrides: { state?: CompositionState; blobs?: BlobMetadata[]; pendingEvents?: InterpretationEvent[] } = {}) =>
    promptFileEditability({ state: overrides.state ?? stateFor({ prompt_template: marker }), blobs: overrides.blobs ?? [blob],
      pendingEvents: overrides.pendingEvents ?? [], nodeId: "llm", fieldPath: "prompt_template", blobId: "file" });

  it("allows one ready UTF-8 prompt file, including an unrelated pending card", () => {
    expect(check()).toEqual({ editable: true });
    expect(check({ pendingEvents: [{ choice: "pending", kind: "llm_prompt_template", affected_node_id: "other" } as InterpretationEvent] })).toEqual({ editable: true });
    expect(check({ state: stateFor({ prompt_template: marker }), pendingEvents: [{ choice: "pending", kind: "llm_prompt_template", affected_node_id: "llm" } as InterpretationEvent] })).toEqual({ editable: true });
    expect(check({ pendingEvents: [{ choice: "amended", kind: "llm_prompt_template", affected_node_id: "llm" } as InterpretationEvent] })).toEqual({ editable: true });
  });

  it.each([
    ["prompt_file_not_found", { blobs: [] }],
    ["prompt_file_not_ready", { blobs: [{ ...blob, status: "pending" as const }] }],
    ["prompt_file_not_text", { blobs: [{ ...blob, mime_type: "application/pdf" }] }],
    ["prompt_file_mixed_use", { state: { ...stateFor({ prompt_template: marker }), sources: { input: { plugin: "csv", options: { blob_ref: "file" } } } as CompositionState["sources"] } }],
    ["prompt_file_mixed_use", { state: stateFor({ prompt_template: marker, queries: { one: { reference_blob_id: "file" } } }) }],
    ["prompt_file_shared_binding", { state: stateFor({ prompt_template: marker }, [{ id: "llm-2", plugin: "llm", options: { prompt_template: marker } }]) }],
    ["prompt_file_shared_binding", { state: stateFor({ prompt_template: marker, system_prompt: marker }) }],
    ["prompt_file_encoding_unsupported", { state: stateFor({ prompt_template: { ...marker, encoding: "latin-1" } }) }],
    ["prompt_file_review_pending_inline", { state: stateFor({ prompt_template: marker, system_prompt: "You are helpful." }), pendingEvents: [{ choice: "pending", kind: "llm_prompt_template", affected_node_id: "llm" } as InterpretationEvent] }],
  ])("returns %s for a pre-checkable refusal", (code, overrides) => {
    expect(check(overrides)).toMatchObject({ editable: false, code });
  });

  const pendingCard = { choice: "pending", kind: "llm_prompt_template", affected_node_id: "llm" } as InterpretationEvent;
  const otherFile = { ...marker, blob_ref: "other" };

  it.each([
    ["the system prompt", { prompt_template: marker, system_prompt: otherFile }],
    ["a mapping-form query", { prompt_template: marker, queries: { one: { template: otherFile } } }],
    ["a list-form query", { prompt_template: marker, queries: [{ name: "one", template: otherFile }] }],
  ])("allows a file on a step whose other prompt file backs %s", (_slot, options) => {
    expect(check({ state: stateFor(options) })).toEqual({ editable: true });
  });

  it.each([
    ["prompt_file_review_pending", { prompt_template: marker, system_prompt: otherFile }],
    ["prompt_file_review_pending", { prompt_template: marker, queries: [{ name: "one", template: otherFile }] }],
    // Inline text beside another file: the other-file refusal is reported, as the server reports it.
    ["prompt_file_review_pending", { prompt_template: marker, system_prompt: "You are helpful.", queries: { one: { template: otherFile } } }],
    ["prompt_file_review_pending_inline", { prompt_template: marker, system_prompt: "You are helpful." }],
  ])("returns %s while the step's card is pending over content this editor does not show", (code, options) => {
    expect(check({ state: stateFor(options), pendingEvents: [pendingCard] })).toMatchObject({ editable: false, code, opens: true });
    // The same step with no card pending is editable.
    expect(check({ state: stateFor(options) })).toEqual({ editable: true });
  });

  it("does not count an unset prompt slot as content the pending card covers", () => {
    expect(check({ state: stateFor({ prompt_template: marker, system_prompt: null }), pendingEvents: [pendingCard] })).toEqual({ editable: true });
  });

  it.each([
    ["prompt_file_not_found", false, { blobs: [] }],
    ["prompt_file_not_ready", false, { blobs: [{ ...blob, status: "pending" as const }] }],
    ["prompt_file_not_text", false, { blobs: [{ ...blob, mime_type: "application/pdf" }] }],
    ["prompt_file_mixed_use", true, { state: stateFor({ prompt_template: marker, queries: { one: { reference_blob_id: "file" } } }) }],
    ["prompt_file_shared_binding", true, { state: stateFor({ prompt_template: marker, system_prompt: marker }) }],
    ["prompt_file_encoding_unsupported", true, { state: stateFor({ prompt_template: { ...marker, encoding: "latin-1" } }) }],
  ])("%s opens the editor read-only: %s", (code, opens, overrides) => {
    expect(check(overrides)).toMatchObject({ editable: false, code, opens });
  });
});
