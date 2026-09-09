// Unit coverage for the goal-first tutorial planner shape (tutorial-planner-shape.ts).
//
// The staging walk it grades needs a deployed build and a live provider, so the
// predicate itself is pinned here: the passing goal-first shape, and one red per
// way the shape can break — a second planner run (the step-3 Send that 2.1
// removes), a run before the step-2 finish (the pre-Send auto-proposal 2.2
// removes), a run that hands back something other than the proposal, no run at
// all, a stray planner call outside the run, and an unreadable ledger.

import { describe, expect, it } from "vitest";

import {
  ledgerTotals,
  transitionViolations,
  TRANSITION_LEDGER_SCHEMA,
  type TransitionEvidence,
  type TransitionLedger,
  type TransitionLedgerEntry,
} from "./transition-ledger";
import { tutorialPlannerShapeViolations } from "./tutorial-planner-shape";

function evidence(overrides: Partial<TransitionEvidence> = {}): TransitionEvidence {
  return {
    status: "complete",
    reason: null,
    provider_calls: 0,
    planner_calls: 0,
    planner_runs: 0,
    failed_calls: 0,
    model_latency_ms: 0,
    attempt_phases: [],
    row_ids: [],
    includes_rows_from_unavailable_transition: false,
    ...overrides,
  };
}

interface EntrySpec {
  endpoint: TransitionLedgerEntry["endpoint"];
  gesture: string;
  nextTurn?: string | null;
  newTurn?: boolean;
  evidence?: TransitionEvidence;
}

function entryOf(spec: EntrySpec, ordinal: number): TransitionLedgerEntry {
  const base: Omit<TransitionLedgerEntry, "violations"> = {
    ordinal,
    endpoint: spec.endpoint,
    gesture: spec.gesture,
    gestures: [{ label: spec.gesture, at_ms: ordinal * 10_000 }],
    gesture_count: 1,
    phase_before: null,
    request: { start_profile: null, respond_shape: null, control_signal: null, chat_message_chars: null },
    response: {
      status: 200,
      step_after: null,
      next_turn_type: spec.nextTurn ?? null,
      new_turn_occurrence: spec.newTurn ?? spec.nextTurn === "propose_pipeline",
      terminal: null,
      assistant_message_kind: null,
      run_id: null,
    },
    requested_at_ms: ordinal * 10_000 + 100,
    responded_at_ms: ordinal * 10_000 + 2_100,
    wall_clock_ms: 2_000,
    since_previous_ms: null,
    error: null,
    evidence: spec.evidence ?? evidence(),
  };
  return { ...base, violations: transitionViolations(base) };
}

/** A ledger whose totals are DERIVED from its entries, as the recorder derives
 *  them — a hand-written total could pass a check the real walk would fail. */
function ledgerOf(specs: EntrySpec[]): TransitionLedger {
  const entries = specs.map((spec, index) => entryOf(spec, index + 1));
  return {
    schema: TRANSITION_LEDGER_SCHEMA,
    deployment: { bundle: "/assets/index-goalfirst.js", legacy_auto_run: false },
    session_id: "0f6f0b8e-1f2a-4c3d-9e8f-7a6b5c4d3e2f",
    entries,
    post_gestures: [],
    final_read: { status: "complete", reason: null },
    in_flight_at_finalize: 0,
    totals: ledgerTotals(entries, [], []),
    violations: [],
  };
}

/** The one planner run: the step-2 finish, handing back the proposal. */
const PLANNER_RUN: EntrySpec = {
  endpoint: "guided/respond",
  gesture: "Finish outputs",
  nextTurn: "propose_pipeline",
  evidence: evidence({
    provider_calls: 2,
    planner_calls: 2,
    planner_runs: 1,
    model_latency_ms: 60_000,
    attempt_phases: ["discovery", "candidate"],
    row_ids: ["p1", "p2", "p3", "p4"],
  }),
};

/** The goal-first walk: T1 start (goal + ack seeded, no provider call) through
 *  the run, with the single planner run at "Finish outputs". */
