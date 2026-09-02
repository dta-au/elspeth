// src/stores/guidedReviewedComponents.ts
//
// Reviewed-components ledger for the guided build (elspeth-9f0873426a, IA-1).
//
// The guided wire carries the CURRENT turn only, and the pre-commit
// composition state is empty by design until Confirm wiring. Between the
// moment the learner finishes reviewing the source (step 1) and the moment a
// proposal arrives (step 3) nothing on the wire names the components already
// agreed — GuidedSession.history holds payload hashes and prose summaries.
// This ledger is the frontend's only memory of them: it folds every
// review_components turn as it is published, so the right-pane graph can draw
// the reviewed source (and later the reviewed output) while the learner is
// still deciding. It is server-derived display data — nothing here reads
// source rows (epic elspeth-e7757e5c58 D1) and nothing here authors structure
// (AGENTS.md composer invariant 1): a reviewed component is drawn as a lone
// node, never joined by an edge the planner has not proposed.
//
// Semantics are deliberately narrow: a review turn REPLACES the kind it
// reviews wholesale (the turn's `items` is the server's complete current list
// for that kind), leaves the other kind alone, and every other turn is an
// identity fold. The ledger resets with the rest of the guided context on
// session navigation (sessionStore clearedGuidedState). Known limit: a
// browser reload mid-step-2 starts the ledger empty, so the source is drawn
// again only once the proposal names it.

import type { ComponentReviewItem, TurnPayload } from "@/types/guided";

export interface GuidedReviewedComponents {
  readonly sources: readonly ComponentReviewItem[];
  readonly outputs: readonly ComponentReviewItem[];
}

export const EMPTY_GUIDED_REVIEWED_COMPONENTS: GuidedReviewedComponents = {
  sources: [],
  outputs: [],
};

/**
 * Fold one published turn into the ledger. Returns the SAME ledger object for
 * every non-review turn (and for no turn) so store subscribers keyed on
 * reference equality do not re-render.
 */
export function foldGuidedReviewedComponents(
  ledger: GuidedReviewedComponents,
  turn: TurnPayload | null,
): GuidedReviewedComponents {
  if (turn === null || turn.type !== "review_components") return ledger;
  const { component_kind, items } = turn.payload;
  switch (component_kind) {
    case "source":
      return { sources: items, outputs: ledger.outputs };
    case "output":
      return { sources: ledger.sources, outputs: items };
  }
}
