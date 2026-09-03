import { describe, expect, it } from "vitest";

import { makeComposition } from "@/test/composerFixtures";
import type {
  ChatTurn,
  ComponentReviewItem,
  GuidedReviewedComponents,
  GuidedStep,
  TurnRecord,
  WireStageData,
} from "@/types/guided";
import {
  guidedDecisionRecord,
  guidedDecisionRows,
  guidedDecisionTurns,
  reviewedGuidedStages,
} from "./guidedDecisionStages";

const SOURCE_ID = "00000000-0000-4000-8000-00000000d001";
const OUTPUT_ID = "00000000-0000-4000-8000-00000000d002";
const NODE_ID = "00000000-0000-4000-8000-00000000d003";

function reviewedItem(
  stable_id: string,
  name: string,
  plugin: string,
): ComponentReviewItem {
  return { stable_id, name, plugin, status: "reviewed" };
}

function ledger(
  overrides: Partial<GuidedReviewedComponents> = {},
): GuidedReviewedComponents {
  return { sources: [], outputs: [], ...overrides };
}

function chatTurn(overrides: Partial<ChatTurn> & { seq: number }): ChatTurn {
  return {
    role: "user",
    content: "hello",
    step: "step_1_source",
    ts_iso: "2026-09-03T00:00:00Z",
    assistant_message_kind: null,
    synthetic_failure_reason: null,
    turn_token: null,
    ...overrides,
  };
}

function wireNode(
  overrides: Partial<WireStageData["nodes"][number]> = {},
): WireStageData["nodes"][number] {
  return {
    stable_id: NODE_ID,
    label: "Summarise",
    node_type: "transform",
    plugin: "llm",
    behavior: { kind: "transform" },
    required_fields: [],
    guaranteed_fields: [],
    row_cardinality: { input: "one", output: "one", expected_output_count: null },
    structured_output_fields: [],
    node_options_summary: [],
    ...overrides,
  };
}

describe("reviewedGuidedStages", () => {
  it("reads Source and Output from the server ledger, not from walk position", () => {
    // The defect this rule replaces: a stepper that read "settled" as
    // "index below the current step" calls a stage the server HAS a decision
    // for "not started" the moment the learner stands upstream of it.
    const settled = reviewedGuidedStages(
      ledger({ sources: [reviewedItem(SOURCE_ID, "pages", "csv_file")] }),
      "step_1_source",
      false,
    );
    expect(settled.has("step_1_source")).toBe(true);
  });

  it("keeps a settled stage DOWNSTREAM of the current step settled", () => {
    // The rewind case the index rule cannot express: a learner back at Source
    // has already settled Output, which now sits ahead of them.
    const settled = reviewedGuidedStages(
      ledger({
        sources: [reviewedItem(SOURCE_ID, "pages", "csv_file")],
        outputs: [reviewedItem(OUTPUT_ID, "results", "csv_file")],
      }),
      "step_1_source",
      false,
    );
    expect(settled.has("step_2_sink")).toBe(true);
  });

  it("leaves a stage with no ledger entry unsettled", () => {
    const settled = reviewedGuidedStages(ledger(), "step_2_sink", false);
    expect(settled.has("step_1_source")).toBe(false);
    expect(settled.has("step_2_sink")).toBe(false);
  });

  it("settles Transforms at the wire step and Wire only on commit", () => {
    const atWire = reviewedGuidedStages(ledger(), "step_4_wire", false);
    expect(atWire.has("step_3_transforms")).toBe(true);
    expect(atWire.has("step_4_wire")).toBe(false);

    const atTransforms = reviewedGuidedStages(
      ledger(),
      "step_3_transforms",
      false,
    );
    expect(atTransforms.has("step_3_transforms")).toBe(false);
  });

  it("settles every stage on a completed session even with an empty ledger", () => {
    // A committed pipeline settled all four stages by construction, and the
    // graduation view selects exactly such a session. Deriving three of its
    // ticks from a ledger a projection defect could empty would take the whole
    // record away at the one moment it is entirely history.
    const settled = reviewedGuidedStages(ledger(), "step_4_wire", true);
    expect([...settled].sort()).toEqual([
      "step_1_source",
      "step_2_sink",
      "step_3_transforms",
      "step_4_wire",
    ]);
  });
});

describe("guidedDecisionRows", () => {
  const reviewed = ledger({
    sources: [reviewedItem(SOURCE_ID, "pages", "csv_file")],
    outputs: [reviewedItem(OUTPUT_ID, "results", "azure_blob")],
  });

  it("projects the ledger's own components for Source and Output, in order", () => {
    expect(
      guidedDecisionRows("step_1_source", reviewed, [], null, false),
    ).toEqual([{ key: SOURCE_ID, name: "pages", plugin: "csv_file" }]);
    expect(guidedDecisionRows("step_2_sink", reviewed, [], null, false)).toEqual(
      [{ key: OUTPUT_ID, name: "results", plugin: "azure_blob" }],
    );
  });

  it("takes Transforms from the live wire card mid-walk", () => {
    // Same labels the wire card above the sheet is showing: two renderings of
    // one accepted graph must not name its nodes differently.
    expect(
      guidedDecisionRows(
        "step_3_transforms",
        reviewed,
        [wireNode(), wireNode({ stable_id: "n2", label: "Fetch", plugin: null })],
        null,
        false,
      ),
    ).toEqual([
      { key: NODE_ID, name: "Summarise", plugin: "llm" },
      { key: "n2", name: "Fetch", plugin: null },
    ]);
  });

  it("takes Transforms from the committed composition once complete, humanised", () => {
    // The wire card is gone on a completed session (next_turn is null), so the
    // committed nodes are the only surviving record — and their ids go through
    // the app's single node-name choke point rather than being printed raw.
    const rows = guidedDecisionRows(
      "step_3_transforms",
      reviewed,
      [wireNode()],
      makeComposition(3),
      true,
    );
    expect(rows).toEqual([
      { key: "select_columns", name: "Select Columns", plugin: "select_columns" },
    ]);
  });

  it("gives the wire stage no component rows", () => {
    expect(guidedDecisionRows("step_4_wire", reviewed, [wireNode()], null, true))
      .toEqual([]);
  });
});

