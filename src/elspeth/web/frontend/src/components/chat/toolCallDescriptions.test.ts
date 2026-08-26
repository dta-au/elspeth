import { describe, expect, it } from "vitest";

import {
  MUTATING_TOOL_CALL_NAMES,
  READ_ONLY_TOOL_CALL_NAMES,
  TOOL_CALL_DESCRIPTIONS,
  describeToolCall,
  isReadOnlyToolCall,
  liveToolCallLabel,
  toolCallOutcomeLabel,
} from "./toolCallDescriptions";

// ── Membership oracle for the read-only / mutating split (TQ-5) ─────────────
//
// The module's own header calls the split load-bearing: liveToolCallLabel
// derives its no-stamp prefix from which half a name lives in, so a mid-flight
// mutation is never mislabelled as a lookup. Until this block existed nothing
// asserted the membership — the map was module-private, and the surrounding
// tests only exercise names OUTSIDE it, which passes for any name outside the
// map INCLUDING one wrongly added to the read-only half. Add `set_pipeline`
// to the read-only map and "Looked up: set_pipeline" ships green: exactly the
// fabrication the module's doctrine forbids, and the circular-gate shape
// where the data satisfying the check is the data it validates.
describe("read-only / mutating membership", () => {
  const EXPECTED_READ_ONLY = [
    "list_sources",
    "list_transforms",
    "list_sinks",
    "list_models",
    "list_sessions",
    "get_plugin_schema",
    "get_plugin_assistance",
    "get_pipeline_state",
    "get_audit_info",
    "get_expression_grammar",
    "diff_pipeline",
    "preview_pipeline",
    "explain_validation_error",
    "generate_yaml",
  ] as const;

  it("pins the exact read-only membership", () => {
    // Exact, order-insensitive: a name ADDED to or REMOVED from the read-only
    // half fails here rather than silently changing what "Looked up" claims.
    expect([...READ_ONLY_TOOL_CALL_NAMES].sort()).toEqual(
      [...EXPECTED_READ_ONLY].sort(),
    );
  });

  it("keeps the two halves disjoint", () => {
    const mutating = new Set(MUTATING_TOOL_CALL_NAMES);
    for (const name of READ_ONLY_TOOL_CALL_NAMES) {
      expect(mutating.has(name)).toBe(false);
    }
    // Neither half may be silently emptied into the other.
    expect(READ_ONLY_TOOL_CALL_NAMES.length).toBeGreaterThan(0);
    expect(MUTATING_TOOL_CALL_NAMES.length).toBeGreaterThan(0);
    expect(Object.keys(TOOL_CALL_DESCRIPTIONS)).toHaveLength(
      READ_ONLY_TOOL_CALL_NAMES.length + MUTATING_TOOL_CALL_NAMES.length,
    );
  });

  it("admits no mutating verb prefix into the read-only half", () => {
    // A structural check independent of the list above: it catches a NEW
    // mutating tool misfiled on the read-only side even if someone updates
    // EXPECTED_READ_ONLY to match their mistake.
    const MUTATING_VERBS =
      /^(set|upsert|patch|remove|delete|splice|apply|clear|create|update|wire)_/;
    for (const name of READ_ONLY_TOOL_CALL_NAMES) {
      expect(name).not.toMatch(MUTATING_VERBS);
    }
  });

  it("is the predicate the label functions actually use", () => {
    // Guards against pinning a function production ignores: every read-only
    // name must render the lookup label, and no mutating name may.
    for (const name of READ_ONLY_TOOL_CALL_NAMES) {
      expect(isReadOnlyToolCall(name)).toBe(true);
      expect(liveToolCallLabel(name, undefined)).toBe(`Looked up: ${name}`);
    }
    for (const name of MUTATING_TOOL_CALL_NAMES) {
      expect(isReadOnlyToolCall(name)).toBe(false);
      expect(liveToolCallLabel(name, undefined)).toBe(`Running: ${name}`);
    }
    // Unknown names are not read-only: absence of evidence is not evidence.
    expect(isReadOnlyToolCall("definitely_not_a_tool")).toBe(false);
  });
});

