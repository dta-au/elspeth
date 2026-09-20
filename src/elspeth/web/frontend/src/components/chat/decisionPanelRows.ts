// ============================================================================
// decisionPanelRows.ts — pure projection for the chat's DecisionPanel
// (elspeth-cb0d4b8dba).
//
// The panel is the one place that answers "why can't I proceed, and what can
// I do about it". This module turns the stores' existing facts into a flat,
// ordered list of rows with a CLOSED kind union, and no DOM, so the panel is
// a dumb render and phase 2 (migrating the interpretation-review cards and
// the proposal accept/reject in full) is a row-kind addition rather than a
// rewrite.
//
// Sources, none of them new:
//   * executionStore.validationResult.readiness — the durable completion
//     gate the server re-emits on every POST /validate (blockers + axes);
//   * sessionStore.compositionState.validation_suggestions — the validator's
//     optional-improvement nudges (restored by the backend from current validation
//     on reload as well as live compose/validate responses);
//   * interpretationEventsStore.pendingBySession — pending review cards;
//   * sessionStore.compositionProposals / staleProposalIds — pending
//     proposals, filtered by the SAME predicate the banner renders from.
//
// Rules:
//   * Which verb is blocked is derived from the readiness AXES
//     (execution_ready / completion_ready), never from blocker codes, so a
//     new backend code needs no client change to name what it blocks.
//   * Suggestions appear ONLY while something is blocked. S1 ("no retaining
//     error routing") fires on nearly every default-routed pipeline since
//     a9887ded9; promoting it to the chat permanently would be a nag.
//   * The interpretation_review_pending blocker and the pending review
//     cards describe one fact; when cards exist the blocker collapses into
//     pointer rows, otherwise it stays (an orphaned or not-yet-loaded review
//     must not vanish for want of a pointer).
// ============================================================================

import { actionableProposals } from "./actionableProposals";
import type {
  CompositionProposal,
  CompositionState,
  ValidationEntryDTO,
  ValidationReadiness,
  ValidationResult,
} from "@/types/index";
import type { InterpretationEvent } from "@/types/interpretation";

/** Blocker code the backend emits while a review card awaits the user
 *  (`INTERPRETATION_REVIEW_PENDING_CODE`, web/interpretation_state.py). The
 *  one code this projection reads, and only to deduplicate against the
 *  pending cards that carry the same fact. */
export const INTERPRETATION_REVIEW_PENDING_CODE = "interpretation_review_pending";

export type BlockedVerb = "run" | "save_for_review";

export type DecisionRow =
  | { kind: "inline_source_fallback"; id: string; candidateText: string }
  | {
      kind: "blocker";
      id: string;
      code: string;
      detail: string;
      suggestion: string | null;
    }
  | {
      kind: "suggestion";
      id: string;
      suggestion: ValidationEntryDTO;
    }
  | {
      kind: "pending_interpretation";
      id: string;
      eventId: string;
      userTerm: string | null;
      affectedNodeId: string | null;
    }
  | {
      kind: "pending_proposal";
      id: string;
      proposalId: string;
    };

export interface DecisionRowsInput {
  inlineSourceCandidate?: string | null;
  validationResult: ValidationResult | null;
  compositionState: CompositionState | null;
  pendingInterpretations: readonly InterpretationEvent[];
  proposals: readonly CompositionProposal[];
  staleProposalIds: readonly string[];
}

export interface DecisionRows {
  rows: DecisionRow[];
  /** Verbs the readiness axes withhold, in bar order (Run, then Save). */
  blockedVerbs: BlockedVerb[];
  /** Total rows: the panel's heading count and live-region figure. */
  count: number;
}

/** Which action-bar verbs the readiness axes withhold. */
export function blockedVerbsFromReadiness(
  readiness: ValidationReadiness,
): BlockedVerb[] {
  const verbs: BlockedVerb[] = [];
  if (!readiness.execution_ready) verbs.push("run");
  if (!readiness.completion_ready) verbs.push("save_for_review");
  return verbs;
}

export function projectDecisionRows(input: DecisionRowsInput): DecisionRows {
  const {
    validationResult,
    compositionState,
    pendingInterpretations,
    proposals,
    staleProposalIds,
  } = input;

  const blockedVerbs =
    validationResult === null
      ? []
      : blockedVerbsFromReadiness(validationResult.readiness);
  const isBlocked = blockedVerbs.length > 0;

  const pointerRows: DecisionRow[] = pendingInterpretations.map((event) => ({
    kind: "pending_interpretation",
    id: `interpretation:${event.id}`,
    eventId: event.id,
    userTerm: event.user_term,
    affectedNodeId: event.affected_node_id,
  }));

  // A duplicate occurrence gets its own identity without tying distinct
  // decisions to array positions (reordering must not announce new work).
  const occurrences = new Map<string, number>();
  const decisionId = (kind: string, values: readonly unknown[]): string => {
    const key = `${kind}:${JSON.stringify(values)}`;
    const occurrence = occurrences.get(key) ?? 0;
    occurrences.set(key, occurrence + 1);
    return `${key}:${occurrence}`;
  };
  const blockerRows: DecisionRow[] = [];
  if (validationResult !== null && isBlocked) {
    for (const blocker of validationResult.readiness.blockers) {
      if (
        blocker.code === INTERPRETATION_REVIEW_PENDING_CODE &&
        pointerRows.length > 0
      ) {
        continue;
      }
      blockerRows.push({
        kind: "blocker",
        id: decisionId("blocker", [blocker.code, blocker.component_id, blocker.detail, blocker.suggestion]),
        code: blocker.code,
        detail: blocker.detail,
        suggestion: blocker.suggestion,
      });
    }
  }

  const suggestionRows: DecisionRow[] = [];
  if (isBlocked && compositionState !== null) {
    (compositionState.validation_suggestions ?? []).forEach((suggestion) => {
      suggestionRows.push({
        kind: "suggestion",
        id: decisionId("suggestion", [suggestion.component, suggestion.message, suggestion.severity]),
        suggestion,
      });
    });
  }

  const proposalRows: DecisionRow[] = actionableProposals(
    proposals,
    staleProposalIds,
  ).map((proposal) => ({
    kind: "pending_proposal",
    id: `proposal:${proposal.id}`,
    proposalId: proposal.id,
  }));

  const fallbackRows: DecisionRow[] = input.inlineSourceCandidate == null ? [] : [{
    kind: "inline_source_fallback",
    id: `inline-source:${input.inlineSourceCandidate}`,
    candidateText: input.inlineSourceCandidate,
  }];
  const rows = [...blockerRows, ...suggestionRows, ...pointerRows, ...proposalRows, ...fallbackRows];
  return { rows, blockedVerbs, count: rows.length };
}
