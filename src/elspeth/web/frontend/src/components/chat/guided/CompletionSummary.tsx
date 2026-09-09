// src/components/chat/guided/CompletionSummary.tsx
//
// Guided-mode terminal widget for the "completed" outcome (Task 7.10).
// Conventions inherited from ExitToFreeformButton (Task 7.8) and
// Current guided completion projection.
//
//   - Props: { terminal: TerminalState } -- read-only consumer of the terminal.
//   - Renders only when terminal.kind === "completed".
//   - Keeps the readiness-derived outcome and the guided-to-freeform state
//     transition. YAML, validation, and run history belong to the surrounding
//     common workspace.
//   - <button type="button"> (never <div onClick>).
//   - CSS via components/chat/guided/guided.css guided-completion-* classes.
//   - No auto-focus on mount.

import { Button } from "@/components/ui";
import { useSessionStore } from "@/stores/sessionStore";
import {
  COMPLETION_OUTCOME_LABELS,
  useCompletionOutcome,
} from "../completionOutcome";
import type { TerminalState } from "@/types/guided";

// ── Props ─────────────────────────────────────────────────────────────────────

interface CompletionSummaryProps {
  terminal: TerminalState;
  // Concern B: in a tutorial the "Open freeform editor" action is suppressed
  // (the only path out of a tutorial is graduation, never freeform).
  isTutorial?: boolean;
}

// ── Component ─────────────────────────────────────────────────────────────────

export function CompletionSummary({ terminal, isTutorial }: CompletionSummaryProps) {
  if (terminal.kind !== "completed") {
    return null;
  }

  return <CompletionSummaryInner isTutorial={isTutorial} />;
}

// ── Inner component (separated so hooks run unconditionally) ──────────────────
//
// React hooks must not be called conditionally.  The outer CompletionSummary
// returns null before reaching any hook calls.  CompletionSummaryInner is the
// always-rendered branch; it can call hooks without violating the Rules of Hooks.

interface CompletionSummaryInnerProps {
  isTutorial?: boolean;
}

function CompletionSummaryInner({ isTutorial }: CompletionSummaryInnerProps) {
  const exitToFreeform = useSessionStore((s) => s.exitToFreeform);
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  // Heading honesty (elspeth-bf9c296ee5): a guided completion always carries
  // a composed pipeline (pipelineMutated: true), so the heading ladders over
  // Review required / Pipeline ready / Pipeline updated from the same
  // signals that gate Run — it must never claim "ready" while the
  // acknowledgement stack above still blocks execution or before the backend
  // has admitted the pipeline.
  const completionOutcome = useCompletionOutcome(activeSessionId, true);

  function handleExit(): void {
    void exitToFreeform();
  }

  return (
    <div className="guided-completion">
      {/* Heading per Task 7.6 M3 convention for primary entity names */}
      <h3 className="guided-completion-heading">
        {COMPLETION_OUTCOME_LABELS[completionOutcome]}
      </h3>

      {!isTutorial && (
        <Button
          variant="bare"
          className="guided-completion-save-btn"
          onClick={handleExit}
        >
          Open freeform editor
        </Button>
      )}
    </div>
  );
}
