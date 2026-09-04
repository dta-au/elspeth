import { describe, expect, it } from "vitest";

import { compositionContentEqual } from "./compositionContent";
import type { CompositionState } from "@/types/index";

function content(
  overrides: Partial<CompositionState> = {},
): Pick<
  CompositionState,
  "sources" | "nodes" | "edges" | "outputs" | "metadata"
> {
  return {
    sources: {
      source: { plugin: "csv", options: { path: "in.csv" } },
    },
    nodes: [
      {
        id: "n1",
        node_type: "transform",
        plugin: "llm",
        input: "source",
        on_success: null,
        on_error: null,
        options: { prompt_template: "summarise {{ row.text }}" },
      },
    ],
    edges: [{ id: "e1", from: "source", to: "n1" }],
    outputs: [{ id: "o1", plugin: "csv", options: { path: "out.csv" } }],
    metadata: { name: "demo" },
    ...overrides,
  } as Pick<
    CompositionState,
    "sources" | "nodes" | "edges" | "outputs" | "metadata"
  >;
}

describe("compositionContentEqual", () => {
  it("is true for two independently built but identical contents", () => {
    expect(compositionContentEqual(content(), content())).toBe(true);
  });

  it("ignores fields outside the hashed set (version, id, validation)", () => {
    // The caller passes whole CompositionStates; only the authored projection
    // decides. `version` differing is the WHOLE POINT — that is the bump
    // being classified.
    const left = { ...content(), version: 7, id: "s7", is_valid: true };
    const right = { ...content(), version: 8, id: "s8", is_valid: false };
    expect(
      compositionContentEqual(
        left as unknown as CompositionState,
        right as unknown as CompositionState,
      ),
    ).toBe(true);
  });

  it("is insensitive to object key order", () => {
    const reordered = content({
      sources: { source: { options: { path: "in.csv" }, plugin: "csv" } as never },
    });
    expect(compositionContentEqual(content(), reordered)).toBe(true);
  });

  it.each([
    [
      "a changed node option (interpretation Accept rewrites prompt_template)",
      content({
        nodes: [
          {
            id: "n1",
            node_type: "transform",
            plugin: "llm",
            input: "source",
            on_success: null,
            on_error: null,
            options: { prompt_template: "ACCEPTED" },
          },
        ],
      }),
    ],
    ["an added node", content({ nodes: [] })],
    ["a changed source option", content({ sources: { source: { plugin: "csv", options: { path: "other.csv" } } } })],
    ["a rewired edge", content({ edges: [{ id: "e1", from: "source", to: "n2" } as never] })],
    ["a changed output", content({ outputs: [] })],
    ["changed metadata", content({ metadata: { name: "renamed" } as never })],
  ])("is false for %s", (_label, right) => {
    expect(compositionContentEqual(content(), right)).toBe(false);
  });

  it("keeps ARRAY order significant — reordering nodes is an authored change", () => {
    const nodeA = {
      id: "a",
      node_type: "transform",
      plugin: "llm",
      input: "source",
      on_success: null,
      on_error: null,
      options: {},
    };
    const nodeB = { ...nodeA, id: "b" };
    expect(
      compositionContentEqual(
        content({ nodes: [nodeA, nodeB] as never }),
        content({ nodes: [nodeB, nodeA] as never }),
      ),
    ).toBe(false);
  });

  it("fails CLOSED on an absent side", () => {
    // A false "equal" would suppress a clear + re-validate that a real edit
    // needed; a false "not equal" only costs a redundant validate.
    expect(compositionContentEqual(null, content())).toBe(false);
    expect(compositionContentEqual(content(), null)).toBe(false);
    expect(compositionContentEqual(undefined, undefined)).toBe(false);
  });

  it("distinguishes null from a missing key", () => {
    // {a: null} and {} are different authored content; a length-blind compare
    // would call them equal.
    expect(
      compositionContentEqual(
        content({ metadata: { name: null } as never }),
        content({ metadata: {} as never }),
      ),
    ).toBe(false);
  });
});
