import { useId, useRef, useState } from "react";

import { Button } from "@/components/ui";
import type { GuidedEditTarget, ProposalFlow, ProposalNodeBehavior, WireRowCardinality, WireStageData } from "@/types/guided";
import { useShowAdvanced } from "@/stores/preferencesStore";
import { plural } from "@/utils/plural";
import { focusAcknowledgementCard } from "../AcknowledgementCard";
import { stepLabelForPlugin } from "../interpretationStepLabel";
import { behaviorSummary, humanToken } from "./behaviorSummary";
import { NodeOptionsSummary, nodeOptionText } from "./nodeOptionDisplay";
import { optionTier } from "./optionTiers";
import { WireReviewList } from "./WireReviewList";
import type { WiringApprovalClientBlockers, WiringApprovalSignals } from "./wiringApproval";

/**
 * One named blocker behind a disabled "Confirm wiring" — a pending
 * acknowledgement the user must resolve first. `id` is the interpretation
 * event id; clicking the entry scrolls to + focuses the blocking card
 * (`focusAcknowledgementCard`). Part of the shared dead-end fix: a disabled
 * primary action must name each pending item and give a direct path to it
 * (elspeth-3b35abf148 variant 1).
 */
export interface WireBlockerLink {
  id: string;
  label: string;
}

export interface WireEdge {
  stable_id: string;
  from: string;
  to: string;
  label: string;
  flow: ProposalFlow;
  satisfied: boolean | null;
  missing_fields: string[];
}

export function reconstructWireEdges(data: WireStageData): WireEdge[] {
  return data.connections.map((connection) => ({
    stable_id: connection.stable_id,
    from: connection.from_endpoint.stable_id,
    to: connection.to_endpoint.kind === "discard" ? "discard" : connection.to_endpoint.stable_id,
    label: connection.flow.kind,
    flow: connection.flow,
    satisfied: connection.schema_contract?.satisfied ?? null,
    missing_fields: connection.schema_contract?.missing_fields ?? [],
  }));
}

export interface WireStageTurnProps {
  data: WireStageData;
  onConfirm: () => void;
  confirmDisabled: boolean;
  /** Exit to freeform remains available without changing proposal authority. */
  onExitToFreeform?: () => void;
  /** Pending acknowledgements blocking this confirm. Each renders as a named
   *  jump link under the disabled button (scrolls to + focuses the card). */
  pendingAcknowledgements?: WireBlockerLink[];
  /** Client-known validation blockers (the persisted composition is invalid).
   *  Non-empty DISABLES confirm — a confirm the server must reject is never
   *  offered as a live button (elspeth-3b35abf148 variant 3, client side). */
  validationIssues?: string[];
  onCorrect?: (target: GuidedEditTarget, feedback: string) => void;
}

/**
 * Human names for every topology entity, keyed by its internal id
 * (elspeth-016f463ff0: internal node and output identifiers must not reach
 * first-run copy). Transforms reuse the acknowledgement cards' step-label
 * mapping (stepLabelForPlugin, e.g. llm → "Summarise") so the wiring list and
 * the cards name a step identically; a plugin-less structural node falls back
 * to its node_type through the same humaniser. Sources read as "Source" (or
 * "<name> source" for a named source); outputs read as "<sink_name> output" —
 * both names are user-meaningful, not internal ids.
 */
export function buildEntityNames(data: WireStageData): Map<string, string> {
  const names = new Map<string, string>();
  for (const source of data.sources) {
    names.set(source.stable_id, source.label);
  }
  for (const node of data.nodes) {
    names.set(node.stable_id, `${node.label} (${stepLabelForPlugin(node.plugin ?? node.node_type)})`);
  }
  for (const output of data.outputs) {
    names.set(output.stable_id, output.label);
  }
  names.set("discard", "Discard");
  return names;
}

/** Plain-language connection state, the register-shifted sibling of
 *  ``rawEdgeRow``'s parenthesised technical status: a first-run user reads
 *  "no static check" where the raw row says "contract not statically
 *  checked".
 *
 *  Both stay cause-free about WHY the validator omitted the contract — with
 *  the one exception the payload itself carries: a route whose destination is
 *  `discard` has no consumer to check a contract against, so it reads as the
 *  discard route it is instead of borrowing the absent-check wording.
 *
 *  Every other absent contract reads "no static check": a statement about
 *  this card, not a promise. The previous "not yet checked" implied a check
 *  still coming, and none is — those routes are enforced row by row at run
 *  time (elspeth-e4c2ebb697: the card must not read greener, or more
 *  provisional, than the validation it reflects). */
