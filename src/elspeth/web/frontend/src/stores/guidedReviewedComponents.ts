// src/stores/guidedReviewedComponents.ts
//
// Reviewed-components ledger for the guided build (elspeth-9f0873426a IA-1,
// server-projected by elspeth-f2a8550b3d).
//
// WHAT CHANGED: this module used to FOLD the ledger client-side, replacing a
// kind wholesale every time a `review_components` turn was published. That
// fold was the frontend's only memory of the settled components, and it was
// reconstructed rather than authoritative — the guided wire carried the
// CURRENT turn only, so a browser reload mid-step-2 started the ledger empty,
// and a completed session (whose `next_turn` is `null`) could never have one
// at all. The graduation view selects exactly such a session.
//
// The server now projects `guided_session.reviewed_components` on every
// guided response, so this module holds no derivation of its own: it reads
// the wire field, and that field is the one authority for "what has been
// settled so far". The fold is gone rather than kept beside it — two
// derivations of one fact is how the stale-ledger bug arose.
//
// The ledger stays display data: nothing here reads source rows (epic
// elspeth-e7757e5c58 D1) and nothing here authors structure (AGENTS.md
// composer invariant 1) — the right-pane graph draws a reviewed component as
// a lone node, never joined by an edge the planner has not proposed.

import type { GuidedReviewedComponents, GuidedSession } from "@/types/guided";

export type { GuidedReviewedComponents } from "@/types/guided";

export const EMPTY_GUIDED_REVIEWED_COMPONENTS: GuidedReviewedComponents = {
  sources: [],
  outputs: [],
};

/**
 * The ledger the server projected for this session, or the empty ledger when
 * there is no guided session in the store (freeform, or before the first
 * fetch). Returns the wire object itself — a stable reference for the life of
 * the published session — and the module-level empty constant otherwise, so
 * store subscribers keyed on reference equality do not re-render.
 */
export function selectGuidedReviewedComponents(
  session: GuidedSession | null,
): GuidedReviewedComponents {
  if (session === null) return EMPTY_GUIDED_REVIEWED_COMPONENTS;
  return session.reviewed_components;
}
