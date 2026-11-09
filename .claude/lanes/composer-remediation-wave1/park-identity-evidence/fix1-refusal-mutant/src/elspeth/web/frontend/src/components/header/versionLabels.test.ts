import { describe, it, expect } from "vitest";
import type {
  ChatMessage,
  CompositionStateVersion,
  NodeSpec,
  ToolCall,
} from "@/types/index";
import {
  appliedToolCallName,
  deriveVersionLabel,
  versionOperationIdentifier,
  versionLabelKind,
  isSnapshotOnly,
} from "./versionLabels";

function makeNode(
  id: string,
  options: Record<string, unknown> = {},
): NodeSpec {
  return {
    id,
    node_type: "transform",
    plugin: "field_mapper",
    input: "source",
    on_success: null,
    on_error: null,
    options,
  };
}

function makeVersion(
  overrides: Partial<CompositionStateVersion> & {
    id: string;
    version: number;
  },
): CompositionStateVersion {
  return {
    created_at: "2026-08-13T10:00:00Z",
    sources: { main: { plugin: "csv_source", options: { path: "in.csv" } } },
    nodes: [makeNode("n1", { fields: ["a", "b"] })],
    edges: [],
    outputs: [],
    metadata: { name: "pipeline", description: null },
    ...overrides,
  };
}

function makeMessage(toolCalls: Partial<ToolCall>[]): ChatMessage {
  return {
    id: "msg-1",
    session_id: "sess-1",
    role: "assistant",
    content: "",
    tool_calls: toolCalls.map(
      (call, index) =>
        ({
          id: `call-${index}`,
          type: "function",
          function: { name: "upsert_edge", arguments: "{}" },
          ...call,
        }) as ToolCall,
    ),
    created_at: "2026-08-13T10:00:00Z",
  };
}

describe("appliedToolCallName", () => {
  it("joins an applied stamp to its version", () => {
    const messages = [
      makeMessage([
        {
          function: { name: "upsert_edge", arguments: "{}" },
          outcome: "applied",
          applied_state_version: 3,
        },
      ]),
    ];
    expect(
      appliedToolCallName(makeVersion({ id: "st-3", version: 3 }), messages),
    ).toBe("upsert_edge");
  });

  it("never labels from a rejected call", () => {
    const messages = [
      makeMessage([
        {
          function: { name: "set_pipeline", arguments: "{}" },
          outcome: "rejected",
          applied_state_version: 3,
        },
      ]),
    ];
    expect(
      appliedToolCallName(makeVersion({ id: "st-3", version: 3 }), messages),
    ).toBeNull();
  });

  it("never labels from an unstamped call", () => {
    const messages = [
      makeMessage([
        { function: { name: "set_pipeline", arguments: "{}" } },
      ]),
    ];
    expect(
      appliedToolCallName(makeVersion({ id: "st-3", version: 3 }), messages),
    ).toBeNull();
  });

  it("ignores stamps for other versions and null tool_calls", () => {
    const messages: ChatMessage[] = [
      { ...makeMessage([]), tool_calls: null },
      makeMessage([
        {
          function: { name: "set_source", arguments: "{}" },
          outcome: "applied",
          applied_state_version: 2,
        },
      ]),
    ];
    expect(
      appliedToolCallName(makeVersion({ id: "st-3", version: 3 }), messages),
    ).toBeNull();
  });
});

describe("versionOperationIdentifier", () => {
  it("returns the raw applied-tool identifier, for the operator title", () => {
    const messages = [
      makeMessage([
        {
          function: { name: "set_pipeline", arguments: "{}" },
          outcome: "applied",
          applied_state_version: 2,
        },
      ]),
    ];
    expect(
      versionOperationIdentifier(makeVersion({ id: "st-2", version: 2 }), messages),
    ).toBe("Applied: set_pipeline");
  });

  it("returns the raw name for an unknown tool, and null for an unlabeled version", () => {
    const messages = [
      makeMessage([
        {
          function: { name: "not_a_real_tool", arguments: "{}" },
          outcome: "applied",
          applied_state_version: 2,
        },
      ]),
    ];
    expect(
      versionOperationIdentifier(makeVersion({ id: "st-2", version: 2 }), messages),
    ).toBe("Applied: not_a_real_tool");
    expect(
      versionOperationIdentifier(makeVersion({ id: "st-3", version: 3 }), []),
    ).toBeNull();
  });
});

