import type { BlobMetadata, CompositionState } from "@/types/api";
import type { InterpretationEvent } from "@/types/interpretation";
import { sourceComponentId } from "@/utils/compositionState";

export function inlineContentMarker(value: unknown): { blobId: string; sha256: string } | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const marker = value as Record<string, unknown>;
  if (marker.mode !== "inline_content" || typeof marker.blob_ref !== "string" || typeof marker.sha256 !== "string") return null;
  return { blobId: marker.blob_ref, sha256: marker.sha256 };
}

/** Mirrors the bound-blob retention vocabulary in composer/tools/blobs.py. */
export function optionBlobReferences(options: Record<string, unknown>): Array<{ blobId: string; fieldPath: string }> {
  const references: Array<{ blobId: string; fieldPath: string }> = [];
  const walk = (value: unknown, path: string): void => {
    if (Array.isArray(value)) {
      value.forEach((item, index) => walk(item, path ? `${path}.${index}` : String(index)));
      return;
    }
    if (typeof value !== "object" || value === null) return;
    for (const [key, child] of Object.entries(value)) {
      const fieldPath = path ? `${path}.${key}` : key;
      if (typeof child === "string") {
        if (key === "blob_ref" || key === "blob_id" || key.endsWith("_blob_id")) {
          references.push({ blobId: child.toLowerCase(), fieldPath: path || key });
        } else if ((key === "path" || key === "file") && child.startsWith("blob:")) {
          references.push({ blobId: child.slice(5).toLowerCase(), fieldPath });
        }
      } else {
        walk(child, fieldPath);
      }
    }
  };
  walk(options, "");
  return references;
}

/** Resolve the same source/node/output ids used by the graph inspector. */
export function componentOptionsById(state: CompositionState | null, id: string | null): Record<string, unknown> | null {
  if (state === null || id === null) return null;
  const source = Object.entries(state.sources).find(([name]) => sourceComponentId(name) === id);
  if (source) return source[1].options;
  const node = state.nodes.find((candidate) => candidate.id === id);
  if (node) return node.options;
  return state.outputs.find((candidate) => candidate.name === id)?.options ?? null;
}

export interface PromptFileBinding { nodeId: string; fieldPath: string; blobId: string; encoding: unknown }

/** Prompt slots understood by the file editor and the server's prompt binding check. */
export function promptFileSlots(options: Record<string, unknown>): Array<{ fieldPath: string; value: unknown }> {
  const slots = ["prompt_template", "system_prompt"]
    .filter((key) => Object.prototype.hasOwnProperty.call(options, key))
    .map((fieldPath) => ({ fieldPath, value: options[fieldPath] }));
  const queries = options.queries;
  const entries = Array.isArray(queries)
    ? queries.map((query, index) => [String(index), query] as const)
    : typeof queries === "object" && queries !== null ? Object.entries(queries) : [];
  for (const [name, query] of entries) {
    if (typeof query === "object" && query !== null && !Array.isArray(query) && Object.prototype.hasOwnProperty.call(query, "template")) {
      slots.push({ fieldPath: `queries.${name}.template`, value: (query as Record<string, unknown>).template });
    }
  }
  return slots;
}

export function promptFileBindings(state: CompositionState | null): PromptFileBinding[] {
  if (state === null) return [];
  return state.nodes.flatMap((node) => node.plugin === "llm"
    ? promptFileSlots(node.options).flatMap(({ fieldPath, value }) => {
      if (typeof value !== "object" || value === null || Array.isArray(value)) return [];
      const marker = value as Record<string, unknown>;
      if (marker.mode !== "inline_content" || typeof marker.blob_ref !== "string") return [];
      return [{ nodeId: node.id, fieldPath, blobId: marker.blob_ref.toLowerCase(), encoding: marker.encoding ?? "utf-8" }];
    }) : []);
}

export type PromptFileEditability =
  | { editable: true }
  | { editable: false; code: string; reason: string };

const EDITABLE_MIME_TYPES = new Set(["text/plain", "text/csv", "application/json", "application/x-jsonlines", "application/jsonl", "text/jsonl"]);

/** Display mirror only. The server remains the save authority. */
export function promptFileEditability({ state, blobs, pendingEvents, nodeId, fieldPath, blobId }: {
  state: CompositionState | null;
  blobs: readonly BlobMetadata[];
  pendingEvents: readonly InterpretationEvent[];
  nodeId: string;
  fieldPath: string;
  blobId: string;
}): PromptFileEditability {
  const refuse = (code: string, reason: string): PromptFileEditability => ({ editable: false, code, reason });
  const id = blobId.toLowerCase();
  const blob = blobs.find((item) => item.id.toLowerCase() === id);
  if (!blob) return refuse("prompt_file_not_found", "This file is not in this session.");
  if (blob.status !== "ready") return refuse("prompt_file_not_ready", "This file is still being processed.");
  if (!EDITABLE_MIME_TYPES.has(blob.mime_type)) return refuse("prompt_file_not_text", "Only UTF-8 text files can be edited here.");
  if (state === null) return { editable: true };
  const bindings = promptFileBindings(state);
  const promptReferences = new Set(bindings.map((binding) => `${binding.nodeId}:${binding.fieldPath}:${binding.blobId}`));
  const mixed = Object.entries(state.sources).some(([, source]) => optionBlobReferences(source.options).some((ref) => ref.blobId === id))
    || state.outputs.some((output) => optionBlobReferences(output.options).some((ref) => ref.blobId === id))
    || state.nodes.some((node) => optionBlobReferences(node.options).some((ref) =>
      ref.blobId === id && !promptReferences.has(`${node.id}:${ref.fieldPath}:${id}`)));
  if (mixed) return refuse("prompt_file_mixed_use", "This file is also used outside a prompt. Ask the assistant to give this prompt its own file.");
  const matches = bindings.filter((binding) => binding.blobId === id);
  if (matches.length > 1) return refuse("prompt_file_shared_binding", "This file backs more than one prompt. Give each prompt its own file before editing it here.");
  const node = state.nodes.find((candidate) => candidate.id === nodeId);
  if (!node || !matches.some((binding) => binding.nodeId === nodeId && binding.fieldPath === fieldPath)) {
    return refuse("prompt_file_unbound", "This prompt file is no longer bound here. Reload the session.");
  }
  if (bindings.filter((binding) => binding.nodeId === nodeId).length > 1) {
    return refuse("prompt_file_node_multi_binding", "This node uses more than one prompt file. Re-point it through the agent and approve the review card.");
  }
  if (matches[0]?.encoding !== "utf-8") return refuse("prompt_file_encoding_unsupported", "This prompt reads the file as non-UTF-8 text. Re-attach it as UTF-8 to edit it here.");
  if (pendingEvents.some((event) => event.choice === "pending" && event.kind === "llm_prompt_template" && event.affected_node_id === nodeId)
    && promptFileSlots(node.options).some((slot) => slot.fieldPath !== fieldPath && typeof slot.value === "string")) {
    return refuse("prompt_file_review_pending_inline", "Approve the review card first, then edit the file.");
  }
  return { editable: true };
}
