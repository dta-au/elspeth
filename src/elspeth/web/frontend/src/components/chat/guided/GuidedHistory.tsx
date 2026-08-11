// src/components/chat/guided/GuidedHistory.tsx
//
// Guided-mode decision summary. This is deliberately not a protocol log: the
// operator needs a visible recap of choices made so far, while low-level
// emitter/type/hash details stay out of the default workflow surface.
import type { GuidedStep, TurnRecord } from "@/types/guided";
import { GUIDED_STEP_LABELS } from "./stepLabels";

// ── Component ────────────────────────────────────────────────────────────────

interface Props {
  /** Completed turn records from GuidedSession.history. */
  history: TurnRecord[];
  /**
   * The step the learner is currently on. The in-progress step is NOT a
   * completed decision, so its turns are excluded even when an already-answered
   * sub-turn (e.g. the source single_select before the source schema_form) has
   * recorded a summary.
   */
  currentStep: GuidedStep;
}

/**
 * Select the visible decision recap rows from guided protocol history.
 *
 * This projection is the single availability rule for both the History tab
 * and the rendered history card: only summarised records outside the current
 * in-progress step are eligible, with the latest summary winning for each
 * completed step.
 */
export function projectCompletedGuidedHistory(
  history: readonly TurnRecord[],
  currentStep: GuidedStep,
): TurnRecord[] {
  const latestByStep = new Map<GuidedStep, TurnRecord>();
  for (const turn of history) {
    if (turn.step === currentStep) continue;
    if (turn.summary === null) continue;
    latestByStep.set(turn.step, turn);
  }
  return [...latestByStep.values()];
}

/**
 * Read-only plain-language list of completed guided-mode decisions.
 *
 * Returns null whenever the shared projection finds no completed, summarised
 * prior-step rows, including current-step-only and summary-null history.
 */
export function GuidedHistory({ history, currentStep }: Props): React.ReactElement | null {
  const rows = projectCompletedGuidedHistory(history, currentStep);

  // No completed decisions yet (e.g. still on the first step before any Send):
  // render nothing rather than an empty "Decisions so far" card.
  if (rows.length === 0) {
    return null;
  }

  return (
    <section
      className="guided-history"
      aria-labelledby="guided-history-heading"
    >
      <h2 id="guided-history-heading" className="guided-history-heading">
        Decisions so far
      </h2>
      <ol className="guided-history-list">
        {rows.map((turn) => (
          <li key={turn.step} className="guided-history-item">
            <span className="guided-history-step-name">
              {GUIDED_STEP_LABELS[turn.step]}
            </span>
            {/* Only past, summarised steps reach here (the current step and
                summary-null turns are filtered above), so every row has a real
                summary — no fallback needed. */}
            <span className="guided-history-summary">
              {turn.summary}
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}