function edgeStatus(edge: WireEdge): string {
  if (edge.satisfied === true) return "connected";
  if (edge.satisfied === false) return "not connected correctly";
  return edge.to === "discard" ? "discard route" : "no static check";
}

/** Chip variant for a route's contract state (WireReviewList status). */
function edgeStatusKind(edge: WireEdge): "connected" | "warning" | "unchecked" | "discard" {
  if (edge.satisfied === true) return "connected";
  if (edge.satisfied === false) return "warning";
  return edge.to === "discard" ? "discard" : "unchecked";
}

/**
 * Routes carrying no static verdict for a reason the payload does NOT carry.
 *
 * Discard destinations are deliberately excluded: for those the destination
 * IS the reason, they are named as discard routes in their own right, and
 * counting them here would make the Confirm caption accept routes nobody
 * needs to accept — the button would read LESS green than the validation
 * warrants, the mirror image of the defect this calibration fixes.
 */
export function routesWithoutStaticCheck(edges: WireEdge[]): number {
  return edges.filter((edge) => edge.satisfied === null && edge.to !== "discard").length;
}

/**
 * One-line route roll-up ("9 routes — 1 connected, 3 with no static check,
 * 5 discard routes") so the list's overall state reads once, instead of every
 * row trailing the same clause (the operator-reported debug-dump read).
 * Zero-count categories are elided, so the line never names a category the
 * pipeline does not have.
 */
export function routesSummaryText(edges: WireEdge[]): string {
  const connected = edges.filter((edge) => edge.satisfied === true).length;
  const broken = edges.filter((edge) => edge.satisfied === false).length;
  const unchecked = routesWithoutStaticCheck(edges);
  const discarded = edges.length - connected - broken - unchecked;
  const parts = [
    ...(connected > 0 ? [`${connected} connected`] : []),
    ...(broken > 0 ? [`${broken} not connected correctly`] : []),
    ...(unchecked > 0 ? [`${unchecked} with no static check`] : []),
    ...(discarded > 0 ? [plural(discarded, "discard route")] : []),
  ];
  const heading = edges.length === 1 ? "1 route" : `${edges.length} routes`;
  return parts.length > 0 ? `${heading} — ${parts.join(", ")}` : heading;
}

/**
 * The one sentence that is true in EVERY absent-contract case.
 *
 * A statement about what THIS CARD knows, deliberately not about what the
 * validator did. The validator omits an EdgeContract for at least four
 * different reasons (see ``rawEdgeRow``) and the payload cannot tell them
 * apart — and there is a fifth case where it did check the route and the
 * verdict simply never reached the card: the wire payload looks contracts up
 * by the graph edge's own endpoints while `EdgeContract.from_id` is the
 * WALKED-BACK producer, and a `gate` is the one node kind that shifts that id
 * (emitters.py `_project_wire`, state.py's producer walk-back;
 * `test_state.py::test_same_gate_multiple_routes_to_same_sink_emit_one_contract`
 * is the live reproduction, and elspeth-0edd5b73ec is the defect). A
 * gate-routed edge therefore arrives here with a null contract even though the
 * validator checked it and found it satisfied. The terse labels — the chip,
 * the roll-up, this helper's own name — are deliberately NOT reworded around
 * that: they come true the moment the verdict reaches the card, and rewording
 * them would paper over the defect instead of fixing it.
 *
 * So this claims only the absence itself, which holds in all five cases: this
 * card has no verdict, and the destination's own requirements are enforced
 * per row at run time. It names no cause, asserts nothing about the
 * validator, and promises no later static check.
 */
const ROUTES_WITHOUT_STATIC_CHECK_NOTE =
  "This card carries no static verdict for these routes; whatever their " +
  "destination requires is enforced row by row when the pipeline runs.";

/** The arm both blocker sources share: the card's panel names every one of
 *  them — server blockers, client validation issues and acknowledgement jump
 *  links alike — so this sentence stays true whichever combination holds. */
const CARD_NAMES_THE_ISSUES = "Fix the issues named on the card, then press Confirm wiring.";

