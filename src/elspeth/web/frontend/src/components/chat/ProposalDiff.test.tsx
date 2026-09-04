import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  ArgumentFields,
  buildProposalDiff,
  ProposalChanges,
} from "./ProposalDiff";
import type { CompositionState } from "@/types/api";
import { compositionStateAuthorityFields } from "@/test/composerFixtures";
import { redactedArguments } from "@/test/redactedArgumentFixture";

function makeState(overrides: Partial<CompositionState> = {}): CompositionState {
  return {
    id: "state-1",
    ...compositionStateAuthorityFields,
    version: 3,
    sources: {
      source: {
        plugin: "csv",
        options: { path: "input.csv" },
        on_success: "rows",
        on_validation_failure: "discard",
      },
    },
    nodes: [
      {
        id: "extract",
        node_type: "transform",
        plugin: "field_mapper",
        input: "rows",
        on_success: "mapped",
        on_error: null,
        options: { mappings: { a: "b" } },
      },
    ],
    edges: [
      {
        id: "e1",
        from_node: "source",
        to_node: "extract",
        edge_type: "on_success",
        label: null,
      },
    ],
    outputs: [{ name: "results", plugin: "json", options: { path: "out.json" } }],
    metadata: { name: "My pipeline", description: null },
    ...overrides,
  };
}

