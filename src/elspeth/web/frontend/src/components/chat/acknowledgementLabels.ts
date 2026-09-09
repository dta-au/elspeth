// ============================================================================
// acknowledgementLabels.ts — the acknowledgement card's control vocabulary,
// and the rule for which controls a PENDING SET actually renders.
//
// Leaf module by design (no React import, no store import).  The prose
// surfaces that point AT a review card must name the exact buttons the card
// renders (elspeth-0a9f77dd75), so they interpolate these constants rather
// than repeating literals — but one of those surfaces is `stores/
// subscriptions.ts`, and importing them from `AcknowledgementCard.tsx` made
// the store-bootstrap module pull in a React component graph (CodeBlock,
// useInterpretationResolver).  That was the only store→components edge in the
// tree.  The constants live here instead; AcknowledgementCard, ChatInput and
// subscriptions all import from this leaf, so the anti-drift guarantee is
// unchanged and only the direction of the dependency moved.
// ============================================================================

import type { InterpretationEvent } from "@/types/interpretation";

/** Kinds the operator can amend inline (vague_term / legacy null). */
export function supportsAmendment(kind: InterpretationEvent["kind"]): boolean {
  return kind === null || kind === "vague_term";
}

/**
 * The card's visible primary-action labels.  Prose that points at the card
 * (the ChatInput pending-review placeholder, the subscriptions.ts validation
 * system note) interpolates these so the vocabularies structurally cannot
 * diverge again.  The "…" is U+2026 (one character), byte-exact with the
 * rendered button.
 *
 * The controls VARY BY KIND (ux-review 2026-08-13): prompt-template cards
 * render "View prompt" + "Approve" (never "Acknowledge"), and the "Change…"
 * amend affordance exists only where `supportsAmendment` says so.  Callers
 * must therefore go through `characterisePendingControls` rather than picking
 * a constant per event — naming a control the card does not render is the
 * exact drift these exports exist to prevent.
 */
export const ACKNOWLEDGEMENT_ACCEPT_LABEL = "Acknowledge";
export const ACKNOWLEDGEMENT_AMEND_LABEL = "Change…";
export const ACKNOWLEDGEMENT_APPROVE_LABEL = "Approve";
export const ACKNOWLEDGEMENT_VIEW_PROMPT_LABEL = "View prompt";

/**
 * Which controls EVERY card in a pending set renders.
 *
 * "unnameable" is the safe arm: an empty/stale pending map (the events stream
 * in on a separate fetch that can lag validation) or a mixed set that
 * includes a prompt card, where no single control name is true of every
 * visible card.  Callers must then fall back to wording that names no control
 * at all.  The invariant is one-way: never name a control the pending card(s)
 * do not render.
 */
export type PendingCardControls =
  | { readonly kind: "prompt_template"; readonly labels: readonly string[] }
  | { readonly kind: "amendable"; readonly labels: readonly string[] }
  | { readonly kind: "accept_only"; readonly labels: readonly string[] }
  | { readonly kind: "unnameable"; readonly labels: readonly [] };

/**
 * Characterise a pending set by the controls its cards share.
 *
 * Shared by both prose surfaces (elspeth-0a9f77dd75 sibling defect,
 * ux-review 2026-08-13): ChatInput's placeholder previously took the FIRST
 * pending event and spoke in the singular, so a mixed set named controls some
 * visible cards do not render.  Both callers now derive their wording from
 * this one rule.
 */
export function characterisePendingControls(
  kinds: readonly InterpretationEvent["kind"][],
): PendingCardControls {
  if (kinds.length === 0) return { kind: "unnameable", labels: [] };
  if (kinds.every((kind) => kind === "llm_prompt_template")) {
    return {
      kind: "prompt_template",
      labels: [
        ACKNOWLEDGEMENT_VIEW_PROMPT_LABEL,
        ACKNOWLEDGEMENT_APPROVE_LABEL,
      ],
    };
  }
  if (kinds.some((kind) => kind === "llm_prompt_template")) {
    return { kind: "unnameable", labels: [] };
  }
  // Every non-prompt card renders the accept label; only the amend half is
  // conditional, so name it only when EVERY card offers it.
  if (kinds.every((kind) => supportsAmendment(kind))) {
    return {
      kind: "amendable",
      labels: [ACKNOWLEDGEMENT_ACCEPT_LABEL, ACKNOWLEDGEMENT_AMEND_LABEL],
    };
  }
  return { kind: "accept_only", labels: [ACKNOWLEDGEMENT_ACCEPT_LABEL] };
}

/**
 * Render the "pick X (then|or) Y" instruction fragment for a pending set, or
 * null when the set is unnameable and the caller must use control-free
 * wording.
 *
 * `emphasise` lets each surface apply its own convention — markdown bold for
 * the injected system note, identity for the plain-text textarea placeholder
 * — without either surface re-deriving which controls exist.
 */
export function pendingControlsInstruction(
  controls: PendingCardControls,
  emphasise: (label: string) => string,
): string | null {
  switch (controls.kind) {
    case "prompt_template":
      return (
        `pick ${emphasise(ACKNOWLEDGEMENT_VIEW_PROMPT_LABEL)}, then ` +
        `${emphasise(ACKNOWLEDGEMENT_APPROVE_LABEL)}`
      );
    case "amendable":
      return (
        `pick ${emphasise(ACKNOWLEDGEMENT_ACCEPT_LABEL)} ` +
        `or ${emphasise(ACKNOWLEDGEMENT_AMEND_LABEL)}`
      );
    case "accept_only":
      return `pick ${emphasise(ACKNOWLEDGEMENT_ACCEPT_LABEL)}`;
    case "unnameable":
      return null;
  }
}