/**
 * Composer placeholder for the wire stage (step 4) as a function of live
 * state, not one frozen sentence.
 *
 * The frozen wording ("Clear pending acknowledgements, then press Confirm
 * wiring…") named a blocker most sessions do not have, and read as an
 * instruction the user could not follow. Each arm here names the action that
 * is actually available: the card's own named issues while anything stops the
 * confirm, the pending cards while they are the only thing outstanding,
 * otherwise the two real controls on the card.
 *
 * WHY THE WIRE TURN'S OWN VERDICT LEADS. `can_confirm` and `blockers` are the
 * server's judgment and the USUAL reason Confirm is off at step 4 — the
 * pre-commit guided composition is empty-by-design, so `validationIssues` is
 * normally 0 there (ChatPanel's own note on `wireValidationIssues`). A
 * placeholder blind to them fell through to the last arm and told the learner
 * to press a disabled button; in the tutorial, whose read-only box is empty,
 * that was the only instruction on screen. Leading with it also keeps the
 * message TRUE when several things hold at once: acknowledgement cards are
 * named on the card too, so "the issues named on the card" covers them, while
 * an acknowledgement-first order would promise that clearing the cards makes
 * Confirm live when server blockers remain. That is why the order here is not
 * the blockers panel's; it is "the sentence that survives every combination".
 *
 * The tutorial needs no branch: its wire phase has no locked prompt, so the
 * read-only box is empty and this line is the only thing in it — which is
 * exactly the explanation of why Send does nothing there.
 *
 * `wiring` is null when the card is not on screen (a step-4 session whose next
 * turn is not `confirm_wiring`); there is no verdict to lead with then, and
 * the client-known arms carry the caption on their own.
 *
 * Length budget (the measured one `GUIDED_CHAT_PLACEHOLDERS` records): every
 * arm stays ≤80 chars, or the 360px pane clips the last line silently.
 */
export function wireStagePlaceholder(
  blockers: WiringApprovalClientBlockers,
  wiring: WiringApprovalSignals | null,
): string {
  if (wiring !== null && (!wiring.can_confirm || wiring.blockers.length > 0)) {
    return CARD_NAMES_THE_ISSUES;
  }
  if (blockers.pendingAcknowledgements > 0) {
    return `Resolve the ${plural(
      blockers.pendingAcknowledgements,
      "pending acknowledgement card",
    )}, then press Confirm wiring.`;
  }
  if (blockers.validationIssues > 0) {
    return CARD_NAMES_THE_ISSUES;
  }
  // NOT "Re-plan wiring": that is the correction form's submit label only
  // while an edge or a node is selected, and the form opens on the FIRST
  // correction target, which is a source — so the default render shows "Edit
  // component settings" under the heading "Edit reviewed component". The
  // design specified the button name verbatim and was wrong about the code;
  // naming the form instead is true in both of its states, and reordering
  // `correctionTargets` to make the old copy true would silently change which
  // component a blind Enter corrects.
  return "Press Confirm wiring on the card, or use its form to change a component.";
}

function cardinalityText(cardinality: WireRowCardinality): string {
  const expected = cardinality.expected_output_count === null
    ? ""
    : ` (expected ${cardinality.expected_output_count})`;
  return `Cardinality: ${humanToken(cardinality.input)} → ${humanToken(cardinality.output)}${expected}`;
}

function fieldsText(label: "Required" | "Guaranteed", fields: string[]): string {
  return `${label} fields: ${fields.length > 0 ? fields.join(", ") : "none"}`;
}

/** Alias → author-visible route key, from each gate's behavior bindings
 *  (aliases are globally unique ordinals, so one flat map covers all gates). */
export function buildRouteKeys(data: WireStageData): Map<string, string> {
  const keys = new Map<string, string>();
  for (const node of data.nodes) {
    if (node.behavior.kind !== "gate") continue;
    for (const { alias, key } of node.behavior.routes) keys.set(alias, key);
  }
  return keys;
}

function flowText(flow: ProposalFlow, routeKeys: ReadonlyMap<string, string>): string {
  // "(when <key>)" resolves the ordinal to the author-visible route key so a
  // route row reads as the branch it actually is (F11); the ordinal alias
  // stays visible — it is the correction-target / integrity token.
  const keyed = (alias: string): string => {
    const key = routeKeys.get(alias);
    return key === undefined ? alias : `${alias} (when ${key})`;
  };
  switch (flow.kind) {
    case "source_success":
      return flow.branch === null ? "Source success" : `Source success on ${flow.branch}`;
    case "source_validation_failure":
      return "Source validation failure";
    case "node_success":
      return flow.branch === null ? "Node success" : `Node success on ${flow.branch}`;
    case "node_error":
      return "Node failure";
    case "gate_route":
      return flow.branch === null
        ? `Gate route ${keyed(flow.route)}`
        : `Gate route ${keyed(flow.route)} on ${flow.branch}`;
    case "gate_fork":
      return `Gate fork ${flow.routes.map(keyed).join(", ")} as ${flow.branch}`;
    case "queue_continue":
      return flow.branch === null ? "Queue continuation" : `Queue continuation on ${flow.branch}`;
    case "coalesce_success":
      return flow.branch === null ? "Coalesce success" : `Coalesce success on ${flow.branch}`;
    case "row_union_success":
      return flow.branch === null
        ? "Row union success"
        : `Row union success on ${flow.branch}`;
    case "output_write_failure":
      return "Output write failure";
  }
}

