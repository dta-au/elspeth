import type { GuidedSession } from "@/types/guided";
import { GUIDED_EXPLAIN_MESSAGE } from "./explainPrompt";

/**
 * A rationale is a HEADLINE (rendered as the decision card's h2, plain text,
 * no markdown). Long multi-paragraph replies belong in the transcript bubble
 * — using them verbatim here produced a wall of bold text when a model wrote
 * an essay (or leaked its tool scratchpad) into assistant_message. Beyond
 * this length the caller falls back to the static step purpose.
 */
const RATIONALE_MAX_LENGTH = 200;

/**
 * The headline renders as PLAIN TEXT in an h2, so markdown emphasis arrives
 * as literal asterisks ("You're at **Step 1: Source**" — operator-observed).
 * Unwrap the common inline tokens; this is a display sanitiser, not a parser
 * — anything it doesn't recognise passes through unchanged.
 */
function stripInlineMarkdown(line: string): string {
  return line
    .replace(/^#{1,6}\s+/, "")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/`([^`]+)`/g, "$1");
}

/**
 * Openers that mean the line is chat prose, not a decision name
 * (elspeth-bc8a35c1ab). The headline must say WHAT was decided; "Here is an
 * updated proposal." and "Great — you've given me the exact rows…" say
 * nothing, and echoing them into the h2 duplicates the bubble directly below.
 *
 * These are display sanitisers on the same footing as `stripInlineMarkdown`:
 * rejecting is always safe because the caller falls back to the static step
 * purpose, so they lean toward rejecting a marginal line rather than letting
 * conversational filler occupy the decision card.
 */

/** "Here is/Here's/Here are …" — announces a thing without naming it. */
const PRESENTATION_OPENER = /^here(?:['’]s| is| are)\b/i;

/**
 * A social interjection ("Great —", "Sure,", "Perfect."). Anchored on trailing
 * punctuation so it only fires on the interjection use: "Good rows land in the
 * sink." is a headline, "Good. Rows land in the sink." is chat.
 */
const ACKNOWLEDGEMENT_OPENER =
  /^(?:great|sure|thanks|thank you|perfect|excellent|awesome|nice|good|got it|ok|okay|alright|absolutely|certainly|of course|no problem|understood|happy to|glad to)(?:\s*[,.!?]|\s+[-–—]|$)/i;

/**
 * Future-intent narration ("Let me…", "I'll…"). Past-tense first person
 * ("I've set the source to inline CSV") is deliberately NOT here — that
 * reports a completed decision and makes a legitimate headline.
 */
const INTENT_OPENER =
  /^(?:let me|let['’]?s|i['’]?ll|i will|i ?a?m going to|i can|i['’]?d|i would|now i|next i)\b/i;

/**
 * Two or more sentences on the line — a paragraph, not a headline. Matches a
 * terminator followed by whitespace and more content, so a single trailing
 * full stop ("Sink set.") and inline decimals ("Uses v1.2 of the schema.")
 * both pass.
 */
const MULTIPLE_SENTENCES = /[.!?]["'’)\]]?\s+\S/;

/** True when the line reads as conversation rather than a named decision. */
function isConversationalProse(line: string): boolean {
  return (
    PRESENTATION_OPENER.test(line) ||
    ACKNOWLEDGEMENT_OPENER.test(line) ||
    INTENT_OPENER.test(line) ||
    MULTIPLE_SENTENCES.test(line)
  );
}

/**
 * The current step's latest assistant rationale (the LLM's "what I built"
 * summary), used as the prominent decision headline. Highest-seq assistant
 * turn whose step matches the active step; null when none OR when the turn
 * doesn't read as a headline (the caller falls back to the static step
 * purpose):
 *  - synthetic-failure turns are SKIPPED (C-2, composer first-principles
 *    review 2026-07-04) — a scaffold-guard rejection or "unavailable"
 *    message is not a build rationale; it must never become the "Current
 *    decision" headline. Because this is a per-step filter (matched against
 *    `session.step`), a synthetic turn from a step the wizard has since
 *    left behind is already excluded by the step check below — the
 *    exclusion here additionally covers a synthetic turn on the CURRENT
 *    step (e.g. a retry that hasn't produced a real reply yet),
 *  - replies to the Explain button's canned question are SKIPPED — an
 *    explanation of the current state is not a build rationale, and letting
 *    it hijack the headline replaced "what I built" with "You're at Step 1…"
 *    (identified by the immediately preceding user turn being the exact
 *    canned message),
 *  - only the FIRST non-empty line is considered (the full reply lives in
 *    the conversation bubble; the headline must not duplicate it), and
 *  - a line that is over-long or carries tool-call scaffolding is rejected;
 *    inline markdown emphasis is unwrapped (the h2 is plain text).
 */
export function latestAssistantRationale(session: GuidedSession): string | null {
  const bySeq = new Map(session.chat_history.map((turn) => [turn.seq, turn]));
  let best: { seq: number; content: string } | null = null;
  for (const turn of session.chat_history) {
    if (turn.role !== "assistant" || turn.step !== session.step) continue;
    if (turn.assistant_message_kind === "synthetic_failure") continue;
    const preceding = bySeq.get(turn.seq - 1);
    if (
      preceding !== undefined &&
      preceding.role === "user" &&
      preceding.content === GUIDED_EXPLAIN_MESSAGE
    ) {
      continue;
    }
    if (best === null || turn.seq > best.seq) best = { seq: turn.seq, content: turn.content };
  }
  if (best === null) return null;
  const firstLine = best.content
    .split("\n")
    .map((line) => line.trim())
    .find((line) => line !== "");
  if (firstLine === undefined || firstLine.length > RATIONALE_MAX_LENGTH) return null;
  if (/<\/?tool_(call|response)/i.test(firstLine)) return null;
  const headline = stripInlineMarkdown(firstLine);
  // Checked AFTER unwrapping so "**Here is** an updated proposal." is caught
  // by the same predicate as its unformatted twin.
  if (isConversationalProse(headline)) return null;
  return headline;
}
