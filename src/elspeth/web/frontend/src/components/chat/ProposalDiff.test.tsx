import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  ArgumentFields,
  buildProposalDiff,
  ProposalChanges,
} from "./ProposalDiff";
import type { DiffEntry } from "@/components/recovery/RecoveryDiff";
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

/**
 * Entries only, for the assertions that do not care about the caveat flag.
 * `null` still means "no projection" — `entries` is always an array when a
 * projection exists, so the coalesce cannot hide an empty result.
 */
function projectEntries(
  ...args: Parameters<typeof buildProposalDiff>
): DiffEntry[] | null {
  return buildProposalDiff(...args)?.entries ?? null;
}

describe("buildProposalDiff", () => {
  it("returns null with no current state — no honest before side exists", () => {
    expect(projectEntries("upsert_node", { id: "x", node_type: "transform" }, null)).toBeNull();
  });

  it("returns null for tools with no state-fragment projection", () => {
    expect(projectEntries("save_session", { name: "s" }, makeState())).toBeNull();
    expect(projectEntries("create_blob", { filename: "f" }, makeState())).toBeNull();
  });

  it("projects upsert_node on an existing id as a changed node", () => {
    const entries = projectEntries(
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
    const entries = projectEntries(
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
    const entries = projectEntries("remove_node", { id: "extract" }, makeState());
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
    const entries = projectEntries(
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
    const setEntries = projectEntries(
      "set_output",
      { sink_name: "errors", plugin: "csv", options: {} },
      makeState(),
    );
    expect(setEntries).toEqual([
      expect.objectContaining({ kind: "added", section: "output", identity: "errors" }),
    ]);

    const removeEntries = projectEntries(
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
    const entries = projectEntries(
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
      projectEntries(
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
      projectEntries(
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
      projectEntries(
        "patch_node_options",
        redactedArguments("patch_node_options_empty_patch"),
        makeState(),
      ),
    ).toEqual([]);
  });

  it("returns null for a patch summary whose root is not a mapping (defensive)", () => {
    // DEFENSIVE, not live-path: the patch_*_options argument models require a
    // dict, so pydantic rejects a non-mapping patch before redaction runs and
    // no proposal is created. The guard exists so that if a future producer
    // change ever routes a sequence or scalar payload through this argument,
    // it renders nothing rather than "patch of 2 entries" — a count that would
    // describe a list's elements as if they were option keys. Without this
    // test the guard is a mutation survivor: deleting the rootShape clause
    // keeps the whole suite green.
    const sequenceRootPatch = JSON.stringify({
      _option_shape: "sequence",
      entry_count: 2,
      value_shape_counts: { mapping: 0, scalar: 2, sequence: 0, set: 0 },
    });

    expect(
      projectEntries(
        "patch_node_options",
        { node_id: "extract", patch: sequenceRootPatch },
        makeState(),
      ),
    ).toBeNull();
  });

  it("returns null when a patch targets a node missing from the current state", () => {
    expect(
      projectEntries(
        "patch_node_options",
        redactedArguments("patch_node_options_missing_node"),
        makeState(),
      ),
    ).toBeNull();
  });

  it("projects set_metadata per named key, claiming only that the field is written", () => {
    const entries = projectEntries(
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
    const entries = projectEntries(
      "set_metadata",
      redactedArguments("set_metadata_unknown_key"),
      makeState(),
    );

    // This is an approval surface: an operator must not be shown a partial
    // account of what they are approving. The field cannot be named because
    // the producer collapses every unrecognised key to one token — and for
    // the same reason the row must stay CARDINALITY-NEUTRAL: that one token
    // is emitted for a three-unknown-key patch exactly as for a one-key one,
    // so "(unrecognised field)" would claim a count the payload cannot carry.
    expect(entries).toEqual([
      expect.objectContaining({ section: "metadata", identity: "name" }),
      expect.objectContaining({
        section: "metadata",
        identity: "(one or more unrecognised fields)",
        afterSummary: "written, names and values redacted",
      }),
    ]);
  });

  it("renders the unrecognised-field row alone when it is the patch's only key", () => {
    // Producible: a patch whose every key is outside {name, description}
    // collapses to the bare `unknown` token, so there is no named key to
    // pair it with. The proposal must not render as empty — it writes
    // something, and the operator is being asked to approve it.
    const entries = projectEntries(
      "set_metadata",
      redactedArguments("set_metadata_only_unknown_key"),
      makeState(),
    );

    expect(entries).toEqual([
      expect.objectContaining({
        kind: "changed",
        section: "metadata",
        identity: "(one or more unrecognised fields)",
      }),
    ]);
  });

  it("returns null when set_metadata carries no patch argument at all", () => {
    // `patch` is ABSENT from this redacted payload, not summarised — a third
    // state beside "mapping" and "summary string". No projection is derivable,
    // so the caller falls back to the argument fields rather than rendering an
    // empty diff that would read as "changes nothing".
    expect(
      projectEntries(
        "set_metadata",
        redactedArguments("set_metadata_no_arguments"),
        makeState(),
      ),
    ).toBeNull();
  });

  it("separates an empty metadata patch from an unreadable one", () => {
    // empty → the projection ran and found nothing to report.
    expect(
      projectEntries("set_metadata", redactedArguments("set_metadata_empty"), makeState()),
    ).toEqual([]);
    // invalid → no honest projection; ToolCallCard falls back to the raw
    // argument fields. "No projection" is not "no change".
    expect(
      projectEntries("set_metadata", redactedArguments("set_metadata_invalid"), makeState()),
    ).toBeNull();
  });

  it("reports nothing when set_pipeline replays the current state verbatim", () => {
    // The live-shape regression for the spurious-"Changed" defect. The
    // redactor summarises every `options` mapping into a string, which can
    // never equal the unredacted mapping in state — before providedKeysDiffer
    // learned to skip those, this proposal reported source, node AND output
    // as changed while proposing no change at all.
    const entries = projectEntries(
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
    const entries = projectEntries(
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
    const entries = projectEntries(
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

  it("reports that option values were never compared, so an empty result cannot claim 'no difference'", () => {
    // The redacted set_pipeline payload carries `options` as a shape summary
    // on the source, the node AND the output. Those three keys are skipped,
    // not compared — a set_pipeline whose ONLY change is plugin options is
    // byte-identical on the wire to one that changes nothing, so an empty
    // result here means "nothing comparable differs", never "nothing differs".
    const diff = buildProposalDiff(
      "set_pipeline",
      redactedArguments("set_pipeline_replaying_current_state"),
      makeState(),
    );

    expect(diff).not.toBeNull();
    expect(diff?.entries).toEqual([]);
    expect(diff?.optionValuesNotCompared).toBe(true);
  });

  it("does not raise the caveat for a projection that compared everything it was given", () => {
    // upsert_node's arm never calls providedKeysDiffer, so nothing is skipped
    // — the flag must not be set merely because the payload CONTAINS a
    // redacted summary. This is what keeps the honest empty-state sentence
    // reachable.
    const diff = buildProposalDiff(
      "upsert_node",
      redactedArguments("upsert_node_with_options"),
      makeState(),
    );

    expect(diff?.optionValuesNotCompared).toBe(false);
  });
});

describe("ProposalChanges", () => {
  it("renders diff rows through the shared recovery-diff row rendering", () => {
    const entries = projectEntries(
      "upsert_node",
      { id: "extract", node_type: "transform", plugin: "html_extract", input: "rows" },
      makeState(),
    );
    render(
      <ProposalChanges diff={{ entries: entries ?? [], optionValuesNotCompared: false }} />,
    );

    expect(screen.getByText("Proposed changes")).toBeInTheDocument();
    expect(screen.getByText("Changed node")).toBeInTheDocument();
    expect(screen.getByText("extract")).toBeInTheDocument();
    expect(screen.getByText("transform field_mapper")).toBeInTheDocument();
    expect(screen.getByText("transform html_extract")).toBeInTheDocument();
  });

  it("says so plainly when the projection finds no difference", () => {
    render(<ProposalChanges diff={{ entries: [], optionValuesNotCompared: false }} />);
    expect(
      screen.getByText("No difference from the current pipeline."),
    ).toBeInTheDocument();
  });

  it("never claims 'no difference' when option values were not compared", () => {
    // The same empty list, a different fact about it. Claiming "No difference
    // from the current pipeline." here would be an affirmative false statement
    // on a human approval gate — worse than the false "Changed" rows it
    // replaced, because it positively asserts safety.
    render(<ProposalChanges diff={{ entries: [], optionValuesNotCompared: true }} />);

    expect(
      screen.queryByText("No difference from the current pipeline."),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("No difference in what this view can compare."),
    ).toBeInTheDocument();
    expect(screen.getByTestId("proposal-diff-caveat")).toBeInTheDocument();
  });

  it("carries the caveat when rows ARE present, because the list is not exhaustive either", () => {
    // A reviewer who sees rows reasonably infers that is the complete set and
    // approves. Option values were never compared, so there may be
    // differences this list cannot show — the same over-claim of completeness
    // as the empty state, on the same gate, so it gets the same correction.
    render(
      <ProposalChanges
        diff={{
          entries: [
            {
              kind: "changed",
              section: "node",
              identity: "extract",
              before: undefined,
              after: undefined,
              beforeSummary: "transform field_mapper",
              afterSummary: "transform html_extract",
            },
          ],
          optionValuesNotCompared: true,
        }}
      />,
    );

    expect(screen.getByText("Changed node")).toBeInTheDocument();
    expect(screen.getByTestId("proposal-diff-caveat")).toHaveTextContent(
      "Option values are not compared, so a change to them would not appear here.",
    );
    // The rows render normally; the caveat qualifies the list, it does not
    // replace it, and the empty-state sentence must not appear beside rows.
    expect(
      screen.queryByText("No difference from the current pipeline."),
    ).not.toBeInTheDocument();
  });

  it("shows no caveat when the projection compared everything it was given", () => {
    // Gated on the ledger, not on a constant: a future change that stops
    // skipping removes the note automatically rather than stranding a caveat
    // that is no longer true.
    render(
      <ProposalChanges
        diff={{
          entries: [
            {
              kind: "changed",
              section: "node",
              identity: "extract",
              before: undefined,
              after: undefined,
              beforeSummary: "a",
              afterSummary: "b",
            },
          ],
          optionValuesNotCompared: false,
        }}
      />,
    );

    expect(screen.queryByTestId("proposal-diff-caveat")).not.toBeInTheDocument();
  });

  it("labels redaction-limited rows 'Writes', not 'Changed'", () => {
    // "Changed option" asserts a delta this projection cannot measure: a patch
    // setting an option to the value it already holds is byte-identical, on
    // the wire, to one that changes it. The pre-redaction code could suppress
    // that no-op; this code cannot, so the label must not imply it did.
    const optionDiff = buildProposalDiff(
      "patch_node_options",
      redactedArguments("patch_node_options_one_mapping"),
      makeState(),
    );
    const { unmount } = render(<ProposalChanges diff={optionDiff!} />);

    expect(screen.getByText("Writes option")).toBeInTheDocument();
    expect(screen.queryByText("Changed option")).not.toBeInTheDocument();
    unmount();

    const metadataDiff = buildProposalDiff(
      "set_metadata",
      redactedArguments("set_metadata_name_only"),
      makeState(),
    );
    render(<ProposalChanges diff={metadataDiff!} />);

    expect(screen.getByText("Writes metadata")).toBeInTheDocument();
    expect(screen.queryByText("Changed metadata")).not.toBeInTheDocument();
  });

  it("keeps the kind-derived label on rows built from values it can actually see", () => {
    // The override is scoped to the two redaction-limited sections. A node row
    // is compared against unredacted state, so "Changed node" is a measured
    // claim and must survive.
    const diff = buildProposalDiff(
      "upsert_node",
      redactedArguments("upsert_node_with_options"),
      makeState(),
    );
    render(<ProposalChanges diff={diff!} />);

    expect(screen.getByText("Changed node")).toBeInTheDocument();
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