describe("buildProposalDiff", () => {
  it("returns null with no current state — no honest before side exists", () => {
    expect(buildProposalDiff("upsert_node", { id: "x", node_type: "transform" }, null)).toBeNull();
  });

  it("returns null for tools with no state-fragment projection", () => {
    expect(buildProposalDiff("save_session", { name: "s" }, makeState())).toBeNull();
    expect(buildProposalDiff("create_blob", { filename: "f" }, makeState())).toBeNull();
  });

  it("projects upsert_node on an existing id as a changed node", () => {
    const entries = buildProposalDiff(
      "upsert_node",
      { id: "extract", node_type: "transform", plugin: "html_extract", input: "rows" },
      makeState(),
    );
    expect(entries).toEqual([
      expect.objectContaining({
        kind: "changed",
        section: "node",
        identity: "extract",
        beforeSummary: "transform field_mapper",
        afterSummary: "transform html_extract",
      }),
    ]);
  });

  it("projects upsert_node on a new id as an added node", () => {
    const entries = buildProposalDiff(
      "upsert_node",
      { id: "score", node_type: "transform", plugin: "llm_judge", input: "mapped" },
      makeState(),
    );
    expect(entries).toEqual([
      expect.objectContaining({
        kind: "added",
        section: "node",
        identity: "score",
        afterSummary: "transform llm_judge",
      }),
    ]);
  });

  it("projects remove_node against the current fragment", () => {
    const entries = buildProposalDiff("remove_node", { id: "extract" }, makeState());
    expect(entries).toEqual([
      expect.objectContaining({
        kind: "removed",
        section: "node",
        identity: "extract",
        beforeSummary: "transform field_mapper",
      }),
    ]);
  });

  it("projects set_source over the existing source as changed", () => {
    const entries = buildProposalDiff(
      "set_source",
      { plugin: "json", on_success: "rows", options: {}, on_validation_failure: "discard" },
      makeState(),
    );
    expect(entries).toEqual([
      expect.objectContaining({
        kind: "changed",
        section: "source",
        identity: "source",
        beforeSummary: "csv",
        afterSummary: "json",
      }),
    ]);
  });

  it("projects set_output and remove_output by sink name", () => {
    const setEntries = buildProposalDiff(
      "set_output",
      { sink_name: "errors", plugin: "csv", options: {} },
      makeState(),
    );
    expect(setEntries).toEqual([
      expect.objectContaining({ kind: "added", section: "output", identity: "errors" }),
    ]);

    const removeEntries = buildProposalDiff(
      "remove_output",
      { sink_name: "results" },
      makeState(),
    );
    expect(removeEntries).toEqual([
      expect.objectContaining({
        kind: "removed",
        section: "output",
        identity: "results",
        beforeSummary: "results (json)",
      }),
    ]);
  });

  // ---------------------------------------------------------------------
  // patch_*_options and set_metadata.
  //
  // Every case below feeds `redactedArguments(...)` — the payload the Python
  // redactor really produced, read from the committed fixture. These arms
  // previously had tests that hand-built `patch` as a plain object and all
  // four passed while the arms were DEAD on the live path (elspeth-b1c14dd3c2):
  // the producer ships a summary string, so `asRecord` returned null and every
  // projection bailed. Do not "simplify" these back to inline objects — an
  // input the producer cannot emit certifies nothing about the producer.
  // ---------------------------------------------------------------------

  it("projects patch_node_options as one row: real current options vs the patch's measured size", () => {
    const entries = buildProposalDiff(
      "patch_node_options",
      redactedArguments("patch_node_options_mixed_shapes"),
      makeState(),
    );

    // The patch carried four entries (a mapping, a sequence, a scalar and a
    // null). Per-key rows are impossible — the summary names no keys — so the
    // row states the size of the patch and nothing it cannot know.
    expect(entries).toEqual([
      expect.objectContaining({
        kind: "changed",
        section: "option",
        identity: "extract.options",
        beforeSummary: "1 option set",
        afterSummary: "patch of 4 entries, keys and values redacted",
      }),
    ]);
  });

  it("projects patch_source_options and patch_output_options through the same row", () => {
    expect(
      buildProposalDiff(
        "patch_source_options",
        redactedArguments("patch_source_options_two_scalars"),
        makeState(),
      ),
    ).toEqual([
      expect.objectContaining({
        section: "option",
        identity: "source.options",
        beforeSummary: "1 option set",
        afterSummary: "patch of 2 entries, keys and values redacted",
      }),
    ]);

    expect(
      buildProposalDiff(
        "patch_output_options",
        redactedArguments("patch_output_options_one_scalar"),
        makeState(),
      ),
    ).toEqual([
      expect.objectContaining({
        section: "option",
        identity: "results.options",
        afterSummary: "patch of 1 entry, keys and values redacted",
      }),
    ]);
  });

  it("returns an empty projection (not null) for an empty patch, which merges to a no-op", () => {
    expect(
      buildProposalDiff(
        "patch_node_options",
        redactedArguments("patch_node_options_empty_patch"),
        makeState(),
      ),
    ).toEqual([]);
  });

  it("returns null when a patch targets a node missing from the current state", () => {
    expect(
      buildProposalDiff(
        "patch_node_options",
        redactedArguments("patch_node_options_missing_node"),
        makeState(),
      ),
    ).toBeNull();
  });

  it("projects set_metadata per named key, claiming only that the field is written", () => {
    const entries = buildProposalDiff(
      "set_metadata",
      redactedArguments("set_metadata_name_and_description"),
      makeState(),
    );

    // "changed", not "added", even for the unset description: the sentinel
    // names the key but not the value, and a patch that clears a field to
    // null is indistinguishable from one that sets it. For the same reason
    // the row says the field is WRITTEN rather than promising a new value
    // sits behind the redaction.
    expect(entries).toEqual([
      expect.objectContaining({
        kind: "changed",
        section: "metadata",
        identity: "description",
        beforeSummary: "(not set)",
        afterSummary: "written, value redacted",
      }),
      expect.objectContaining({
        kind: "changed",
        section: "metadata",
        identity: "name",
        beforeSummary: '"My pipeline"',
        afterSummary: "written, value redacted",
      }),
    ]);
  });

  it("surfaces a metadata patch's unrecognised field instead of dropping it", () => {
    const entries = buildProposalDiff(
      "set_metadata",
      redactedArguments("set_metadata_unknown_key"),
      makeState(),
    );

    // This is an approval surface: an operator must not be shown a partial
    // account of what they are approving. The field cannot be named because
    // the producer collapses every unrecognised key to one token.
    expect(entries).toEqual([
      expect.objectContaining({ section: "metadata", identity: "name" }),
      expect.objectContaining({
        section: "metadata",
        identity: "(unrecognised field)",
        afterSummary: "written, field name and value redacted",
      }),
    ]);
  });

  it("renders the unrecognised-field row alone when it is the patch's only key", () => {
    // Producible: a patch whose every key is outside {name, description}
    // collapses to the bare `unknown` token, so there is no named key to
    // pair it with. The proposal must not render as empty — it writes
    // something, and the operator is being asked to approve it.
    const entries = buildProposalDiff(
      "set_metadata",
      redactedArguments("set_metadata_only_unknown_key"),
      makeState(),
    );

    expect(entries).toEqual([
      expect.objectContaining({
        kind: "changed",
        section: "metadata",
        identity: "(unrecognised field)",
      }),
    ]);
  });

  it("returns null when set_metadata carries no patch argument at all", () => {
    // `patch` is ABSENT from this redacted payload, not summarised — a third
    // state beside "mapping" and "summary string". No projection is derivable,
    // so the caller falls back to the argument fields rather than rendering an
    // empty diff that would read as "changes nothing".
    expect(
      buildProposalDiff(
        "set_metadata",
        redactedArguments("set_metadata_no_arguments"),
        makeState(),
      ),
    ).toBeNull();
  });

  it("separates an empty metadata patch from an unreadable one", () => {
    // empty → the projection ran and found nothing to report.
    expect(
      buildProposalDiff("set_metadata", redactedArguments("set_metadata_empty"), makeState()),
    ).toEqual([]);
    // invalid → no honest projection; ToolCallCard falls back to the raw
    // argument fields. "No projection" is not "no change".
    expect(
      buildProposalDiff("set_metadata", redactedArguments("set_metadata_invalid"), makeState()),
    ).toBeNull();
  });

  it("reports nothing when set_pipeline replays the current state verbatim", () => {
    // The live-shape regression for the spurious-"Changed" defect. The
    // redactor summarises every `options` mapping into a string, which can
    // never equal the unredacted mapping in state — before providedKeysDiffer
    // learned to skip those, this proposal reported source, node AND output
    // as changed while proposing no change at all.
    const entries = buildProposalDiff(
      "set_pipeline",
      redactedArguments("set_pipeline_replaying_current_state"),
      makeState(),
    );

    expect(entries).toEqual([]);
  });

  it("keeps the identity-bearing arms working on the real redacted payload", () => {
    // upsert_node's projection reads id / node_type / plugin, which survive
    // redaction while `options` does not. Pinned on the live payload so a
    // future redaction change that starts summarising them fails here rather
    // than silently emptying the proposal card.
    const entries = buildProposalDiff(
      "upsert_node",
      redactedArguments("upsert_node_with_options"),
      makeState(),
    );

    expect(entries).toEqual([
      expect.objectContaining({
        kind: "changed",
        section: "node",
        identity: "extract",
        beforeSummary: "transform field_mapper",
        afterSummary: "transform html_extract",
      }),
    ]);
  });

  it("projects set_pipeline as added/removed/changed rows across collections", () => {
    // SYNTHETIC payload, deliberately: this exercises the collection-matching
    // logic (added / removed / changed / identity alignment) across a spread
    // of differences that one recorded proposal does not contain. The
    // identity keys it feeds — id, plugin, sink_name, edge endpoints — are
    // the ones that DO survive redaction unchanged, which the live-shape
    // tests above pin. Do not add option or metadata assertions here; those
    // arrive summarised and belong on a fixture-driven test.
    const entries = buildProposalDiff(
      "set_pipeline",
      {
        source: { plugin: "json", on_success: "rows" },
        nodes: [
          // extract kept but plugin changed
          { id: "extract", node_type: "transform", plugin: "html_extract", input: "rows" },
          // score added
          { id: "score", node_type: "transform", plugin: "llm_judge", input: "mapped" },
        ],
        edges: [
          { id: "e1", from_node: "source", to_node: "extract", edge_type: "on_success" },
        ],
        outputs: [
          // results dropped, errors added
          { sink_name: "errors", plugin: "csv", options: {} },
        ],
      },
      makeState(),
    );

    expect(entries).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ kind: "changed", section: "source", identity: "source" }),
        expect.objectContaining({ kind: "changed", section: "node", identity: "extract" }),
        expect.objectContaining({ kind: "added", section: "node", identity: "score" }),
        expect.objectContaining({ kind: "added", section: "output", identity: "errors" }),
        expect.objectContaining({ kind: "removed", section: "output", identity: "results" }),
      ]),
    );
    // e1's provided keys match the current edge — must NOT be flagged.
    expect(entries?.some((entry) => entry.section === "edge")).toBe(false);
  });
});