describe("guidedDecisionTurns", () => {
  const CONFIRMATION = "c".repeat(64);
  const history: ChatTurn[] = [
    chatTurn({ seq: 2, step: "step_1_source", content: "source please" }),
    chatTurn({ seq: 1, step: "step_1_source", content: "earlier" }),
    chatTurn({ seq: 3, step: "step_4_wire", content: "pre-commit wire talk" }),
    chatTurn({
      seq: 4,
      step: "step_4_wire",
      content: "what does this pipeline do?",
      turn_token: CONFIRMATION,
    }),
    chatTurn({
      seq: 5,
      step: "step_4_wire",
      role: "assistant",
      content: "post-commit answer",
    }),
    // The turn a re-entered session types at its rewound stage: step_2_sink,
    // and above the confirmation boundary because the boundary is older than
    // the re-entry.
    chatTurn({
      seq: 6,
      step: "step_2_sink",
      content: "change the output to JSON",
    }),
  ];

  it("returns only the stage's own turns, in seq order", () => {
    expect(
      guidedDecisionTurns("step_1_source", history, null, true).map((t) => t.seq),
    ).toEqual([1, 2]);
  });

  it("excludes the post-build conversation from the wire stage", () => {
    // Post-commit questions are persisted with step="step_4_wire", identical
    // to the pre-commit wire turns, so a stage filter alone would replay the
    // entire advisory conversation as part of the wiring decision — on exactly
    // the completed sessions these sheets exist to serve.
    const turns = guidedDecisionTurns("step_4_wire", history, CONFIRMATION, true);
    expect(turns.map((t) => t.seq)).toEqual([3]);
    expect(turns.map((t) => t.content)).not.toContain("post-commit answer");
  });

  it("keeps every wire turn when the session never confirmed a build", () => {
    expect(
      guidedDecisionTurns("step_4_wire", history, null, false).map((t) => t.seq),
    ).toEqual([3, 4, 5]);
  });

  it("keeps the live conversation at a rewound stage after a re-entry", () => {
    // The regression this guards. `afterConfirmationChatToken` answers a
    // question about the TRANSCRIPT, so it still returns the confirmation
    // hash after `/guided/reenter` on changed content rewinds the session to
    // step_2_sink with terminal=null. Applying the boundary there would drop
    // every turn the learner has typed SINCE the rewind and replay only the
    // superseded pre-commit conversation under a heading that says the stage
    // is decided.
    const turns = guidedDecisionTurns("step_2_sink", history, CONFIRMATION, false);
    expect(turns.map((t) => t.seq)).toEqual([6]);
    expect(turns.map((t) => t.content)).toEqual(["change the output to JSON"]);
  });

  it("drops the same live turn once that session completes", () => {
    // The other direction: `completed` is the whole of the difference, so a
    // filter that ignored it and a filter that ignored the token are both
    // ruled out by this pair.
    expect(
      guidedDecisionTurns("step_2_sink", history, CONFIRMATION, true).map(
        (t) => t.seq,
      ),
    ).toEqual([]);
  });

  it("keeps the whole wire transcript on a live session", () => {
    // The deliberate cost of gating on `completed`, stated rather than
    // stumbled into: a live session's wire turns are no longer split at the
    // boundary. It reaches no screen — `reviewedGuidedStages` settles
    // step_4_wire only in its completed arm, so the Wire tick offers no sheet
    // while the session is live — and it is the honest reading either way,
    // since a rewound session's later wire turns are not post-commit.
    expect(
      guidedDecisionTurns("step_4_wire", history, CONFIRMATION, false).map(
        (t) => t.seq,
      ),
    ).toEqual([3, 4, 5]);
  });
});

describe("guidedDecisionRecord", () => {
  function record(step: GuidedStep, summary: string | null): TurnRecord {
    return {
      step,
      turn_type: "confirm_wiring",
      payload_hash: "p".repeat(64),
      response_hash: "r".repeat(64),
      summary,
      emitter: "server",
    };
  }

  it("takes the latest summarised record for the stage", () => {
    expect(
      guidedDecisionRecord("step_4_wire", [
        record("step_4_wire", "Guided pipeline wiring correction requested."),
        record("step_3_transforms", "Structured guided response accepted."),
        record("step_4_wire", "Guided pipeline wiring confirmed."),
      ]),
    ).toBe("Guided pipeline wiring confirmed.");
  });

  it("ignores unsummarised records and returns null when the stage has none", () => {
    expect(guidedDecisionRecord("step_4_wire", [record("step_4_wire", null)])).toBe(
      null,
    );
    expect(guidedDecisionRecord("step_4_wire", [])).toBe(null);
  });

  it("gives the component stages no record at all", () => {
    // Their rows ARE the decision; the generic per-turn summary the server
    // writes for them is protocol register, not decision copy.
    const history = [
      record("step_1_source", "Structured guided response accepted."),
      record("step_2_sink", "Structured guided response accepted."),
      record("step_3_transforms", "Guided pipeline proposal advanced to wiring review."),
    ];
    expect(guidedDecisionRecord("step_1_source", history)).toBe(null);
    expect(guidedDecisionRecord("step_2_sink", history)).toBe(null);
    expect(guidedDecisionRecord("step_3_transforms", history)).toBe(null);
  });
});