function goalFirstWalk(): EntrySpec[] {
  return [
    { endpoint: "guided/start", gesture: "Let's go", nextTurn: "single_select" },
    {
      endpoint: "guided/chat",
      gesture: "Send",
      nextTurn: "schema_form",
      evidence: evidence({ provider_calls: 1, model_latency_ms: 4_000, row_ids: ["c1"] }),
    },
    { endpoint: "guided/respond", gesture: "Continue", nextTurn: "inspect_and_confirm" },
    { endpoint: "guided/respond", gesture: "Looks right", nextTurn: "review_components" },
    { endpoint: "guided/respond", gesture: "Finish sources", nextTurn: "single_select" },
    {
      endpoint: "guided/chat",
      gesture: "Send",
      nextTurn: "schema_form",
      evidence: evidence({ provider_calls: 1, model_latency_ms: 4_000, row_ids: ["c2"] }),
    },
    { endpoint: "guided/respond", gesture: "Continue", nextTurn: "multi_select_with_custom" },
    {
      endpoint: "guided/respond",
      gesture: "Let source decide (pass all fields through)",
      nextTurn: "review_components",
    },
    PLANNER_RUN,
    { endpoint: "guided/respond", gesture: "Review wiring", nextTurn: "confirm_wiring" },
    { endpoint: "guided/respond", gesture: "Confirm wiring", nextTurn: null },
    { endpoint: "tutorial/run", gesture: "Run" },
  ];
}

describe("tutorialPlannerShapeViolations", () => {
  it("accepts the goal-first walk: one planner run, at the step-2 finish, handing back the proposal", () => {
    expect(tutorialPlannerShapeViolations(ledgerOf(goalFirstWalk()))).toEqual([]);
  });

  it("rejects a second planner run on a step-3 Send (the turn 2.1 removes)", () => {
    const walk = goalFirstWalk();
    walk.splice(9, 0, {
      endpoint: "guided/chat",
      gesture: "Send",
      nextTurn: "propose_pipeline",
      evidence: evidence({
        provider_calls: 2,
        planner_calls: 2,
        planner_runs: 1,
        row_ids: ["r1", "r2"],
      }),
    });

    const violations = tutorialPlannerShapeViolations(ledgerOf(walk));

    expect(violations).toHaveLength(2);
    expect(violations[0]).toContain("exactly one planner run; the ledger totals 2");
    expect(violations[1]).toContain("2 transitions opened a planner run");
    expect(violations[1]).toContain("gesture Finish outputs");
    expect(violations[1]).toContain("guided/chat, gesture Send");
  });

  it("rejects a planner run that fires before the step-2 finish (the auto-proposal 2.2 removes)", () => {
    const walk = goalFirstWalk();
    walk[7] = { ...walk[7], nextTurn: "propose_pipeline", evidence: PLANNER_RUN.evidence };
    walk[8] = { endpoint: "guided/respond", gesture: "Finish outputs", nextTurn: "review_components" };

    const violations = tutorialPlannerShapeViolations(ledgerOf(walk));

    expect(violations).toEqual([
      expect.stringContaining('the planner run must be the learner\'s "Finish outputs" gesture'),
    ]);
    expect(violations[0]).toContain("Let source decide (pass all fields through)");
  });

  it("rejects a planner run whose response does not hand back the proposal", () => {
    const walk = goalFirstWalk();
    walk[8] = { ...PLANNER_RUN, nextTurn: "review_components", newTurn: true };

    const violations = tutorialPlannerShapeViolations(ledgerOf(walk));

    expect(violations).toEqual([
      expect.stringContaining("must hand back a propose_pipeline turn"),
    ]);
    expect(violations[0]).toContain("next turn review_components");
  });

  it("rejects a walk that planned nothing at all", () => {
    const walk = goalFirstWalk();
    walk[8] = { endpoint: "guided/respond", gesture: "Finish outputs", nextTurn: "propose_pipeline" };

    const violations = tutorialPlannerShapeViolations(ledgerOf(walk));

    expect(violations).toHaveLength(2);
    expect(violations[0]).toContain("exactly one planner run; the ledger totals 0");
    expect(violations[1]).toContain("no transition was attributed a planner run");
  });

  it("rejects a planner call attributed outside the one run, and names a shifted attribution", () => {
    const walk = goalFirstWalk();
    walk[9] = {
      ...walk[9],
      evidence: evidence({
        provider_calls: 1,
        planner_calls: 1,
        planner_runs: 0,
        row_ids: ["stray"],
        includes_rows_from_unavailable_transition: true,
      }),
    };

    const violations = tutorialPlannerShapeViolations(ledgerOf(walk));

    expect(violations).toEqual([
      expect.stringContaining("paid 1 planner provider call(s) outside the one planner run"),
    ]);
    expect(violations[0]).toContain("gesture Review wiring");
    expect(violations[0]).toContain("attribution may have shifted here");
  });

  it("treats an unreadable ledger as a failure, not a vacuous pass", () => {
    expect(tutorialPlannerShapeViolations(null, "durable read failed: 503")).toEqual([
      "per-transition ledger unavailable, so the tutorial planner shape could not be checked: durable read failed: 503",
    ]);
    expect(tutorialPlannerShapeViolations(null)).toEqual([
      "per-transition ledger unavailable, so the tutorial planner shape could not be checked: unknown",
    ]);
  });
});