describe("ProposalChanges", () => {
  it("renders diff rows through the shared recovery-diff row rendering", () => {
    const entries = buildProposalDiff(
      "upsert_node",
      { id: "extract", node_type: "transform", plugin: "html_extract", input: "rows" },
      makeState(),
    );
    render(<ProposalChanges entries={entries ?? []} />);

    expect(screen.getByText("Proposed changes")).toBeInTheDocument();
    expect(screen.getByText("Changed node")).toBeInTheDocument();
    expect(screen.getByText("extract")).toBeInTheDocument();
    expect(screen.getByText("transform field_mapper")).toBeInTheDocument();
    expect(screen.getByText("transform html_extract")).toBeInTheDocument();
  });

  it("says so plainly when the projection finds no difference", () => {
    render(<ProposalChanges entries={[]} />);
    expect(
      screen.getByText("No difference from the current pipeline."),
    ).toBeInTheDocument();
  });
});

describe("ArgumentFields", () => {
  it("renders one labelled row per top-level argument", () => {
    render(
      <ArgumentFields
        args={{ sink_name: "results", plugin: "json", options: { path: "out.json" } }}
      />,
    );

    expect(screen.getByTestId("proposal-arg-fields")).toBeInTheDocument();
    expect(screen.getByText("sink_name")).toBeInTheDocument();
    expect(screen.getByText('"results"')).toBeInTheDocument();
    expect(screen.getByText("plugin")).toBeInTheDocument();
    // Nested objects render as formatted JSON, not a flat stringify of the
    // whole argument payload.
    expect(screen.getByText(/"path": "out\.json"/)).toBeInTheDocument();
  });

  it("handles zero-argument tools without rendering an empty list", () => {
    render(<ArgumentFields args={{}} />);
    expect(screen.getByText("No settings change in this step.")).toBeInTheDocument();
  });
});
