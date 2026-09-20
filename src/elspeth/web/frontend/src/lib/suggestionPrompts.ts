// ============================================================================
// suggestionPrompts.ts — the canned freeform prompts a click can send.
//
// The prompt is a planner call: the click sends ordinary chat text through
// useComposer.sendMessage, so the LLM does the job and no client path
// authors pipeline structure (AGENTS.md § Composer invariants). It costs a
// compose request.
//
// One home for the string so the two surfaces that offer Apply — the Checks
// tab's SideRailValidationBanner and the chat's DecisionPanel
// (elspeth-cb0d4b8dba) — send byte-identical prompts. The Apply text is
// pinned by SideRailValidationBanner.test.tsx since a58479c19; changing it
// changes what the planner has been taught to expect.
//
// There is deliberately NO "review again" prompt here. A compose turn that
// mutates nothing saves no composition-state row (routes/messages.py guards
// the save on a version change), so the durable completion gate is never
// rewritten and the block cannot clear; and an advisor FLAG on a turn with
// no runtime preflight publishes the fully blocking shape. A button that
// invites that turn would read as a fix and do the opposite. Only mutating
// prompts, such as Apply, can move the gate.
// ============================================================================

import type { ValidationEntryDTO } from "@/types/index";

/**
 * The DRAFT a blocker row's "Ask the composer about this" places in the chat
 * input (ruling D4, 2026-09-20). It is never sent by the click: the user
 * reads it, edits it and sends it, so the planner answers an ordinary user
 * turn. It asks; it does not say "fix" — a blocker carries no server-vetted
 * remedy, and some (an operator-held endpoint policy) cannot be cleared by
 * any pipeline edit. The blocker text is a block quotation because it can
 * carry user-authored step names.
 */
export function askAboutBlockerDraft(detail: string, stepPhrase: string | null): string {
  const subject = stepPhrase === null ? "this" : `this on ${stepPhrase}`;
  const quoted = detail.split("\n").map((line) => `> ${line}`).join("\n");
  return `I'm blocked by ${subject}:\n\n${quoted}\n\nWhat does it mean, and what are my options?`;
}

/** The canonical "apply this validator suggestion" chat prompt. */
export function applySuggestionPrompt(suggestion: ValidationEntryDTO): string {
  return `Please apply this suggestion to the pipeline:\n\n**${suggestion.component}:** ${suggestion.message}`;
}
