// src/stores/guidedReviewedComponents.test.ts
//
// The reviewed-components ledger names WHICH source and output the learner
// has already confirmed; the right-pane graph draws from it between the
// source review and the proposal (elspeth-9f0873426a, IA-1).
//
// It used to be FOLDED here from every published review_components turn.
// That fold is retired (elspeth-f2a8550b3d): the server projects the ledger
// on `guided_session.reviewed_components`, and this module only reads it.
// These tests are the fold suite's behavioural coverage carried over to the
// selector — a reviewed source is in the ledger, the other kind is
// independent, a re-review replaces a kind wholesale, and the reference is
// stable — plus the two cases the fold could never satisfy, which are the
// reason the projection exists: a mid-build reload and a completed session.
//
// There is deliberately NO second derivation to test against. The fold is
// gone rather than kept beside the selector: two derivations of "what has
// been settled" is what let the ledger go stale in the first place.

import { describe, expect, it } from "vitest";

import type { ComponentReviewItem, GuidedSession } from "@/types/guided";

import * as ledgerModule from "./guidedReviewedComponents";
import {
  EMPTY_GUIDED_REVIEWED_COMPONENTS,
  selectGuidedReviewedComponents,
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

function session(overrides: Partial<GuidedSession> = {}): GuidedSession {
  return {
    step: "step_2_sink",
    history: [],
    terminal: null,
    chat_history: [],
    chat_turn_seq: 0,
    reviewed_components: { sources: [], outputs: [] },
    profile: null,
    ...overrides,
  };
}

describe("selectGuidedReviewedComponents", () => {
  it("starts empty", () => {
    expect(EMPTY_GUIDED_REVIEWED_COMPONENTS).toEqual({ sources: [], outputs: [] });
  });

  it("has no client-side fold left to disagree with", () => {
    // The retirement is the point: one derivation of the ledger exists, and
    // it is the server's. A re-introduced fold would show up here.
    expect(ledgerModule).not.toHaveProperty("foldGuidedReviewedComponents");
    expect(Object.keys(ledgerModule).sort()).toEqual([
      "EMPTY_GUIDED_REVIEWED_COMPONENTS",
      "selectGuidedReviewedComponents",
    ]);
  });

  it("reads the reviewed sources the server projected", () => {
    const selected = selectGuidedReviewedComponents(
      session({ reviewed_components: { sources: [SOURCE], outputs: [] } }),
    );
    expect(selected).toEqual({ sources: [SOURCE], outputs: [] });
  });

  it("carries both kinds independently", () => {
    const selected = selectGuidedReviewedComponents(
      session({ reviewed_components: { sources: [SOURCE], outputs: [OUTPUT] } }),
    );
    expect(selected).toEqual({ sources: [SOURCE], outputs: [OUTPUT] });
  });

  it("follows the server when a kind is re-reviewed wholesale", () => {
    const replacement: ComponentReviewItem = {
      ...SOURCE,
      stable_id: "00000000-0000-4000-8000-000000000699",
      name: "source-2",
      plugin: "json",
    };
    const before = session({
      reviewed_components: { sources: [SOURCE], outputs: [OUTPUT] },
    });
    const after = session({
      reviewed_components: { sources: [replacement], outputs: [OUTPUT] },
    });

    expect(selectGuidedReviewedComponents(before).sources).toEqual([SOURCE]);
    expect(selectGuidedReviewedComponents(after).sources).toEqual([replacement]);
    expect(selectGuidedReviewedComponents(after).outputs).toEqual([OUTPUT]);
  });

  it("serves a mid-build session with no current review turn", () => {
    // The fold's known limit, now fixed: at step 3 the wire carries a
    // proposal, not a review turn, and a reload had nothing to fold — so the
    // pane forgot the source the learner had confirmed.
    const selected = selectGuidedReviewedComponents(
      session({
        step: "step_3_transforms",
        reviewed_components: { sources: [SOURCE], outputs: [OUTPUT] },
      }),
    );
    expect(selected).toEqual({ sources: [SOURCE], outputs: [OUTPUT] });
  });

  it("serves a completed session, which has no turn at all", () => {
    const selected = selectGuidedReviewedComponents(
      session({
        step: "step_4_wire",
        terminal: { kind: "completed", reason: null, pipeline_yaml: "sources: {}\n" },
        reviewed_components: { sources: [SOURCE], outputs: [OUTPUT] },
      }),
    );
    expect(selected).toEqual({ sources: [SOURCE], outputs: [OUTPUT] });
  });

  it("returns the empty ledger with no guided session, by reference", () => {
    // Reference stability matters: store subscribers key on it. Both branches
    // return an object the caller did not build — the wire field itself, or
    // the module constant — so no consumer re-renders on an identical read.
    expect(selectGuidedReviewedComponents(null)).toBe(EMPTY_GUIDED_REVIEWED_COMPONENTS);
    const live = session({ reviewed_components: { sources: [SOURCE], outputs: [] } });
    expect(selectGuidedReviewedComponents(live)).toBe(live.reviewed_components);
  });
});
