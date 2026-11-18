import type { CompositionState } from "@/types/api";
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