describe("deriveVersionLabel", () => {
  it("labels an applied tool call", () => {
    const version = makeVersion({ id: "st-3", version: 3 });
    const messages = [
      makeMessage([
        {
          function: { name: "upsert_edge", arguments: "{}" },
          outcome: "applied",
          applied_state_version: 3,
        },
      ]),
    ];
    expect(deriveVersionLabel(version, [version], messages)).toBe(
      "Applied: Adds or replaces a connection between two nodes in the pipeline.",
    );
  });

  it("labels a revert whose lineage target is older than the adjacent predecessor", () => {
    const v2 = makeVersion({ id: "st-2", version: 2 });
    const v3 = makeVersion({ id: "st-3", version: 3 });
    const v4 = makeVersion({
      id: "st-4",
      version: 4,
      derived_from_state_id: "st-2",
    });
    expect(deriveVersionLabel(v4, [v2, v3, v4], [])).toBe("Reverted to v2");
  });

  it("does not read adjacent lineage as a revert (ordinary persists derive from the previous row)", () => {
    const v2 = makeVersion({ id: "st-2", version: 2 });
    const v3 = makeVersion({
      id: "st-3",
      version: 3,
      derived_from_state_id: "st-2",
    });
    expect(deriveVersionLabel(v3, [v2, v3], [])).toBe("Edited");
  });

  it("prefers the applied stamp — a direct server-authenticated statement — over revert-shaped lineage, which is only inferred", () => {
    const v2 = makeVersion({ id: "st-2", version: 2 });
    const v5 = makeVersion({
      id: "st-5",
      version: 5,
      derived_from_state_id: "st-2",
    });
    const messages = [
      makeMessage([
        {
          function: { name: "patch_node_options", arguments: "{}" },
          outcome: "applied",
          applied_state_version: 5,
        },
      ]),
    ];
    expect(deriveVersionLabel(v5, [v2, v5], messages)).toBe(
      "Applied: Updates configuration options on a pipeline node.",
    );
  });

  it("falls back without crashing when the lineage target is outside the fetched window", () => {
    const v4 = makeVersion({
      id: "st-4",
      version: 4,
      derived_from_state_id: "st-outside-window",
    });
    expect(deriveVersionLabel(v4, [v4], [])).toBe("Edited");
  });

  it("labels version 1 as the session seed", () => {
    const v1 = makeVersion({ id: "st-1", version: 1 });
    expect(deriveVersionLabel(v1, [v1], [])).toBe("Session created");
  });
});

// The structural discriminant the history tree groups on (elspeth-c8a402a9a4).
// Grouping must key on THIS, not on deriveVersionLabel's string: Wave 3's
// register batch rewrites the word "Edited", and a copy edit must not be able
// to turn grouping off (or silently regroup the list).
describe("versionLabelKind", () => {
  it("reports 'applied' for a version carrying an applied tool-call stamp", () => {
    const version = makeVersion({ id: "st-3", version: 3 });
    const messages = [
      makeMessage([
        {
          function: { name: "upsert_edge", arguments: "{}" },
          outcome: "applied",
          applied_state_version: 3,
        },
      ]),
    ];
    expect(versionLabelKind(version, [version], messages)).toBe("applied");
  });

  it("reports 'revert' when lineage points strictly older than the adjacent predecessor", () => {
    const v2 = makeVersion({ id: "st-2", version: 2 });
    const v3 = makeVersion({ id: "st-3", version: 3 });
    const v4 = makeVersion({
      id: "st-4",
      version: 4,
      derived_from_state_id: "st-2",
    });
    expect(versionLabelKind(v4, [v2, v3, v4], [])).toBe("revert");
  });

  it("reports 'seed' for version 1", () => {
    const v1 = makeVersion({ id: "st-1", version: 1 });
    expect(versionLabelKind(v1, [v1], [])).toBe("seed");
  });

  it("reports 'edited' for an ordinary persist deriving from the previous row", () => {
    const v2 = makeVersion({ id: "st-2", version: 2 });
    const v3 = makeVersion({
      id: "st-3",
      version: 3,
      derived_from_state_id: "st-2",
    });
    expect(versionLabelKind(v3, [v2, v3], [])).toBe("edited");
  });

  it("keeps the same priority as the label: an applied stamp beats revert-shaped lineage", () => {
    const v2 = makeVersion({ id: "st-2", version: 2 });
    const v5 = makeVersion({
      id: "st-5",
      version: 5,
      derived_from_state_id: "st-2",
    });
    const messages = [
      makeMessage([
        {
          function: { name: "patch_node_options", arguments: "{}" },
          outcome: "applied",
          applied_state_version: 5,
        },
      ]),
    ];
    expect(versionLabelKind(v5, [v2, v5], messages)).toBe("applied");
  });
});

