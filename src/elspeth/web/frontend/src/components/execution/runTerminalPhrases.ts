// ============================================================================
// runTerminalPhrases
//
// ONE vocabulary for "the run reached a terminal state", shared by the two
// surfaces that announce that fact:
//
//   - ProgressView's polite live region (inside the Run panel), which appends
//     the run totals to the phrase.
//   - RunOutcomeNotice's toast + live region (App level, off the Run tab),
//     which has no counters to append.
//
// Before this module the two surfaces owned separate literals and had already
// drifted to three noun phrases for one event ("Pipeline completed — …",
// "Pipeline execution was cancelled", "Pipeline run completed."). An operator
// switching tabs heard the same fact in two wordings. Same anti-drift shape as
// the exported ACKNOWLEDGEMENT_*_LABEL constants: the vocabularies are now
// structurally unable to diverge, because there is only one copy.
//
// The map stores a STEM (subject + verb, no terminal punctuation, no totals)
// rather than a finished sentence, because the two callers punctuate
// differently. `takesTotals: false` marks the two statuses whose phrasing is
// already complete: `empty` states its own reason (there are no per-token
// counts worth reading back) and `cancelled` reports an operator action, not
// a measurement. Appending totals to either would be noise at best and, for
// `empty`, a contradiction.
//
// Pure data + string composition: no React, no store reads, so either surface
// can import it without dragging the other's dependency graph along.
// ============================================================================

import type { TerminalRunStatus } from "@/types/index";

interface TerminalRunPhrase {
  /** Subject + verb clause, no trailing punctuation, no totals clause. */
  stem: string;
  /** Whether a caller holding run totals may append them to the stem. */
  takesTotals: boolean;
}

/** Exhaustive by construction: `Record<TerminalRunStatus, …>` means adding a
 *  terminal status to TERMINAL_RUN_STATUS_VALUES fails to compile here until
 *  its phrasing is decided, rather than silently falling back to a default. */
const TERMINAL_RUN_PHRASES: Record<TerminalRunStatus, TerminalRunPhrase> = {
  completed: { stem: "Pipeline completed", takesTotals: true },
  completed_with_failures: {
    stem: "Pipeline completed with failures",
    takesTotals: true,
  },
  failed: { stem: "Pipeline failed", takesTotals: true },
  empty: { stem: "Pipeline completed — no rows processed", takesTotals: false },
  cancelled: { stem: "Pipeline execution was cancelled", takesTotals: false },
};

/**
 * The terminal phrase as a standalone sentence — the form the App-level
 * RunOutcomeNotice uses, where no run counters are in hand.
 */
export function terminalRunPhrase(status: TerminalRunStatus): string {
  return `${TERMINAL_RUN_PHRASES[status].stem}.`;
}

/**
 * The terminal phrase with the run totals appended — the form ProgressView's
 * live region uses. `totals` is ignored for the statuses whose phrasing is
 * already complete, so a caller never has to special-case them.
 */
export function terminalRunAnnouncement(
  status: TerminalRunStatus,
  totals: string,
): string {
  const phrase = TERMINAL_RUN_PHRASES[status];
  return phrase.takesTotals
    ? `${phrase.stem} — ${totals}.`
    : `${phrase.stem}.`;
}