function behaviorDetails(
  behavior: ProposalNodeBehavior,
  routeDestination: (alias: string) => string | null = () => null,
  nodeLabel: (stableId: string) => string | null = () => null,
): string[] {
  switch (behavior.kind) {
    case "transform":
      return ["Policy: transform each input row"];
    case "queue":
      return ["Policy: continue queued items individually"];
    case "collector": {
      const opener = nodeLabel(behavior.opener_stable_id);
      return [
        opener === null
          ? "Closes the row group opened by its scope opener"
          : `Closes the row group opened by ${opener}`,
        `Policy: ${humanToken(behavior.policy)}`,
      ];
    }
    case "gate":
      return [
        // The authored predicate itself (F11, verbatim) is the row's
        // always-visible summary line now (elspeth-ca456d9d8d), so the
        // disclosure starts at each author-visible route key resolved to the
        // entity it feeds — repeating the predicate here would print the same
        // sentence twice in one row.
        ...behavior.routes.map(({ alias, key }) => {
          const destination = routeDestination(alias);
          return destination === null
            ? `When ${key} (${alias})`
            : `When ${key} → ${destination} (${alias})`;
        }),
        `Routes: ${behavior.route_aliases.join(", ")}`,
        ...behavior.fork_branches.map((fork) => `Fork branch ${fork.branch}: ${fork.routes.join(", ")}`),
      ];
    case "aggregation":
      return [
        `Triggers: ${behavior.trigger_kinds.join(", ")}`,
        ...(behavior.count === null ? [] : [`Count: ${behavior.count}`]),
        ...(behavior.timeout_seconds === null ? [] : [`Timeout: ${behavior.timeout_seconds} seconds`]),
        `Output mode: ${humanToken(behavior.output_mode)}`,
        ...(behavior.expected_output_count === null ? [] : [`Expected output count: ${behavior.expected_output_count}`]),
      ];
    case "coalesce":
      return [
        `Branches: ${behavior.branch_aliases.join(", ")}`,
        `Policy: ${humanToken(behavior.policy)}`,
        `Merge: ${humanToken(behavior.merge)}`,
        ...(behavior.timeout_seconds === null ? [] : [`Timeout: ${behavior.timeout_seconds} seconds`]),
      ];
    case "row_union":
      return [
        `Branches preserved: ${behavior.branch_aliases.join(", ")}`,
        "Policy: wait for every branch, then forward each row",
        ...(behavior.timeout_seconds === null
          ? []
          : [`Timeout: ${behavior.timeout_seconds} seconds`]),
      ];
  }
}

/** The verbatim engineer-grade row, preserved behind the Technical details
 *  expander for operators (same idiom as the validation summary's raw dump). */
function rawEdgeRow(edge: WireEdge, routeKeys: ReadonlyMap<string, string>): string {
  // A null contract is CAUSE-FREE on the wire and must render that way. The
  // validator omits an EdgeContract for at least four different reasons —
  // ADR-007 producer abstention with a NON-empty sink_required
  // (state.py:2846-2874), the error-continue paths that skip a node outright
  // (state.py:2637-2673), discard edges (emitters.py), and the genuinely
  // nothing-required case. The payload cannot tell them apart, so the row
  // reports only that no STATIC verdict exists; naming any one cause would
  // assert three falsehoods. (The prior "(contract unchecked)" was rejected for
  // implying a check still pending.)
  const status =
    edge.satisfied === true
      ? "(connected)"
      : edge.satisfied === false
        ? "(not satisfied)"
        : "(contract not statically checked)";
  const missing =
    edge.missing_fields.length > 0
      ? ` Missing fields: ${edge.missing_fields.join(", ")}`
      : "";
  return `[${edge.stable_id}] ${edge.from} -> ${edge.to} via ${flowText(edge.flow, routeKeys)} ${status}${missing}`;
}

function warningText(warning: Record<string, unknown>): string {
  const message = warning.message;
  if (typeof message === "string") return message;
  return JSON.stringify(warning);
}

