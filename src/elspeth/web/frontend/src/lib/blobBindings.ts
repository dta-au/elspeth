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

export interface PromptQueryEntry {
  /** Path segment the server uses: the list index for list form, the key for mapping form. */
  selector: string;
  /** Reader-facing name: the entry's `name` (list form, `#<index>` if unnamed) or the mapping key. */
  name: string;
  entry: Record<string, unknown>;
}

/**
 * The one authority for a multi-query step's entries. `selector` matches the
 * server's `queries.<selector>.template` binding path
 * (sessions/prompt_file_review.py `_surface_slots`); `name` is for labels only.
 */
export function promptQueryEntries(queries: unknown): PromptQueryEntry[] {
  const isEntry = (value: unknown): value is Record<string, unknown> =>
    typeof value === "object" && value !== null && !Array.isArray(value);
  if (Array.isArray(queries)) {
    return queries.flatMap((entry, index) => isEntry(entry)
      ? [{ selector: String(index), name: typeof entry.name === "string" && entry.name !== "" ? entry.name : `#${index}`, entry }]
      : []);
  }
  if (isEntry(queries)) {
    return Object.entries(queries).flatMap(([key, entry]) => isEntry(entry) ? [{ selector: key, name: key, entry }] : []);
  }
  return [];
}

export function promptQueryTemplatePath(selector: string): string {
  return `queries.${selector}.template`;
}

/** Prompt slots understood by the file editor and the server's prompt binding check. */
export function promptFileSlots(options: Record<string, unknown>): Array<{ fieldPath: string; value: unknown; queryName?: string }> {
  const slots: Array<{ fieldPath: string; value: unknown; queryName?: string }> = ["prompt_template", "system_prompt"]
    .filter((key) => Object.prototype.hasOwnProperty.call(options, key))
    .map((fieldPath) => ({ fieldPath, value: options[fieldPath] }));
  for (const { selector, name, entry } of promptQueryEntries(options.queries)) {
    if (Object.prototype.hasOwnProperty.call(entry, "template")) {
      slots.push({ fieldPath: promptQueryTemplatePath(selector), value: entry.template, queryName: name });
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

/**
 * `opens` says what Edit does for a refused save: `true` opens the editor
 * read-only (Save disabled, `reason` shown), `false` is a file that cannot be
 * shown as prompt text at all (missing, still processing, not text).
 */
export type PromptFileEditability =
  | { editable: true }
  | { editable: false; code: string; reason: string; opens: boolean };

const EDITABLE_MIME_TYPES = new Set(["text/plain", "text/csv", "application/json", "application/x-jsonlines", "application/jsonl", "text/jsonl"]);

/**
 * Display mirror of the save route's refusals (blobs/prompt_text_routes.py);
 * the server remains the save authority. Not mirrored, because only the
 * server can see it: whether the step's last approvals still cover its whole
 * prompt surface (`prompt_file_stale` on save).
 */
export function promptFileEditability({ state, blobs, pendingEvents, nodeId, fieldPath, blobId }: {
  state: CompositionState | null;
  blobs: readonly BlobMetadata[];
  pendingEvents: readonly InterpretationEvent[];
  nodeId: string;
  fieldPath: string;
  blobId: string;
}): PromptFileEditability {
  const refuse = (code: string, reason: string, opens = true): PromptFileEditability => ({ editable: false, code, reason, opens });
  const id = blobId.toLowerCase();
  const blob = blobs.find((item) => item.id.toLowerCase() === id);
  if (!blob) return refuse("prompt_file_not_found", "This file is not in this session.", false);
  if (blob.status !== "ready") return refuse("prompt_file_not_ready", "This file is still being processed.", false);
  if (!EDITABLE_MIME_TYPES.has(blob.mime_type)) return refuse("prompt_file_not_text", "Only UTF-8 text files can be edited here.", false);
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
  if (matches[0]?.encoding !== "utf-8") return refuse("prompt_file_encoding_unsupported", "This prompt reads the file as non-UTF-8 text. Re-attach it as UTF-8 to edit it here.");
  // A step may use several prompt files; each is edited on its own. While the
  // step's review card is pending, though, the card covers every prompt slot
  // and this editor shows one file, so any other slot keeps the save refused.
  if (pendingEvents.some((event) => event.choice === "pending" && event.kind === "llm_prompt_template" && event.affected_node_id === nodeId)) {
    const unseen = promptFileSlots(node.options).filter((slot) => slot.fieldPath !== fieldPath && slot.value !== null && slot.value !== undefined);
    // Another prompt file is reported first, inline text only when no other file is involved.
    if (unseen.some((slot) => typeof slot.value !== "string")) {
      return refuse("prompt_file_review_pending", "Complete the existing node review before editing its files.");
    }
    if (unseen.length > 0) {
      return refuse("prompt_file_review_pending_inline", "Approve the review card first, then edit the file.");
    }
  }
  return { editable: true };
}
