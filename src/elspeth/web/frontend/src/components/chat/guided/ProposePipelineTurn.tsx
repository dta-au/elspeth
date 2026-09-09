import { useEffect, useId, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui";
import { stepLabelForPlugin } from "@/components/chat/interpretationStepLabel";
import { dispatchArtifactViewIntent } from "@/lib/composer-events";
import { useShowAdvanced } from "@/stores/preferencesStore";
import { useSessionStore } from "@/stores/sessionStore";

import type {
  GuidedEditTarget,
  GuidedProposalReviewState,
  GuidedProposalRetryAction,
  GuidedRespondAction,
  ProposePipelinePayload,
} from "@/types/guided";
import { behaviorSummary, gateSummary } from "./behaviorSummary";
import { flowLabel, routeKeysByAlias } from "./guidedGraphProjection";
import { NodeOptionsSummary } from "./nodeOptionDisplay";
import { optionTier } from "./optionTiers";
import { WireReviewList, type WireReviewItem } from "./WireReviewList";

interface ProposePipelineTurnProps {
  payload: ProposePipelinePayload;
  reviewState: GuidedProposalReviewState;
  onSubmit: (body: GuidedRespondAction) => void;
  /**
   * Approve the wiring without opening the wire review. Deliberately NOT an
   * `onSubmit` body: the server rejects a `confirm_wiring` from step 3
   * outright (guided.py's closed step-3 action shape), so approval is a
   * two-dispatch chain — review_wiring, then confirm_wiring built from the
   * wire turn that came back, and only if that turn is clean. The parent owns
   * that chain and the stop rule; this turn only reports the intent, carrying
   * the proposal binding the chain's first dispatch must quote. Omitted where
   * the parent cannot run the chain, and the button is not rendered.
   */
  onApproveWiring?: (binding: {
    proposal_id: string;
    draft_hash: string;
  }) => void;
  disabled?: boolean;
  isTutorial?: boolean;
}

const BLOCKER_COPY: Record<ProposePipelinePayload["blockers"][number]["code"], string> = {
  pipeline_invalid: "The proposed pipeline has validation problems that must be revised.",
  policy_review_required: "A policy review is required before this pipeline can advance.",
  plugin_unavailable: "A required plugin is unavailable and must be replaced.",
  interpretation_required: "A pending interpretation must be resolved before wiring review.",
};

function proposalBindingMatches(
  payload: ProposePipelinePayload,
  state: GuidedProposalReviewState,
): boolean {
  return state.proposal_id === payload.proposal_id && state.draft_hash === payload.draft_hash;
}

function sameRetryAction(
  retained: GuidedProposalRetryAction,
  candidate: GuidedProposalRetryAction,
): boolean {
  if (retained.kind !== candidate.kind) return false;
  if (
    retained.kind === "revise_instruction" &&
    candidate.kind === "revise_instruction"
  ) {
    return (
      retained.revision_instruction === candidate.revision_instruction &&
      retained.revision_mode === candidate.revision_mode
    );
  }
  if (retained.kind !== "revise" || candidate.kind !== "revise") return true;
  return (
    retained.edit_target.kind === candidate.edit_target.kind &&
    retained.edit_target.stable_id === candidate.edit_target.stable_id &&
    (retained.correction_feedback ?? null) === (candidate.correction_feedback ?? null)
  );
}

function reviewStatusCopy(
  state: GuidedProposalReviewState,
  isCurrentBinding: boolean,
): { role: "status" | "alert"; message: string } | null {
  if (!isCurrentBinding) {
    return {
      role: "status",
      message: "The previous proposal became stale. Review this refreshed proposal before taking action.",
    };
  }
  switch (state.status) {
    case "active":
      return null;
    case "submitting":
      return { role: "status", message: "Submitting this proposal decision…" };
    case "reloading":
      return { role: "status", message: "This proposal changed. Reloading the authoritative proposal…" };
    case "stale":
      return {
        role: "status",
        message: "This proposal is stale and cannot be used. Ask the assistant to regenerate it.",
      };
    case "error":
      return { role: "alert", message: state.message };
  }
}

/**
 * The proposal's one-line summary, DERIVED from the node count.
 *
 * It used to be the fixed sentence "A complete pipeline is ready for review."
 * That sentence was a claim the card could not support: a zero-node
 * pass-through — rows going straight from source to output, which is a
 * legitimate proposal when the goal names no processing — was announced as a
 * "complete pipeline", and so was every planner result regardless of what it
 * actually built. Reading the count the payload already carries says the true
 * thing in both cases and costs nothing (the counts strip below renders the
 * same field).
 */
function proposalHeadline(nodeCount: number): string {
  if (nodeCount === 0) {
    return "The assistant proposes no processing steps — rows pass straight from your source to your output.";
  }
  // Singular matters: the one-node case is the common shape for a simple goal,
  // and "1 processing steps" reads as a bug in the thing the user is being
  // asked to trust.
  const steps = nodeCount === 1 ? "1 processing step" : `${nodeCount} processing steps`;
  return `The assistant proposed ${steps} from your goal.`;
}

export function ProposePipelineTurn({
  payload,
  reviewState,
  onSubmit,
  onApproveWiring,
  disabled = false,
  isTutorial = false,
}: ProposePipelineTurnProps): JSX.Element {
  const showAdvanced = useShowAdvanced();
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const statusRef = useRef<HTMLParagraphElement | null>(null);
  const revisionFeedbackId = useId();
  const retainedCorrection =
    reviewState.status === "error" &&
    reviewState.retryable &&
    reviewState.retry_action.kind === "revise" &&
    reviewState.retry_action.correction_feedback !== undefined
      ? reviewState.retry_action
      : null;
  const retainedInstruction =
    reviewState.status === "error" &&
    reviewState.retryable &&
    reviewState.retry_action.kind === "revise_instruction"
      ? reviewState.retry_action
      : null;
  const [revisionTarget, setRevisionTarget] = useState<GuidedEditTarget | null>(
    retainedCorrection?.edit_target ?? null,
  );
  const [revisionFeedback, setRevisionFeedback] = useState(
    retainedCorrection?.correction_feedback ?? "",
  );
  // "Edit prompt" on an llm row (I-2) opens the SAME node-scoped revise the
  // "Revise <node>" button opens — pre-targeted — and hands focus to the
  // feedback field once it has mounted. The planner re-plans the node from
  // that feedback; there is no local prompt editor (composer invariant 1).
  const revisionFeedbackRef = useRef<HTMLTextAreaElement | null>(null);
  const focusRevisionFeedbackOnMount = useRef(false);
  useEffect(() => {
    if (focusRevisionFeedbackOnMount.current && revisionFeedbackRef.current !== null) {
      focusRevisionFeedbackOnMount.current = false;
      revisionFeedbackRef.current.focus();
    }
  }, [revisionTarget]);
  const nodeReviseTarget = (stableId: string): GuidedEditTarget | null =>
    payload.edit_targets.find((target) => target.kind === "node" && target.stable_id === stableId) ?? null;
  const labelById = useMemo(() => {
    const labels = new Map<string, string>();
    for (const source of payload.graph.sources) labels.set(source.stable_id, source.label);
    for (const node of payload.nodes) labels.set(node.stable_id, node.label);
    for (const output of payload.outputs) labels.set(output.stable_id, output.label);
    return labels;
  }, [payload]);
  // Alias → author-visible route key (guidedGraphProjection owns the rule so
  // the route list here and the pane's edge labels cannot drift).
  const routeKeyByAlias = useMemo(() => routeKeysByAlias(payload.nodes), [payload.nodes]);
  const routeItems = useMemo<WireReviewItem[]>(() =>
    payload.graph.edges.map((edge) => ({
      id: edge.stable_id,
      from: labelById.get(edge.from_endpoint.stable_id) ?? edge.from_endpoint.stable_id,
      to: edge.to_endpoint.kind === "discard"
        ? "discard"
        : (labelById.get(edge.to_endpoint.stable_id) ?? edge.to_endpoint.stable_id),
      summary: flowLabel(edge.flow, routeKeyByAlias),
    })), [labelById, payload.graph.edges, routeKeyByAlias]);
  const currentBinding = proposalBindingMatches(payload, reviewState);
  const status = reviewStatusCopy(reviewState, currentBinding);
  const controlsLocked =
    disabled ||
    !currentBinding ||
    ["submitting", "reloading", "stale"].includes(reviewState.status) ||
    (reviewState.status === "error" && !reviewState.retryable);
  const actionEnabled = (candidate: GuidedProposalRetryAction): boolean => {
    if (controlsLocked) return false;
    if (reviewState.status !== "error") return true;
    if (!reviewState.retryable) return false;
    return sameRetryAction(reviewState.retry_action, candidate);
  };
  const revisionTargetEnabled = (target: GuidedEditTarget): boolean => {
    if (target.kind === "source" || target.kind === "output") {
      return actionEnabled({
        kind: "revise",
        edit_target: { kind: target.kind, stable_id: target.stable_id },
      });
    }
    return !controlsLocked && reviewState.status !== "error";
  };

  useEffect(() => {
    if (status !== null && reviewState.status !== "submitting") {
      statusRef.current?.focus({ preventScroll: true });
    }
  }, [reviewState.status, reviewState.proposal_id, reviewState.draft_hash, status]);

  const targetLabel = (target: GuidedEditTarget): string => {
    if (target.kind !== "edge") return labelById.get(target.stable_id) ?? `${target.kind} component`;
    const edge = payload.graph.edges.find((candidate) => candidate.stable_id === target.stable_id);
    if (edge === undefined) return "route";
    const from = labelById.get(edge.from_endpoint.stable_id) ?? "component";
    const to = edge.to_endpoint.kind === "discard"
      ? "discard"
      : (labelById.get(edge.to_endpoint.stable_id) ?? "component");
    return `route from ${from} to ${to}: ${flowLabel(edge.flow, routeKeyByAlias)}`;
  };

  return (
    <article className="guided-turn guided-proposal" aria-labelledby="guided-proposal-heading">
      <header className="guided-proposal__header">
        <h3 id="guided-proposal-heading">Review pipeline proposal</h3>
        <p>{proposalHeadline(payload.component_counts.nodes)}</p>
        <p>Review its structure, routes, and blockers before checking the detailed wiring.</p>
        <p className="guided-proposal__counts">
          {payload.component_counts.sources} sources · {payload.component_counts.nodes} nodes ·{" "}
          {payload.component_counts.edges} routes · {payload.component_counts.outputs} outputs
        </p>
      </header>

      {/* The Current Decision footer owns in-flight response activity for
          every guided turn. Proposal-specific recovery states stay here, but
          rendering "submitting" here as well would duplicate the same live
          composer progress above the graph and in the canonical footer. */}
      {status !== null && reviewState.status !== "submitting" ? (
        <p
          ref={statusRef}
          tabIndex={-1}
          role={status.role}
          className={`guided-proposal__status guided-proposal__status--${reviewState.status}`}
        >
          {status.message}
        </p>
      ) : null}

      {payload.blockers.length > 0 ? (
        <section className="guided-proposal__blockers" aria-labelledby="guided-proposal-blockers">
          <h4 id="guided-proposal-blockers">Before wiring review</h4>
          <ul>
            {payload.blockers.map((blocker, index) => (
              <li key={`${blocker.code}-${blocker.edit_target?.stable_id ?? index}`}>
                {BLOCKER_COPY[blocker.code]}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {/* The proposal's DAG is drawn in the Pipeline pane's Graph tab
          (GraphView → GuidedGraphPane, from the same payload via
          guidedGraphProjection) — not here. The 300px in-card mini graph was
          the review's IA-1/V-1 defect: a six-node DAG in the narrow column
          while the wide pane sat empty. This pointer is the card's only
          anchor to it; the button works on narrow viewports too, where the
          pane is a tab (ArtifactWorkspace's REQUEST_ARTIFACT_VIEW handler
          calls showPipeline before selecting the tab). */}
      <p className="guided-proposal__graph-pointer">
        The proposed structure is drawn in the Graph pane.{" "}
        <Button
          compact
          onClick={() =>
            dispatchArtifactViewIntent({
              tab: "graph",
              focusMode: false,
              sessionId: activeSessionId,
            })
          }
        >
          Show graph
        </Button>
      </p>

      <section className="guided-proposal__components" aria-labelledby="guided-proposal-components">
        <h4 id="guided-proposal-components">Components</h4>
        <ul>
          {payload.graph.sources.map((source) => (
            <li key={source.stable_id}>{source.label} · {stepLabelForPlugin(source.plugin.id)}</li>
          ))}
          {payload.nodes.map((node) => (
            <li key={node.stable_id}>
              {/* One humanised step label, exactly as the sibling wire-stage
                  row derives it (WireStageTurn: stepLabelForPlugin(plugin ??
                  node_type)) — the two surfaces named the same field two
                  different ways, and `row_union` is a member of the node_type
                  union, so the raw token reached default visible text. The
                  raw node_type and plugin id stay recoverable in `title`. */}
              <strong
                title={`${node.node_type}${node.plugin === null ? "" : ` · ${node.plugin.id}`}`}
              >
                {node.label} · {stepLabelForPlugin(node.plugin?.id ?? node.node_type)}
              </strong>
              <span>
                {" "}
                {node.behavior.kind === "gate"
                  ? gateSummary(node.behavior, node.stable_id, payload.graph.edges, labelById)
                  : behaviorSummary(node.behavior, (stableId) => labelById.get(stableId) ?? null)}
              </span>
              {/* R2-F3: the behavior discriminant alone made every transform
                  read as "transforms each incoming item"; the allowlisted key
                  options say what this one actually does. Advanced-tier pairs
                  are gated on show_advanced (elspeth-ca456d9d8d) — this list
                  has no per-row disclosure, so a plain gate, not a new
                  surface, is the honest form of "debug mode expands". An llm
                  node's model and prompts are common-tier (I-2): the decision
                  being approved, never gated. Its Edit opens the node revise
                  below; withheld with the rest of the revise flow in the
                  tutorial (step 3.2 un-hides them together). */}
              {(() => {
                const reviseTarget = isTutorial ? null : nodeReviseTarget(node.stable_id);
                return (
                  <NodeOptionsSummary
                    entries={node.node_options_summary.filter(
                      (entry) => showAdvanced || optionTier(entry) !== "advanced",
                    )}
                    nodeLabel={node.label}
                    onEdit={
                      reviseTarget === null
                        ? undefined
                        : () => {
                            setRevisionTarget(reviseTarget);
                            setRevisionFeedback("");
                            focusRevisionFeedbackOnMount.current = true;
                          }
                    }
                    editDisabled={reviseTarget !== null && !revisionTargetEnabled(reviseTarget)}
                  />
                );
              })()}
            </li>
          ))}
          {payload.outputs.map((output) => (
            <li key={output.stable_id}>{output.label} · {stepLabelForPlugin(output.plugin.id)}</li>
          ))}
        </ul>
      </section>

      <section className="guided-proposal__routes" aria-labelledby="guided-proposal-routes">
        <h4 id="guided-proposal-routes">Routes</h4>
        <WireReviewList items={routeItems} ariaLabel="Proposed pipeline routes" />
      </section>

      {/* Tutorial mode follows the same pattern as the other leaf widgets
          (InspectAndConfirmTurn keeps "Looks right" and hides "Edit columns…";
          SchemaFormTurn keeps Continue and hides Edit): keep the PRIMARY
          advance, hide the off-script affordances. Post-7.1 the tutorial's
          transforms phase produces a REAL planner proposal — there is no canned
          recipe exhibit any more (planning.py is the only propose_pipeline
          producer) — so "Review wiring" must dispatch for the learner to reach
          the wire stage at all. Reject (destructive, discards the planner
          build) and Revise (re-enters planner rounds off-script) stay hidden
          for the passive learner. Tutorial mode therefore PRESUPPOSES the
          frozen-prompt proposal arrives unblocked: revise — the only in-turn
          affordance that clears a blocker — is withheld, and the one blocker
          the tutorial realistically sees (interpretation_required) clears via
          the Accept cards outside this turn.

          That presupposition now holds on the FIRST proposal the learner
          sees. Goal-first (elspeth-378cfa0e18) makes the tutorial's frozen
          transforms prompt the session's ROOT INTENT, stated at
          /guided/start, so the step-2 finish plans ONCE from it and produces
          real processing steps. The withheld arm below is gone with the
          proposal it existed for: that transition used to auto-plan a
          source-to-sink pass-through before any transforms instruction
          existed (tutorial run 18: it committed and then failed the tutorial
          launch gate), which a later Send had to supersede — so "Review
          wiring" was withheld while supersedes_draft_hash was null. The one
          planned proposal the walk now produces carries a null
          supersedes_draft_hash and IS the thing to review, so gating on that
          field would strand the learner on a card with no forward
          affordance. */}
      {isTutorial ? (
        <p className="guided-proposal__tutorial-note">
          The assistant planned this pipeline from your prompt. Review how its sources, processing steps, routes, and outputs fit together, then press Review wiring to continue.
        </p>
      ) : null}
      <div className="guided-proposal__controls">
        <div className="guided-proposal__primary-actions">
          {retainedInstruction !== null ? (
            <Button
              variant="bare"
              className="guided-turn-primary"
              disabled={!actionEnabled(retainedInstruction)}
              onClick={() => onSubmit({
                chosen: null,
                edited_values: {
                  revision_instruction: retainedInstruction.revision_instruction,
                  revision_mode: retainedInstruction.revision_mode,
                },
                custom_inputs: null,
                edit_target: null,
                control_signal: null,
                proposal_id: payload.proposal_id,
                draft_hash: payload.draft_hash,
              } satisfies GuidedRespondAction)}
            >
              Retry {retainedInstruction.revision_mode} revision
            </Button>
          ) : null}
          <Button
            variant="bare"
            className="guided-turn-primary"
            disabled={!actionEnabled({ kind: "review_wiring" }) || payload.blockers.length > 0}
            onClick={() => onSubmit({
              chosen: ["review_wiring"],
              edited_values: null,
              custom_inputs: null,
              edit_target: null,
              control_signal: null,
              proposal_id: payload.proposal_id,
              draft_hash: payload.draft_hash,
            } satisfies GuidedRespondAction)}
          >
            Review wiring
          </Button>
          {/* The shortcut for an operator who has read the proposal above and
              does not need the wiring detail. Same gate as Review wiring —
              it dispatches review_wiring first — and withheld from the
              tutorial learner alongside Reject/Revise, since the script walks
              through the wire stage deliberately. */}
          {onApproveWiring !== undefined && !isTutorial && (
            <Button
              variant="bare"
              className="guided-turn-secondary"
              disabled={!actionEnabled({ kind: "review_wiring" }) || payload.blockers.length > 0}
              onClick={() => onApproveWiring({
                proposal_id: payload.proposal_id,
                draft_hash: payload.draft_hash,
              })}
            >
              Approve wiring
            </Button>
          )}
          {!isTutorial && (
            <Button
              variant="bare"
              className="guided-turn-secondary"
              disabled={!actionEnabled({ kind: "reject" })}
              onClick={() => onSubmit({
                chosen: null,
                edited_values: null,
                custom_inputs: null,
                edit_target: null,
                control_signal: "reject",
                proposal_id: payload.proposal_id,
                draft_hash: payload.draft_hash,
              } satisfies GuidedRespondAction)}
            >
              Reject proposal
            </Button>
          )}
        </div>
        {!isTutorial && (
          <fieldset className="guided-proposal__revise" disabled={controlsLocked}>
            <legend>Revise a component</legend>
            {payload.edit_targets.map((target) => (
              <Button
                variant="bare"
                className="guided-turn-secondary"
                key={`${target.kind}-${target.stable_id}`}
                disabled={!revisionTargetEnabled(target)}
                onClick={() => {
                  if (target.kind === "source" || target.kind === "output") {
                    const reviewedFormTarget = {
                      kind: target.kind,
                      stable_id: target.stable_id,
                    } as const;
                    onSubmit({
                      chosen: null,
                      edited_values: null,
                      custom_inputs: null,
                      edit_target: reviewedFormTarget,
                      control_signal: null,
                      proposal_id: payload.proposal_id,
                      draft_hash: payload.draft_hash,
                    } satisfies GuidedRespondAction);
                    return;
                  }
                  setRevisionTarget(target);
                  setRevisionFeedback("");
                }}
              >
                Revise {targetLabel(target)}
              </Button>
            ))}
            {revisionTarget !== null &&
            (revisionTarget.kind === "node" || revisionTarget.kind === "edge") ? (
              <div className="guided-proposal__correction">
                <p>
                  Revision for <strong>{targetLabel(revisionTarget)}</strong>
                </p>
                <label className="guided-schema-label" htmlFor={revisionFeedbackId}>
                  What should change?
                </label>
                <textarea
                  id={revisionFeedbackId}
                  ref={revisionFeedbackRef}
                  className="wire-stage__correction-input"
                  rows={2}
                  value={revisionFeedback}
                  maxLength={4096}
                  onChange={(event) => setRevisionFeedback(event.target.value)}
                />
                <Button
                  variant="bare"
                  className="guided-turn-secondary"
                  disabled={
                    revisionFeedback.trim().length === 0 ||
                    !actionEnabled({
                      kind: "revise",
                      edit_target: revisionTarget,
                      correction_feedback: revisionFeedback.trim(),
                    })
                  }
                  onClick={() => {
                    const correctionFeedback = revisionFeedback.trim();
                    if (correctionFeedback.length === 0) return;
                    onSubmit({
                      chosen: null,
                      edited_values: null,
                      custom_inputs: null,
                      edit_target: revisionTarget,
                      correction_feedback: correctionFeedback,
                      control_signal: null,
                      proposal_id: payload.proposal_id,
                      draft_hash: payload.draft_hash,
                    } satisfies GuidedRespondAction);
                  }}
                >
                  Send revision request
                </Button>
              </div>
            ) : null}
          </fieldset>
        )}
      </div>
    </article>
  );
}
