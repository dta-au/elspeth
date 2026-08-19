import { plural } from "@/utils/plural";

/**
 * Decides whether a one-click "Approve wiring" may confirm the wiring the
 * operator never opened.
 *
 * The proposal turn cannot answer this in advance: `can_confirm`, `blockers`
 * and `warnings` are computed server-side DURING the review_wiring transition
 * (`catalog.validate_composition_state`, emitters.py) and do not exist until
 * the wire turn has been emitted. So the approval is necessarily two
 * dispatches — review_wiring, then confirm_wiring built from what came back —
 * and this is the gate between them.
 *
 * MUST stay a pure leaf — no React, no store imports — because
 * `stores/sessionStore.ts` imports it. That is the same store→components edge
 * `acknowledgementLabels.ts` carries for `stores/subscriptions.ts`, and it is
 * only sound while this module pulls nothing back. Do not import a component,
 * a hook, or a store here.
 */

/** The wire turn's own verdict — structurally satisfied by `WireStageData`. */
export interface WiringApprovalSignals {
  readonly can_confirm: boolean;
  readonly blockers: readonly unknown[];
  readonly warnings: readonly unknown[];
}

/** Blockers the client knows that the wire payload does not carry: pending
 *  acknowledgement cards and the persisted composition's own validation
 *  errors. Both already disable the normal Confirm button (WireStageTurn). */
export interface WiringApprovalClientBlockers {
  readonly pendingAcknowledgements: number;
  readonly validationIssues: number;
}

/**
 * What a one-click approval did.
 *
 * `stopped` and `not_applied` are deliberately distinct: `stopped` means the
 * review dispatch succeeded and the wiring itself refused the shortcut (the
 * user is on the wire review with a reason), while `not_applied` means the
 * chain never got a wire turn to judge — a rejected dispatch, a self-heal, a
 * re-plan. Only the first is a statement about the pipeline.
 */
export type WiringApprovalOutcome =
  | { readonly status: "confirmed" }
  | { readonly status: "stopped"; readonly reason: string }
  | { readonly status: "not_applied" };

/**
 * Why a one-click approval must stop at the wire review, or null when the
 * wiring is clean enough to confirm unseen.
 *
 * Warnings stop an approval even though they deliberately do NOT disable the
 * normal Confirm button: confirming from the review screen is an operator
 * looking at the warning and accepting it, while approving unseen would
 * retire the warning without anyone reading it. The two controls differ on
 * this one axis on purpose — do not "align" them without the operator's call.
 *
 * Ordered by severity so the message names the worst thing that is true: a
 * pipeline that cannot be confirmed at all is never described as merely
 * carrying warnings.
 *
 * NOT a complete list of the server's confirm gates, and cannot be: the
 * server also 409s a confirm whose guided session has unresolved retained
 * instructions (`verified_remaining_deferred_intents`, guided.py), and that
 * is invisible here — `deferred_intents` reaches no client type or wire
 * decoder, and it is not implied by `can_confirm`, which is only
 * `validation.is_valid`. An approval in that state therefore lands on the
 * wire review with the backend's rejection banner instead of the polite
 * notice, which is the honest outcome while the fact stays server-side. If
 * a remaining-intent count ever reaches the wire, add the arm here.
 */
export function approvalStopReason(
  wiring: WiringApprovalSignals,
  clientBlockers: WiringApprovalClientBlockers,
): string | null {
  if (!wiring.can_confirm || wiring.blockers.length > 0) {
    return "Approval stopped: this wiring can't be confirmed yet. Review the problems below.";
  }
  if (clientBlockers.validationIssues > 0) {
    return "Approval stopped: the pipeline isn't ready to confirm. Review the issues below.";
  }
  if (clientBlockers.pendingAcknowledgements > 0) {
    return `Approval stopped: ${plural(
      clientBlockers.pendingAcknowledgements,
      "decision",
    )} still to acknowledge. Resolve them, then confirm below.`;
  }
  if (wiring.warnings.length > 0) {
    return `Approval stopped: the wiring came back with ${plural(
      wiring.warnings.length,
      "warning",
    )}. Review them below, then confirm.`;
  }
  return null;
}
