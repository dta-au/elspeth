// Goal-first tutorial walk: the planner-shape expectations (B-2.1/2.2).
//
// After the goal-first change the tutorial walk has exactly ONE planner run,
// and it happens on the step-2 finish ("Finish outputs"), whose response hands
// the learner the proposal. The step-3 Send is gone from the default walk, so
// there is no second, driver-requested run, and 2.2 removes the run that used
// to fire from a server-authored fallback intent with no intent behind it.
//
// This module states that shape as a pure predicate over the per-transition
// ledger, so the staging spec's assertion can be unit-tested without a browser
// or a live provider (the walk itself only runs against a deployed build).
// It is also the SAFETY NET that replaces the drivers' send-first guard: the
// guard existed to stop the driver accepting a proposal no Send had paid for,
// and this check fails the run outright if a proposal is ever planned anywhere
// but the step-2 finish — or planned more than once.
//
// It does not restate what the ledger and the per-walk efficiency gate already
// check (a fresh proposal that paid no planner call; call counts, phases and
// repair rounds); it adds the per-TRANSITION placement neither of those sees.
//
// Pure module: no Playwright, no I/O. Unit-tested in tutorial-planner-shape.test.ts.

import type { TransitionLedger, TransitionLedgerEntry } from "./transition-ledger";

/** The learner gesture that must carry the walk's one planner run. */
export const PLANNER_RUN_GESTURE = "Finish outputs";

/** The turn that run must hand back. */
export const PLANNER_RUN_NEXT_TURN = "propose_pipeline";

function entryLabel(entry: TransitionLedgerEntry): string {
  const gestures = entry.gestures.map((gesture) => gesture.label).join(" → ") || "none";
  return `transition ${entry.ordinal} (${entry.endpoint}, gesture ${entry.gesture ?? "-"}, gestures ${gestures}, next turn ${entry.response.next_turn_type ?? "-"})`;
}

/** Attribution caveat worth naming in a violation: rows the server wrote for an
 *  earlier transition whose durable read failed are first counted on a later
 *  one, so a planner call on this line may belong to that earlier transition.
 *  The ledger raises the failed read as its own violation; naming it here keeps
 *  the message from reading as a second, independent defect. */
function attributionCaveat(entry: TransitionLedgerEntry): string {
  return entry.evidence.includes_rows_from_unavailable_transition
    ? " (its evidence includes rows first seen after an unavailable read, so the attribution may have shifted here)"
    : "";
}

/**
 * The goal-first tutorial walk's planner shape, as violation strings.
 *
 * Empty means: exactly one planner run in the whole walk; that run on the
 * "Finish outputs" transition, whose next turn is the proposal; and no other
 * transition paying any planner call.
 *
 * A missing ledger is a violation, never a vacuous pass — the check has to be
 * able to say it could not run.
 */
export function tutorialPlannerShapeViolations(
  ledger: TransitionLedger | null,
  unavailableReason: string | null = null,
): string[] {
  if (ledger === null) {
    return [
      `per-transition ledger unavailable, so the tutorial planner shape could not be checked: ${unavailableReason ?? "unknown"}`,
    ];
  }
  const violations: string[] = [];
  if (ledger.totals.planner_runs !== 1) {
    violations.push(
      `the goal-first tutorial walk must open exactly one planner run; the ledger totals ${ledger.totals.planner_runs}`,
    );
  }
  const planning = ledger.entries.filter((entry) => entry.evidence.planner_runs > 0);
  if (planning.length === 0) {
    violations.push(
      `no transition was attributed a planner run; the walk's one run belongs to the learner's "${PLANNER_RUN_GESTURE}" gesture`,
    );
  } else if (planning.length > 1) {
    // Which one is "the" run is ambiguous here, so report them all rather than
    // grading one of them against the expected identity.
    violations.push(
      `${planning.length} transitions opened a planner run: ${planning
        .map((entry) => `${entryLabel(entry)}${attributionCaveat(entry)}`)
        .join("; ")}`,
    );
  } else {
    const [run] = planning;
    if (run.evidence.planner_runs > 1) {
      violations.push(
        `${entryLabel(run)} opened ${run.evidence.planner_runs} planner runs${attributionCaveat(run)}`,
      );
    }
    if (run.gesture !== PLANNER_RUN_GESTURE) {
      violations.push(
        `the planner run must be the learner's "${PLANNER_RUN_GESTURE}" gesture, not ${entryLabel(run)}`,
      );
    }
    if (run.response.next_turn_type !== PLANNER_RUN_NEXT_TURN) {
      violations.push(
        `the planner run must hand back a ${PLANNER_RUN_NEXT_TURN} turn; ${entryLabel(run)}`,
      );
    }
  }
  for (const entry of ledger.entries) {
    if (entry.evidence.planner_runs === 0 && entry.evidence.planner_calls > 0) {
      violations.push(
        `${entryLabel(entry)} paid ${entry.evidence.planner_calls} planner provider call(s) outside the one planner run${attributionCaveat(entry)}`,
      );
    }
  }
  return violations;
}
