// src/components/chat/guided/explainPrompt.ts

/**
 * Canned chat message sent by the decision card's "Explain this step" button.
 * It rides the NORMAL guided-chat path (a real user turn + assistant reply in
 * the transcript, pending strip while it runs) — the button is one-click
 * sugar for typing the question, which matters most in the tutorial where the
 * locked prompt box leaves learners no way to ask "why?".
 *
 * ChatPanel's tutorialStepBuilt check EXCLUDES turns with exactly this
 * content: on the confirm-only tutorial wire step an Explain send must
 * not read as "the step's prompt was sent" and prematurely swap the locked
 * box for the Sent line. Exact string identity is the filter — change the
 * copy here and nowhere else.
 */
export const GUIDED_EXPLAIN_MESSAGE =
  "Explain what I'm seeing on this step: what has been set up so far, why you chose it, and what the settings mean.";

/**
 * Canned chat message sent by the COMPLETED surface's "Explain this pipeline"
 * button (elspeth-986801d218). Same one-click-sugar contract as the per-step
 * message above — an ordinary user turn on the ordinary guided-chat path —
 * but it asks about the COMMITTED pipeline as a whole rather than about a
 * step still under construction, because after Confirm wiring there is no
 * current step to explain and no wizard control left to point at.
 *
 * Deliberately a SEPARATE constant, not a widened per-step map: the
 * tutorialStepBuilt filter in ChatPanel excludes GUIDED_EXPLAIN_MESSAGE by
 * exact string identity, and that filter is an ACTIVE-stage concept. Merging
 * the two strings would silently enrol this message in that rule.
 *
 * The third clause is scoped to confirmation time ON PURPOSE, and asks about
 * the review COUNTS rather than what they say. The committed context opener
 * (chat_solver.py, build_step_chat_context_block) tells the model "never state
 * whether the pipeline is valid or ready to run" — that context carries only
 * the review_status_at_confirmation counts, whose name says outright that they
 * were RECORDED AT CONFIRMATION, not the head record's
 * current is_valid, and a completion whose heading reads "Review required" can
 * sit behind a confirmation-time all-clear. An unscoped "what the validation
 * result means" therefore asks the one question the context is forbidden to
 * answer, and the counts are the only data left to answer it with. Asking what
 * the checks COVERED is likewise wrong: warning and blocker prose is on the
 * projection's `omitted` list, so that phrasing buys a guaranteed partial
 * refusal. Keep the "recorded at confirmation" scope and the counts framing if
 * you reword this; "step" vocabulary is also deliberately absent, because a
 * committed build has no wizard step left to name.
 */
export const GUIDED_EXPLAIN_PIPELINE_MESSAGE =
  "Explain the pipeline I just built: what each component does, how they are " +
  "connected, and what the review counts recorded at confirmation mean.";
