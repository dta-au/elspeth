// src/components/chat/guided/guidedDecisionStages.ts
//
// The ONE derivation behind the guided stepper's "reviewed" ticks and the
// decision sheets they open (elspeth-f2a8550b3d, slice E first landing).
//
// WHY THIS IS NOT AN INDEX COMPARISON. The stepper used to read a tick as
// settled when its index sat below the current step. That is a claim about
// WALK POSITION, and the guided build is about to stop being a one-way walk:
// once a learner can return to a reviewed source, the Output stage they
// already settled sits DOWNSTREAM of the current step and an index rule calls
// it "not started" — a lie about work the server has on record. So the rule
// asks the server's own ledger instead: a stage is settled when
// `guided_session.reviewed_components` (or, for the two stages that settle no
// components, the session's own position) says a decision was recorded there.
//
// The ledger read is deliberately `guidedSession.reviewed_components` rather
// than the store's `guidedReviewedComponents` copy. They agree everywhere
// except the refresh-required path, where the store empties its copy so the
// right-pane graph stops drawing pre-failure nodes (sessionStore.ts). That
// divergence is about the GRAPH's authority over a view it can no longer
// refresh; it is not a statement that the server forgot what was settled, and
// a stepper that forgot with it would tell the user their finished stages had
// never happened. That choice is PINNED where the two are on screen together:
// ChatPanel.test.tsx, "keeps the ticks settled from the published session when
// the store's graph ledger has been emptied" — at step_3_transforms the guided
// surface still renders with a null turn, which is what makes the divergence
// reachable rather than theoretical.
//
// PURE LEAF: no React, no store, no fetch. Every consumer (ChatPanel's
// stepper, GuidedDecisionSheet's rows) binds to these functions so the tick
// that offers a sheet and the sheet's contents can never disagree about what
// "settled" means.

import { humaniseStepLabel } from "@/components/chat/interpretationStepLabel";
import type { CompositionState } from "@/types/index";
import type {
  ChatTurn,
  GuidedReviewedComponents,
  GuidedStep,
  WireStageData,
} from "@/types/guided";

/** A node exactly as the step-4 wire card received it. */
type WireStageNode = WireStageData["nodes"][number];

/**
 * One settled component on a decision sheet. Identity + display only: `key` is
 * a React key, never rendered — the sheets must not print a stable_id (the
 * default-DOM identifier rule) — and `plugin` is the raw plugin id, passed
 * through `pluginDisplayName` at render time and `null` on a structural node
 * that has no plugin.
 */
export interface GuidedDecisionRow {
  readonly key: string;
  readonly name: string;
  readonly plugin: string | null;
}

/**
 * The stages that hold a settled decision, so their ticks can offer a sheet.
 *
 * Per stage, and deliberately from different authorities:
 *   - Source / Output — the server-projected ledger for that kind. These are
 *     the two stages that settle COMPONENTS, so the ledger is the fact.
 *   - Transforms — settled once the session reaches Wire. The proposal is the
 *     transform decision and the wire card is what you see after accepting it;
 *     there is no per-transform ledger to consult.
 *   - Wire — settled only by the commit itself.
 *
 * A COMPLETED session reports all four regardless of the ledger. Every stage
 * of a committed pipeline is settled by construction, and a completed session
 * is exactly the surface these read-only sheets exist to serve (the
 * graduation view selects one) — deriving three of its four ticks from a
 * ledger that a projection defect could empty would silently take the story
 * away at the one moment it is entirely history.
 */
export function reviewedGuidedStages(
  reviewed: GuidedReviewedComponents,
  step: GuidedStep,
  completed: boolean,
): ReadonlySet<GuidedStep> {
  if (completed) {
    return new Set<GuidedStep>([
      "step_1_source",
      "step_2_sink",
      "step_3_transforms",
      "step_4_wire",
    ]);
  }
  const settled = new Set<GuidedStep>();
  if (reviewed.sources.length > 0) settled.add("step_1_source");
  if (reviewed.outputs.length > 0) settled.add("step_2_sink");
  if (step === "step_4_wire") settled.add("step_3_transforms");
  return settled;
}

/**
 * The components a stage settled, in authored order.
 *
 * Source/Output come from the server ledger. Transforms come from whichever
 * surface currently HOLDS the accepted graph: mid-walk that is the step-4 wire
 * card's own nodes (so the sheet and the card above it name the same things
 * the same way), and after the commit it is `compositionState.nodes`, whose
 * ids are humanised through the app's single node-name choke point rather than
 * printed raw. Wire settles no components — its record is the confirmation,
 * which the sheet takes separately.
 */
