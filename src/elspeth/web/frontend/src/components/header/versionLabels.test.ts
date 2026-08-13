import { describe, it, expect } from "vitest";
import { TOOL_CALL_DESCRIPTIONS } from "@/components/chat/toolCallDescriptions";
import type {
  ChatMessage,
  CompositionStateVersion,
  NodeSpec,
  ToolCall,
} from "@/types/index";
import {
  appliedToolCallName,
  deriveVersionLabel,
  describeVersionOperation,
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

describe("describeVersionOperation", () => {
  it("humanizes a known applied tool via TOOL_CALL_DESCRIPTIONS", () => {
    const messages = [
      makeMessage([
        {
          function: { name: "set_pipeline", arguments: "{}" },
          outcome: "applied",
          applied_state_version: 2,
        },
      ]),
    ];
    const description = describeVersionOperation(
      makeVersion({ id: "st-2", version: 2 }),
      messages,
    );
    expect(description).toBe(TOOL_CALL_DESCRIPTIONS.set_pipeline);
    expect(description).toMatch(/pipeline/i);
  });

  it("returns null for unknown tools and unlabeled versions", () => {
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
      describeVersionOperation(makeVersion({ id: "st-2", version: 2 }), messages),
    ).toBeNull();
    expect(
      describeVersionOperation(makeVersion({ id: "st-3", version: 3 }), []),
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
      "Applied: upsert_edge",
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

  it("prefers the applied stamp over revert-shaped lineage (wire_review commits may derive from the grandparent)", () => {
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
      "Applied: patch_node_options",
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

describe("isSnapshotOnly", () => {
  it("classifies a row differing only in bookkeeping axes as snapshot-only", () => {
    const v2 = {
      ...makeVersion({ id: "st-2", version: 2 }),
      composer_meta: { guided_session: { step: "step_3", turn: 4 } },
      is_valid: false,
      validation_errors: ["dangling edge"],
    } as CompositionStateVersion;
    const v3 = {
      ...makeVersion({ id: "st-3", version: 3 }),
      composer_meta: { guided_session: { step: "step_4", turn: 5 } },
      is_valid: true,
      validation_errors: null,
    } as CompositionStateVersion;
    expect(isSnapshotOnly(v3, v2)).toBe(true);
  });

  // Guard-quality pin: the discriminator reads the content fields plus ONE
  // named exception (guided reviewed-source blob bindings, pinned below), so
  // a future field added anywhere else under composer_meta can never flip
  // real edits to "no change" (nor bookkeeping flips to "changed").
  it("classifies a row where ONLY composer_meta.guided_session differs as snapshot-only", () => {
    const v2 = {
      ...makeVersion({ id: "st-2", version: 2 }),
      composer_meta: { guided_session: { transition_consumed: false } },
    } as CompositionStateVersion;
    const v3 = {
      ...makeVersion({ id: "st-3", version: 3 }),
      composer_meta: { guided_session: { transition_consumed: true } },
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

// The guided blob-swap case. A guided commit strips `blob_ref` from the
// executable source and the backend redaction then overwrites that source's
// path carriers with a CONSTANT sentinel, so replacing the input file leaves
// sources/nodes/edges/outputs/metadata byte-identical on the wire. The only
// retained discriminator is the reviewed snapshot's own binding under
// composer_meta.guided_session.reviewed_sources — verified against
// web/composer/redaction.py (redact_guided_snapshot_storage_paths) and
// web/composer/guided_blob_refs.py, which redact only the "path"/"file"
// carriers and leave `blob_ref` and public `blob:<uuid>` sentinels intact.
describe("isSnapshotOnly — guided reviewed-source blob bindings", () => {
  const REDACTED_PATH = "<redacted-blob-source-path>";
  const BLOB_A = "11111111-1111-4111-8111-111111111111";
  const BLOB_B = "22222222-2222-4222-8222-222222222222";

  /** A guided row as the wire delivers it after the explicit-ref redaction:
   *  the committed source carries only the constant sentinel path, and the
   *  reviewed snapshot keeps the blob_ref. */
  function guidedExplicitRefVersion(
    id: string,
    version: number,
    blobRef: string,
    guidedExtras: Record<string, unknown> = {},
  ): CompositionStateVersion {
    return {
      ...makeVersion({
        id,
        version,
        sources: {
          main: { plugin: "csv_source", options: { path: REDACTED_PATH } },
        },
      }),
      composer_meta: {
        guided_session: {
          ...guidedExtras,
          reviewed_sources: {
            "src-1": {
              name: "main",
              options: { blob_ref: blobRef, path: REDACTED_PATH },
            },
          },
          pending_source_intents: {},
        },
      },
    } as CompositionStateVersion;
  }

  it("refuses 'no change' when the guided input blob was swapped (explicit-ref arm)", () => {
    const v2 = guidedExplicitRefVersion("st-2", 2, BLOB_A);
    const v3 = guidedExplicitRefVersion("st-3", 3, BLOB_B);
    // Every CONTENT_FIELD is byte-identical — the blob_ref is the whole
    // difference, and it is a real change of the data the pipeline reads.
    for (const field of ["sources", "nodes", "edges", "outputs", "metadata"] as const) {
      expect(JSON.stringify(v3[field])).toBe(JSON.stringify(v2[field]));
    }
    expect(isSnapshotOnly(v3, v2)).toBe(false);
  });

  it("still classifies a guided bookkeeping turn as snapshot-only when the blob is unchanged", () => {
    const v2 = guidedExplicitRefVersion("st-2", 2, BLOB_A, {
      step: "step_3",
      transition_consumed: false,
    });
    const v3 = guidedExplicitRefVersion("st-3", 3, BLOB_A, {
      step: "step_4",
      transition_consumed: true,
    });
    expect(isSnapshotOnly(v3, v2)).toBe(true);
  });

  it("refuses 'no change' when a guided source gains or loses a blob binding", () => {
    const bound = guidedExplicitRefVersion("st-3", 3, BLOB_A);
    const unbound = {
      ...makeVersion({
        id: "st-2",
        version: 2,
        sources: {
          main: { plugin: "csv_source", options: { path: REDACTED_PATH } },
        },
      }),
      composer_meta: {
        guided_session: {
          reviewed_sources: {
            "src-1": { name: "main", options: { path: REDACTED_PATH } },
          },
          pending_source_intents: {},
        },
      },
    } as CompositionStateVersion;
    expect(isSnapshotOnly(bound, unbound)).toBe(false);
    expect(isSnapshotOnly(unbound, bound)).toBe(false);
  });

  // Public-sentinel arm: the reviewed snapshot passes through the redaction
  // untouched and its path carriers are `blob:<uuid>` strings. Read as a
  // binding too, so the axis is complete in both wire shapes.
  it("reads a public blob: sentinel carrier as a binding", () => {
    const sentinelVersion = (
      id: string,
      version: number,
      blobRef: string,
    ): CompositionStateVersion =>
      ({
        ...makeVersion({
          id,
          version,
          sources: {
            main: { plugin: "csv_source", options: { path: REDACTED_PATH } },
          },
        }),
        composer_meta: {
          guided_session: {
            reviewed_sources: {
              "src-1": { name: "main", options: { path: `blob:${blobRef}` } },
            },
            pending_source_intents: {},
          },
        },
      }) as CompositionStateVersion;
    expect(
      isSnapshotOnly(sentinelVersion("st-3", 3, BLOB_B), sentinelVersion("st-2", 2, BLOB_A)),
    ).toBe(false);
    expect(
      isSnapshotOnly(sentinelVersion("st-3", 3, BLOB_A), sentinelVersion("st-2", 2, BLOB_A)),
    ).toBe(true);
  });

  it("refuses 'no change' when composer_meta cannot be read at all", () => {
    const readable = guidedExplicitRefVersion("st-2", 2, BLOB_A);
    const unreadable = {
      ...makeVersion({
        id: "st-3",
        version: 3,
        sources: {
          main: { plugin: "csv_source", options: { path: REDACTED_PATH } },
        },
      }),
      composer_meta: { guided_session: { reviewed_sources: "not-a-mapping" } },
    } as CompositionStateVersion;
    expect(isSnapshotOnly(unreadable, readable)).toBe(false);
    expect(isSnapshotOnly(readable, unreadable)).toBe(false);
  });

  it("ignores a freeform row's absent composer_meta rather than treating it as unreadable", () => {
    const v2 = makeVersion({ id: "st-2", version: 2 });
    const v3 = makeVersion({ id: "st-3", version: 3 });
    expect(isSnapshotOnly(v3, v2)).toBe(true);
  });
});
