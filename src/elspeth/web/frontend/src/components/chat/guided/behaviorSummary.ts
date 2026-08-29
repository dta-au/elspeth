// ============================================================================
// behaviorSummary.ts — one-sentence plain-language summaries of a proposal
// node's behavior, shared by the proposal turn and the wire-stage review.
//
// These lived in ProposePipelineTurn.tsx until the wire-stage rows needed the
// same summary line above their Technical details disclosure
// (elspeth-ca456d9d8d). Both surfaces must read identically — the proposal
// card and the wiring review describe the SAME node — so the summaries have
// exactly one implementation. The engineer-grade breakdown stays in
// WireStageTurn's `behaviorDetails`; this module is the register-shifted
// summary that replaces it in the default view.
// ============================================================================

import type { ProposalNodeBehavior, ProposePipelinePayload } from "@/types/guided";

/** Wire enum → prose token ("require_all" → "require all"). Copy register:
 *  no internal identifiers in visible text, so every enum the guided review
 *  surfaces print goes through here. */
export function humanToken(value: string): string {
  return value.replace(/_/g, " ");
}

/** "When <condition> — true → <dest>, false → <dest>" (F11): the authored
 *  predicate verbatim, with each author-visible route key resolved to the
 *  destination the proposal actually wires. Ordinal aliases stay visible in
 *  parentheses (they are the revise-target labels). */
export function gateSummary(
  behavior: Extract<ProposalNodeBehavior, { kind: "gate" }>,
  gateId: string,
  edges: ProposePipelinePayload["graph"]["edges"],
  labelById: ReadonlyMap<string, string>,
): string {
  const targetLabel = (edge: ProposePipelinePayload["graph"]["edges"][number]): string =>
    edge.to_endpoint.kind === "discard"
      ? "discard"
      : (labelById.get(edge.to_endpoint.stable_id) ?? edge.to_endpoint.stable_id);
  const destination = (alias: string): string | null => {
    const direct = edges.find(
      (edge) =>
        edge.from_endpoint.stable_id === gateId &&
        edge.flow.kind === "gate_route" &&
        edge.flow.route === alias,
    );
    if (direct !== undefined) return targetLabel(direct);
    const forks = edges.filter(
      (edge) =>
        edge.from_endpoint.stable_id === gateId &&
        edge.flow.kind === "gate_fork" &&
        edge.flow.routes.includes(alias),
    );
    if (forks.length === 0) return null;
    return forks.map(targetLabel).join(" + ");
  };
  const arms = behavior.routes.map(({ alias, key }) => {
    const dest = destination(alias);
    return dest === null ? `${key} (${alias})` : `${key} → ${dest} (${alias})`;
  });
  const forkNote =
    behavior.fork_branches.length > 0 ? ` ${behavior.fork_branches.length} fork branches.` : "";
  return `When ${behavior.condition} — ${arms.join(", ")}.${forkNote}`;
}

export function behaviorSummary(
  behavior: Exclude<ProposalNodeBehavior, { kind: "gate" }>,
  nodeLabel: (stableId: string) => string | null = () => null,
): string {
  switch (behavior.kind) {
    case "transform":
      return "Transforms each incoming item.";
    case "collector": {
      const opener = nodeLabel(behavior.opener_stable_id);
      const policyText = behavior.policy === "require_all"
        ? "requiring every member to arrive"
        : "releasing whichever members arrived (best effort)";
      return `Collects every row expanded by ${opener ?? "its scope opener"} and releases the group as one batch, ${policyText}.`;
    }
    case "aggregation": {
      const triggers: string[] = [];
      if (behavior.count !== null) triggers.push(`count ${behavior.count}`);
      if (behavior.timeout_seconds !== null) triggers.push(`timeout ${behavior.timeout_seconds}s`);
      if (behavior.trigger_kinds.includes("condition")) triggers.push("condition");
      return `Collects until ${triggers.join(" or ")}; ${behavior.output_mode} output.`;
    }
    case "queue":
      return "Queue continues in sequence without correlating records.";
    case "coalesce": {
      const timeout = behavior.timeout_seconds === null
        ? ""
        : `; timeout ${behavior.timeout_seconds}s`;
      // humanToken on the two enums: this sentence is DEFAULT-view copy on the
      // wire-stage rows now, and "require_all" is an internal identifier.
      return `Joins ${behavior.branch_aliases.join(", ")} using ${humanToken(behavior.policy)} / ${humanToken(behavior.merge)}${timeout}.`;
    }
    case "row_union": {
      const timeout = behavior.timeout_seconds === null
        ? ""
        : `; timeout ${behavior.timeout_seconds}s`;
      return `Waits for ${behavior.branch_aliases.join(", ")}, then forwards every row without merging records${timeout}.`;
    }
  }
}