describe("isSnapshotOnly", () => {
  it("classifies a row differing only in bookkeeping axes as snapshot-only", () => {
    const v2 = {
      ...makeVersion({ id: "st-2", version: 2 }),
      composer_meta: { checkpoint: 4 },
      is_valid: false,
      validation_errors: ["dangling edge"],
    } as CompositionStateVersion;
    const v3 = {
      ...makeVersion({ id: "st-3", version: 3 }),
      composer_meta: { checkpoint: 5 },
      is_valid: true,
      validation_errors: null,
    } as CompositionStateVersion;
    expect(isSnapshotOnly(v3, v2)).toBe(true);
  });

  it("classifies a row where only composer_meta differs as snapshot-only", () => {
    const v2 = {
      ...makeVersion({ id: "st-2", version: 2 }),
      composer_meta: { checkpoint: 1 },
    } as CompositionStateVersion;
    const v3 = {
      ...makeVersion({ id: "st-3", version: 3 }),
      composer_meta: { checkpoint: 2 },
    } as CompositionStateVersion;
    expect(isSnapshotOnly(v3, v2)).toBe(true);
  });

  it("classifies changed node options as a real edit", () => {
    const v2 = makeVersion({
      id: "st-2",
      version: 2,
      nodes: [makeNode("n1", { fields: ["a"] })],
    });
    const v3 = makeVersion({
      id: "st-3",
      version: 3,
      nodes: [makeNode("n1", { fields: ["a", "b"] })],
    });
    expect(isSnapshotOnly(v3, v2)).toBe(false);
  });

  it("treats key-order-shuffled but semantically equal content as snapshot-only", () => {
    const v2 = makeVersion({
      id: "st-2",
      version: 2,
      sources: {
        main: { plugin: "csv_source", options: { path: "in.csv", limit: 5 } },
        aux: { plugin: "json_source", options: { path: "aux.json" } },
      },
      nodes: [makeNode("n1", { alpha: 1, beta: 2 })],
    });
    const v3 = makeVersion({
      id: "st-3",
      version: 3,
      sources: {
        aux: { options: { path: "aux.json" }, plugin: "json_source" },
        main: { options: { limit: 5, path: "in.csv" }, plugin: "csv_source" },
      },
      nodes: [makeNode("n1", { beta: 2, alpha: 1 })],
    });
    expect(isSnapshotOnly(v3, v2)).toBe(true);
  });

  it("returns false when the previous row is missing (first version / pagination edge)", () => {
    const v1 = makeVersion({ id: "st-1", version: 1 });
    expect(isSnapshotOnly(v1, undefined)).toBe(false);
    expect(isSnapshotOnly(v1, null)).toBe(false);
  });

  it("returns false for rows without content payloads (synthesized current entry)", () => {
    const slim: CompositionStateVersion = {
      id: "",
      version: 4,
      created_at: "2026-08-13T10:00:00Z",
      node_count: 2,
    };
    const full = makeVersion({ id: "st-3", version: 3 });
    expect(isSnapshotOnly(slim, full)).toBe(false);
    expect(isSnapshotOnly(full, slim)).toBe(false);
  });
});