export function WireStageTurn({
  data,
  onConfirm,
  confirmDisabled,
  onExitToFreeform,
  pendingAcknowledgements,
  validationIssues,
  onCorrect,
}: WireStageTurnProps) {
  const showAdvanced = useShowAdvanced();
  const edges = reconstructWireEdges(data);
  const entityNames = buildEntityNames(data);
  const nameFor = (id: string): string => entityNames.get(id) ?? id;
  const routeKeys = buildRouteKeys(data);
  // Resolve one gate route alias to the human name of the entity it feeds
  // ("When high → review output (route-1)"): a direct gate_route edge names
  // its target; a fork route names every branch target it fans out to.
  const routeDestinationFor = (gateId: string) => (alias: string): string | null => {
    const targets = data.connections
      .filter(
        (connection) =>
          connection.from_endpoint.stable_id === gateId &&
          ((connection.flow.kind === "gate_route" && connection.flow.route === alias) ||
            (connection.flow.kind === "gate_fork" && connection.flow.routes.includes(alias))),
      )
      .map((connection) =>
        connection.to_endpoint.kind === "discard" ? nameFor("discard") : nameFor(connection.to_endpoint.stable_id),
      );
    return targets.length === 0 ? null : targets.join(" + ");
  };
  const blockersId = useId();
  const routesHeadingId = useId();
  const routesNoteId = useId();
  const confirmNoteId = useId();
  const correctionSelectId = useId();
  const correctionFeedbackId = useId();
  const correctionTargets: Array<{ target: GuidedEditTarget; label: string }> = [
    ...data.sources.map((source) => ({ target: { kind: "source" as const, stable_id: source.stable_id }, label: source.label })),
    ...data.nodes.map((node) => ({ target: { kind: "node" as const, stable_id: node.stable_id }, label: node.label })),
    ...data.connections.map((connection) => ({
      target: { kind: "edge" as const, stable_id: connection.stable_id },
      label: `${nameFor(connection.from_endpoint.stable_id)} → ${
        nameFor(
          connection.to_endpoint.kind === "discard"
            ? "discard"
            : connection.to_endpoint.stable_id,
        )
      } — ${flowText(connection.flow, routeKeys)}`,
    })),
    ...data.outputs.map((output) => ({ target: { kind: "output" as const, stable_id: output.stable_id }, label: output.label })),
  ];
  const [correctionTarget, setCorrectionTarget] = useState(correctionTargets[0]?.target.stable_id ?? "");
  const [correctionFeedback, setCorrectionFeedback] = useState("");
  const correctionFeedbackRef = useRef<HTMLTextAreaElement | null>(null);
  // "Edit prompt" on an llm row (I-2): the change goes back through the
  // planner as a node-scoped correction — pre-select that node in the
  // existing form and hand focus to the feedback field. No local editor.
  const beginNodeCorrection = (stableId: string): void => {
    setCorrectionTarget(stableId);
    correctionFeedbackRef.current?.focus();
  };
  const selectedCorrectionKind = correctionTargets.find(
    (item) => item.target.stable_id === correctionTarget,
  )?.target.kind;
  const isFormDirectedCorrection =
    selectedCorrectionKind === "source" || selectedCorrectionKind === "output";

  const acknowledgements = pendingAcknowledgements ?? [];
  const blockingValidationIssues = validationIssues ?? [];
  const hasBlockers =
    acknowledgements.length > 0 || blockingValidationIssues.length > 0 || data.blockers.length > 0;
  const uncheckedRoutes = routesWithoutStaticCheck(edges);
  // What THIS CARD knows against confirming: the server's verdict, its
  // blockers, and the client-known validation issues. Drives the button's own
  // `disabled` term.
  const confirmBlocked =
    !data.can_confirm || data.blockers.length > 0 || blockingValidationIssues.length > 0;
  // The caption claims what pressing Confirm accepts, so it renders only when
  // a press is possible. There are two DURABLE reasons it is not: this card's
  // own verdict/blockers (`confirmBlocked`) and a pending acknowledgement on
  // another card, which the user must resolve before Confirm re-enables.
  // `confirmDisabled` is NOT the gate even though it carries both — it also
  // carries the in-flight dispatch, and a caption that blinked out mid-submit
  // would read as the caveat having been resolved. The durable half arrives
  // here directly, as `acknowledgements`.
  const confirmUnavailable = confirmBlocked || acknowledgements.length > 0;
  // The routes NOTE is deliberately NOT gated with this: that one is a fact
  // about the routes, true whether or not anything can be confirmed today.
  const showConfirmNote = uncheckedRoutes > 0 && !confirmUnavailable;

  // Every id here must be an element that actually renders — an
  // aria-describedby pointing at a missing id is an axe violation
  // (aria-valid-attr-value) and this component is in AUDITED_COMPONENTS. So
  // the list is filtered, never a fixed triple: the blockers panel, the
  // routes note and the confirm caption each render under their own
  // condition. Together they are what the button is accepting.
  const confirmDescribedBy = [
    ...(hasBlockers ? [blockersId] : []),
    ...(uncheckedRoutes > 0 ? [routesNoteId] : []),
    ...(showConfirmNote ? [confirmNoteId] : []),
  ];

  const confirmButton = (
    <Button
      variant="bare"
      className="guided-turn-primary"
      onClick={onConfirm}
      // UNCHANGED by the route calibration: ADR-007 abstention is
      // admissible, so routes with no static check must not disable a confirm
      // the server would accept (`can_confirm` is `validation.is_valid`).
      // What changes is that the button now SAYS what it accepts — the
      // caption below it and the note in the routes section.
      // `confirmBlocked`, not `confirmUnavailable`: a pending acknowledgement
      // already disables through `confirmDisabled` (ChatPanel gates that prop
      // on the same predicate), so folding it in again would be redundant.
      disabled={confirmDisabled || confirmBlocked}
      aria-describedby={confirmDescribedBy.length > 0 ? confirmDescribedBy.join(" ") : undefined}
    >
      Confirm wiring
    </Button>
  );

  // Named-blocker panel: renders directly under the (possibly disabled)
  // confirm button so the unblock path is never buried in another column
  // (elspeth-3b35abf148 variant 1). Acknowledgement entries are jump links to
  // the blocking card; validation issues are the concrete "what's invalid".
  const blockersPanel = hasBlockers ? (
    <div id={blockersId} className="wire-stage__blockers">
      {acknowledgements.length > 0 && (
        <>
          <p className="wire-stage__blockers-heading">
            {acknowledgements.length === 1
              ? "1 acknowledgement pending — resolve it to enable Confirm wiring:"
              : `${acknowledgements.length} acknowledgements pending — resolve each to enable Confirm wiring:`}
          </p>
          <ul className="wire-stage__blockers-list">
            {acknowledgements.map((blocker) => (
              <li key={blocker.id}>
                <Button
                  variant="bare"
                  className="wire-stage__blocker-link"
                  onClick={() => focusAcknowledgementCard(blocker.id)}
                >
                  {blocker.label}
                </Button>
              </li>
            ))}
          </ul>
        </>
      )}
      {blockingValidationIssues.length > 0 && (
        <>
          <p className="wire-stage__blockers-heading">
            The pipeline isn't ready to confirm:
          </p>
          <ul className="wire-stage__blockers-list wire-stage__blockers-list--issues">
            {blockingValidationIssues.map((issue, index) => (
              <li key={index}>{issue}</li>
            ))}
          </ul>
        </>
      )}
      {data.blockers.length > 0 ? (
        <ul className="wire-stage__blockers-list wire-stage__blockers-list--issues">
          {data.blockers.map((blocker, index) => <li key={index}>{warningText(blocker)}</li>)}
        </ul>
      ) : null}
    </div>
  ) : null;

  const exitButton = (
    <Button
      variant="bare"
      className="guided-turn-secondary"
      onClick={() => onExitToFreeform?.()}
    >
      Exit to freeform
    </Button>
  );

  return (
    <div className="guided-turn wire-stage">
      <h3>Review wiring</h3>

      {data.warnings.length > 0 ? (
        <ul className="wire-stage__warnings">
          {data.warnings.map((warning, index) => (
            <li key={index}>{warningText(warning)}</li>
          ))}
        </ul>
      ) : null}

      <section className="wire-stage__components" aria-label="Reviewed components">
        {data.sources.length > 0 ? (
          <div>
            <h4>Sources</h4>
            <ul>
              {data.sources.map((source) => (
                <li key={source.stable_id}>
                  <strong>{source.label}</strong>{" "}
                  <span>({stepLabelForPlugin(source.plugin)})</span>
                  <p>Validation failure: {humanToken(source.on_validation_failure)}</p>
                  <details className="wire-stage__row-technical" open={showAdvanced}>
                    <summary>Technical details</summary>
                    <p>{cardinalityText(source.row_cardinality)}</p>
                    <p>{fieldsText("Guaranteed", source.guaranteed_fields)}</p>
                    <p>Stable ID: <code>{source.stable_id}</code></p>
                  </details>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {data.nodes.length > 0 ? (
          <div>
            <h4>Processing nodes</h4>
            <ul>
              {data.nodes.map((node) => (
                <li key={node.stable_id}>
                  <strong>{node.label}</strong>{" "}
                  <span>({stepLabelForPlugin(node.plugin ?? node.node_type)})</span>
                  <p>
                    {node.behavior.kind === "gate"
                      ? `When ${node.behavior.condition}`
                      : behaviorSummary(node.behavior, (stableId) =>
                          data.nodes.find((candidate) => candidate.stable_id === stableId)?.label ?? null)}
                  </p>
                  {/* Common-tier pairs inline, including an llm node's
                      model and prompts (I-2) — a decision input, never
                      behind the Technical details disclosure. Edit is
                      offered only when a correction path exists. */}
                  <NodeOptionsSummary
                    entries={node.node_options_summary.filter((entry) => optionTier(entry) !== "advanced")}
                    nodeLabel={node.label}
                    onEdit={onCorrect === undefined ? undefined : () => beginNodeCorrection(node.stable_id)}
                  />
                  <details className="wire-stage__row-technical" open={showAdvanced}>
                    <summary>Technical details</summary>
                    <p>{cardinalityText(node.row_cardinality)}</p>
                    <p>{fieldsText("Required", node.required_fields)}</p>
                    <p>{fieldsText("Guaranteed", node.guaranteed_fields)}</p>
                    {behaviorDetails(
                      node.behavior,
                      routeDestinationFor(node.stable_id),
                      (stableId) => data.nodes.find((candidate) => candidate.stable_id === stableId)?.label ?? null,
                    ).map((detail) => <p key={detail}>{detail}</p>)}
                    {node.node_options_summary
                      .filter((entry) => optionTier(entry) === "advanced")
                      .map((entry) => (
                        <p key={entry.key}>{nodeOptionText(entry)}</p>
                      ))}
                    {node.structured_output_fields.length > 0 ? (
                      <ul aria-label={`${node.label} structured output fields`}>
                        {node.structured_output_fields.map((field) => (
                          <li key={`${field.query}:${field.field}`}>
                            {`${field.field} (${field.type}) from ${field.query}${
                              field.enum_values.length > 0 ? `; values: ${field.enum_values.join(", ")}` : ""
                            }`}
                          </li>
                        ))}
                      </ul>
                    ) : null}
                    <p>Stable ID: <code>{node.stable_id}</code></p>
                  </details>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {data.outputs.length > 0 ? (
          <div>
            <h4>Outputs</h4>
            <ul>
              {data.outputs.map((output) => (
                <li key={output.stable_id}>
                  <strong>{output.label}</strong>{" "}
                  <span>({stepLabelForPlugin(output.plugin)})</span>
                  <p>Write failure: {humanToken(output.on_write_failure)}</p>
                  <details className="wire-stage__row-technical" open={showAdvanced}>
                    <summary>Technical details</summary>
                    <p>{fieldsText("Required", output.required_fields)}</p>
                    <p>Schema mode: {humanToken(output.business_schema.mode)}</p>
                    {output.business_schema.fields.length > 0 ? (
                      <ul aria-label={`${output.label} business schema fields`}>
                        {output.business_schema.fields.map((field) => (
                          <li key={field.name}>
                            {`${field.name}: ${field.type} — ${field.required ? "required" : "optional"}, ${
                              field.nullable ? "nullable" : "non-null"
                            }`}
                          </li>
                        ))}
                      </ul>
                    ) : null}
                    <p>{fieldsText("Guaranteed", output.business_schema.guaranteed_fields)}</p>
                    <p>{fieldsText("Required", output.business_schema.required_fields)}</p>
                    <p>Stable ID: <code>{output.stable_id}</code></p>
                  </details>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </section>

      {/* Human step names + plain-language connection state
          (elspeth-016f463ff0). Status renders as a per-row chip plus one
          count roll-up (never per-row trailing "— …" prose) and is folded
          into each row's accessible name — the li aria-label overrides its
          text content, so a chip outside it is invisible to screen readers.
          The raw ids / connection labels stay available verbatim behind the
          Technical details expander. */}
      {edges.length > 0 ? (
        <section className="wire-stage__routes" aria-labelledby={routesHeadingId}>
          <h4 id={routesHeadingId}>Routes</h4>
          <p className="wire-stage__routes-summary">{routesSummaryText(edges)}</p>
          {uncheckedRoutes > 0 ? (
            <p id={routesNoteId} className="wire-stage__routes-note">
              {ROUTES_WITHOUT_STATIC_CHECK_NOTE}
            </p>
          ) : null}
          <WireReviewList
            className="wire-stage__edges"
            ariaLabel="Wiring routes"
            items={edges.map((edge) => ({
              id: edge.stable_id,
              from: nameFor(edge.from),
              to: nameFor(edge.to),
              summary: flowText(edge.flow, routeKeys),
              status: edgeStatusKind(edge),
              detail:
                edge.missing_fields.length > 0
                  ? `Missing fields: ${edge.missing_fields.join(", ")}`
                  : null,
              ariaLabel: `${nameFor(edge.from)} to ${nameFor(edge.to)} — ${flowText(edge.flow, routeKeys)} — ${edgeStatus(edge)}`,
            }))}
          />
          <details className="wire-stage__raw">
            <summary>Technical details</summary>
            <pre className="wire-stage__raw-text">
              {edges.map((edge) => rawEdgeRow(edge, routeKeys)).join("\n")}
            </pre>
          </details>
        </section>
      ) : null}

      {onCorrect !== undefined && correctionTargets.length > 0 ? (
        <form
          className="wire-stage__correction"
          onSubmit={(event) => {
            event.preventDefault();
            const selected = correctionTargets.find((item) => item.target.stable_id === correctionTarget);
            if (selected !== undefined && correctionFeedback.trim().length > 0) {
              onCorrect(selected.target, correctionFeedback.trim());
            }
          }}
        >
          {/* Proper form-group idiom (operator-reported soup: wrapping labels
              overlapped the bare native select and the tiny off-baseline
              textarea). Explicit for/id association + the schema form's field
              classes; keep the accessible names verbatim ("Component" /
              "What should change?"). */}
          <h4>{isFormDirectedCorrection ? "Edit reviewed component" : "Request a wiring correction"}</h4>
          <div className="guided-schema-field-row">
            <label className="guided-schema-label" htmlFor={correctionSelectId}>
              Component
            </label>
            <select
              id={correctionSelectId}
              className="guided-schema-select"
              value={correctionTarget}
              onChange={(event) => setCorrectionTarget(event.target.value)}
            >
              {correctionTargets.map((item) => (
                <option key={`${item.target.kind}:${item.target.stable_id}`} value={item.target.stable_id}>
                  {item.label}
                </option>
              ))}
            </select>
          </div>
          <div className="guided-schema-field-row">
            <label className="guided-schema-label" htmlFor={correctionFeedbackId}>
              What should change?
            </label>
            <textarea
              id={correctionFeedbackId}
              ref={correctionFeedbackRef}
              className="wire-stage__correction-input"
              rows={2}
              value={correctionFeedback}
              maxLength={4096}
              onChange={(event) => setCorrectionFeedback(event.target.value)}
            />
          </div>
          <div className="wire-stage__correction-actions">
            {/* Inside the correction <form>: Button defaults to type="button",
                so the submit role MUST stay explicit — this is the control the
                form's onSubmit (and any Enter-key submit) fires through. */}
            <Button variant="bare" type="submit" className="guided-turn-secondary" disabled={correctionFeedback.trim().length === 0}>
              {isFormDirectedCorrection ? "Edit component settings" : "Re-plan wiring"}
            </Button>
          </div>
        </form>
      ) : null}

      <div className="wire-stage__actions">
        {confirmButton}
        {onExitToFreeform !== undefined ? exitButton : null}
      </div>
      {/* What an ENABLED Confirm is accepting, said at the button rather than
          left to be inferred from the roll-up above it. Absent when every
          route carries a verdict (or is a discard route), so it never
          manufactures a caveat the validation does not have — and absent
          (`confirmUnavailable`) whenever a DURABLE state stops the press: this
          card's own blockers, or a pending acknowledgement whose panel below
          says to resolve it first. It would otherwise sit directly above
          "1 acknowledgement pending — resolve it to enable Confirm wiring:",
          describing what a dead button accepts. Phrased as what this CARD
          knows, for the same reason as the routes note above: a gate-routed
          edge the validator checked and passed can still arrive with a null
          contract. */}
      {showConfirmNote ? (
        <p id={confirmNoteId} className="wire-stage__confirm-note">
          {`Confirming accepts ${plural(uncheckedRoutes, "route")} with no verdict on this card.`}
        </p>
      ) : null}
      {blockersPanel}
    </div>
  );
}