describe("toolCallOutcomeLabel", () => {
  it("maps each stamped outcome to the ribbon vocabulary", () => {
    expect(toolCallOutcomeLabel("upsert_node", "applied")).toBe(
      "Applied: upsert_node",
    );
    expect(toolCallOutcomeLabel("upsert_node", "rejected")).toBe(
      "Attempted: upsert_node (not applied)",
    );
    expect(toolCallOutcomeLabel("upsert_node", "failed")).toBe(
      "Failed: upsert_node",
    );
    expect(toolCallOutcomeLabel("upsert_node", "cancelled")).toBe(
      "Cancelled: upsert_node",
    );
  });

  it("keeps the lookup label for read-only names, stamped 'completed' or unstamped", () => {
    expect(toolCallOutcomeLabel("get_pipeline_state", undefined)).toBe(
      "Looked up: get_pipeline_state",
    );
    expect(toolCallOutcomeLabel("get_pipeline_state", "completed")).toBe(
      "Looked up: get_pipeline_state",
    );
  });

  it("never labels a completed durable mutation as a lookup — blob-store and interpretation writes render Completed", () => {
    // These tools succeed WITHOUT creating a composition-state version, so
    // the server stamps them "completed", not "applied". They are durable
    // writes and must not read as lookups.
    for (const name of [
      "create_blob",
      "update_blob",
      "delete_blob",
      "wire_blob_inline_ref",
      "request_interpretation_review",
    ]) {
      expect(toolCallOutcomeLabel(name, "completed")).toBe(
        `Completed: ${name}`,
      );
    }
  });

  it("labels a completed stamp on a known mutating tool as Completed, not Looked up", () => {
    expect(toolCallOutcomeLabel("set_pipeline", "completed")).toBe(
      "Completed: set_pipeline",
    );
  });

  it("never claims completion for an unstamped non-read-only name post-turn — Ran, not Completed or Looked up", () => {
    // No stamp = no server evidence the call finished successfully.
    // "Completed" would fabricate success; "Looked up" would fabricate a
    // read. "Ran" claims dispatch only.
    for (const name of ["create_blob", "set_pipeline", "mystery_tool"]) {
      const label = toolCallOutcomeLabel(name, undefined);
      expect(label).toBe(`Ran: ${name}`);
      expect(label).not.toContain("Completed");
      expect(label).not.toContain("Looked up");
    }
  });
});

describe("liveToolCallLabel", () => {
  it("reuses the stamped-outcome vocabulary verbatim", () => {
    expect(liveToolCallLabel("set_source", "applied")).toBe(
      "Applied: set_source",
    );
    expect(liveToolCallLabel("set_source", "failed")).toBe(
      "Failed: set_source",
    );
    expect(liveToolCallLabel("preview_pipeline", "completed")).toBe(
      "Looked up: preview_pipeline",
    );
    // A completed durable mutation is Completed in the live log too.
    expect(liveToolCallLabel("create_blob", "completed")).toBe(
      "Completed: create_blob",
    );
  });

  it("labels an unstamped read-only lookup conservatively as Looked up", () => {
    expect(liveToolCallLabel("get_plugin_schema", undefined)).toBe(
      "Looked up: get_plugin_schema",
    );
    expect(liveToolCallLabel("list_transforms", undefined)).toBe(
      "Looked up: list_transforms",
    );
  });

  it("never claims Applied — or Looked up — for an unstamped mutating call", () => {
    expect(liveToolCallLabel("set_pipeline", undefined)).toBe(
      "Running: set_pipeline",
    );
    expect(liveToolCallLabel("remove_node", undefined)).toBe(
      "Running: remove_node",
    );
  });

  it("treats an unknown tool name as unclassified, not a lookup", () => {
    expect(liveToolCallLabel("mystery_tool", undefined)).toBe(
      "Running: mystery_tool",
    );
  });
});

describe("describeToolCall", () => {
  it("keeps the merged catalog covering both halves of the read-only/mutating split", () => {
    expect(TOOL_CALL_DESCRIPTIONS.get_plugin_schema).toBe(
      "Reads a plugin's configuration schema to understand its options.",
    );
    expect(TOOL_CALL_DESCRIPTIONS.set_pipeline).toBe(
      "Replaces the entire pipeline configuration in a single operation.",
    );
  });

  it("falls back to a generic description for unknown names", () => {
    expect(describeToolCall("mystery_tool")).toBe("Composer tool call.");
  });
});