export function guidedDecisionRows(
  stage: GuidedStep,
  reviewed: GuidedReviewedComponents,
  wireNodes: readonly WireStageNode[],
  composition: CompositionState | null,
  completed: boolean,
): readonly GuidedDecisionRow[] {
  switch (stage) {
    case "step_1_source":
      return reviewed.sources.map((item) => ({
        key: item.stable_id,
        name: item.name,
        plugin: item.plugin,
      }));
    case "step_2_sink":
      return reviewed.outputs.map((item) => ({
        key: item.stable_id,
        name: item.name,
        plugin: item.plugin,
      }));
    case "step_3_transforms":
      if (completed) {
        if (composition === null) return [];
        return composition.nodes.map((node) => ({
          key: node.id,
          // Never the raw id: humaniseStepLabel is the app's single node-name
          // choke point, and every node here is present in the composition it
          // is resolved against, so its "Removed" arm is unreachable.
          name: humaniseStepLabel(composition, node.id),
          plugin: node.plugin,
        }));
      }
      return wireNodes.map((node) => ({
        key: node.stable_id,
        name: node.label,
        plugin: node.plugin,
      }));
    case "step_4_wire":
      return [];
  }
}

/**
 * The seq of the first POST-COMMIT chat turn, or null when this transcript has
 * none.
 *
 * Post-commit questions are persisted with `step="step_4_wire"` — identical to
 * the pre-commit wire turns (see GuidedChatHistory's own note) — so a Wire
 * sheet that filtered on `step` alone would replay the entire post-commit
 * advisory conversation as part of the wiring decision, on exactly the
 * completed sessions these sheets serve. The boundary is the transcript's own
 * record of where the build ended: every post-commit turn is submitted under
 * the confirmation hash, so the FIRST user turn carrying it opens the
 * after-build conversation. Same derivation the transcript's "After
 * confirmation" divider uses (completedChatToken.afterConfirmationChatToken).
 */
function postCommitBoundarySeq(
  chatHistory: readonly ChatTurn[],
  afterConfirmationToken: string | null,
): number | null {
  if (afterConfirmationToken === null) return null;
  let boundary: number | null = null;
  for (const turn of chatHistory) {
    if (turn.role !== "user") continue;
    if (turn.turn_token !== afterConfirmationToken) continue;
    if (boundary === null || turn.seq < boundary) boundary = turn.seq;
  }
  return boundary;
}

/**
 * The stage's own chat turns, in seq order, with the post-build conversation
 * excluded — on a COMPLETED session.
 *
 * The exclusion is applied to EVERY stage rather than special-cased on Wire:
 * post-commit turns all carry `step_4_wire` today, so only the Wire sheet can
 * see them, but a sheet that filtered by stage alone would silently start
 * replaying them the day that changes.
 *
 * `completed` is what stops that generality from misfiring on a live session.
 * The boundary comes from `afterConfirmationChatToken`, which is deliberately a
 * question about the TRANSCRIPT rather than the channel: a session that
 * confirmed a build and was then re-entered on changed content is rewound to
 * Step 2 with `terminal=None` while keeping its chat history and its answered
 * `confirm_wiring` record, so the token — and with it the boundary — survives
 * onto a session that is live again (`/guided/reenter`, guided.py; the same
 * note on `completedChatToken.afterConfirmationChatToken` records it for a
 * fork). Every turn the learner then types at the rewound stage sits ABOVE
 * that boundary, so an unconditional filter would drop the whole live
 * conversation and replay only the superseded pre-commit one under a heading
 * that says the stage is decided.
 *
 * A completed session is exactly the set on which every post-boundary turn IS
 * post-commit, so gating on it removes that false drop without reintroducing
 * the per-stage special case. It costs the Wire sheet nothing: `step_4_wire`
 * is a settled stage only on a completed session (`reviewedGuidedStages`
 * adds it in the completed arm alone), so no live sheet can reach the
 * post-commit turns this boundary exists to hide.
 */
export function guidedDecisionTurns(
  stage: GuidedStep,
  chatHistory: readonly ChatTurn[],
  afterConfirmationToken: string | null,
  completed: boolean,
): ChatTurn[] {
  const boundary = completed
    ? postCommitBoundarySeq(chatHistory, afterConfirmationToken)
    : null;
  return chatHistory
    .filter(
      (turn) =>
        turn.step === stage && (boundary === null || turn.seq < boundary),
    )
    .sort((a, b) => a.seq - b.seq);
}

/**
 * The stage's own decision record — the server's summary for the LATEST
 * summarised turn recorded at that step — or null when the stage has none.
 *
 * ONLY the wire stage ever yields one, and that restriction lives here rather
 * than at the call site so the whole rule is in one testable place. The three
 * other stages settle COMPONENTS, and their rows are the decision; their
 * per-turn summaries are protocol register ("Structured guided response
 * accepted.") and would read as noise under a heading that already said what
 * was decided. The wire stage settles routing and leaves no component rows, so
 * its confirmation record ("Guided pipeline wiring confirmed.") is the whole
 * of what it decided — projected from the session's own history rather than
 * written here, so the sheet quotes the audit trail instead of a copy of it.
 */
export function guidedDecisionRecord(
  stage: GuidedStep,
  history: readonly { step: GuidedStep; summary: string | null }[],
): string | null {
  if (stage !== "step_4_wire") return null;
  let latest: string | null = null;
  for (const record of history) {
    if (record.step !== stage) continue;
    if (record.summary === null) continue;
    latest = record.summary;
  }
  return latest;
}
