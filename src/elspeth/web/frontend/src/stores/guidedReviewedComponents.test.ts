// src/stores/guidedReviewedComponents.test.ts
//
// The reviewed-components ledger is the frontend's only memory of WHICH
// source and output the learner has already confirmed once the
// review_components turn that named them has advanced (the wire carries the
// current turn only; GuidedSession.history holds hashes). The right-pane
// graph draws from it between the source review and the proposal
// (elspeth-9f0873426a, IA-1), so the fold must be exact: replace the kind
// the turn reviews, leave the other kind alone, and ignore every other turn.

import { describe, expect, it } from "vitest";

import type { ComponentReviewItem, TurnPayload } from "@/types/guided";

import {
  EMPTY_GUIDED_REVIEWED_COMPONENTS,
  foldGuidedReviewedComponents,
} from "./guidedReviewedComponents";

const SOURCE: ComponentReviewItem = {
  stable_id: "00000000-0000-4000-8000-000000000602",
  name: "source-1",
  plugin: "csv",
  status: "reviewed",
};

const OUTPUT: ComponentReviewItem = {
  stable_id: "00000000-0000-4000-8000-000000000604",
  name: "output-1",
  plugin: "json",
  status: "reviewed",
};

function reviewTurn(
  component_kind: "source" | "output",
  items: ComponentReviewItem[],
): TurnPayload {
  return {
    type: "review_components",
    step_index: component_kind === "source" ? 0 : 1,
    turn_token: "a".repeat(64),
    payload: { component_kind, items, allowed_actions: ["finish"] },
  };
}

const SINGLE_SELECT: TurnPayload = {
  type: "single_select",
  step_index: 1,
  turn_token: "b".repeat(64),
  payload: {
    question: "Choose a sink",
    options: [{ id: "json", label: "JSON", hint: null }],
    allow_custom: false,
  },
};

describe("foldGuidedReviewedComponents", () => {
  it("starts empty", () => {
    expect(EMPTY_GUIDED_REVIEWED_COMPONENTS).toEqual({ sources: [], outputs: [] });
  });

  it("records the reviewed sources from a source review turn", () => {
    const folded = foldGuidedReviewedComponents(
      EMPTY_GUIDED_REVIEWED_COMPONENTS,
      reviewTurn("source", [SOURCE]),
    );
    expect(folded).toEqual({ sources: [SOURCE], outputs: [] });
  });

  it("keeps the reviewed source when the output review turn arrives", () => {
    const afterSource = foldGuidedReviewedComponents(
      EMPTY_GUIDED_REVIEWED_COMPONENTS,
      reviewTurn("source", [SOURCE]),
    );
    const afterOutput = foldGuidedReviewedComponents(
      afterSource,
      reviewTurn("output", [OUTPUT]),
    );
    expect(afterOutput).toEqual({ sources: [SOURCE], outputs: [OUTPUT] });
  });

  it("replaces a kind wholesale on a later review of the same kind", () => {
    const replacement: ComponentReviewItem = {
      ...SOURCE,
      stable_id: "00000000-0000-4000-8000-000000000699",
      name: "source-2",
      plugin: "json",
    };
    const afterFirst = foldGuidedReviewedComponents(
      EMPTY_GUIDED_REVIEWED_COMPONENTS,
      reviewTurn("source", [SOURCE]),
    );
    const afterSecond = foldGuidedReviewedComponents(
      afterFirst,
      reviewTurn("source", [replacement]),
    );
    expect(afterSecond.sources).toEqual([replacement]);
  });

  it("returns the same ledger for every non-review turn and for no turn", () => {
    const ledger = foldGuidedReviewedComponents(
      EMPTY_GUIDED_REVIEWED_COMPONENTS,
      reviewTurn("source", [SOURCE]),
    );
    expect(foldGuidedReviewedComponents(ledger, SINGLE_SELECT)).toBe(ledger);
    expect(foldGuidedReviewedComponents(ledger, null)).toBe(ledger);
  });
});
