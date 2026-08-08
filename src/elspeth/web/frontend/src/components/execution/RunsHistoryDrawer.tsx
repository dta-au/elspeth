// ============================================================================
// RunsHistoryDrawer
//
// Slide-over drawer listing every run for the current session. Opened from
// InlineRunResults' "Runs" button. Preserves audit-trail access to old
// runs after the inspector Runs tab is removed.
// ============================================================================

import { useEffect, useRef, useState } from "react";
import { useExecutionStore } from "@/stores/executionStore";
import { useSessionStore } from "@/stores/sessionStore";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { ConfirmDialog } from "@/components/common/ConfirmDialog";
import { RunOutputsPanel } from "@/components/inspector/RunOutputsPanel";
import { Button, StatusBadge } from "@/components/ui";
import { isTerminalRunStatus } from "@/types/index";
import type { Run, RunDiagnostics, RunDiagnosticsWorkingView } from "@/types/index";

interface RunsHistoryDrawerProps {
  onClose: () => void;
  runsOverride?: ReadonlyArray<Run>;
}

const RUN_DATE_TIME_FORMATTER = new Intl.DateTimeFormat(undefined, {
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "numeric",
  minute: "2-digit",
});

interface NumberedRun {
  run: Run;
  ordinal: number;
}

function formatRunStart(startedAt: string): string {
  const date = new Date(startedAt);
  return Number.isNaN(date.getTime())
    ? "Time unavailable"
    : RUN_DATE_TIME_FORMATTER.format(date);
}

function labelRun(run: Run, ordinal: number): string {
  return `Run ${ordinal} · ${formatRunStart(run.started_at)}`;
}

function numberRunsNewestFirst(runs: ReadonlyArray<Run>): NumberedRun[] {
  // Run.started_at is required by the API contract. Retain input order for
  // defensive/test fixtures that pre-date that field rather than inventing a
  // chronology from UUIDs.
  if (runs.some((run) => Number.isNaN(new Date(run.started_at).getTime()))) {
    return runs.map((run, index) => ({ run, ordinal: index + 1 }));
  }
  const ascending = runs
    .map((run, inputIndex) => ({ run, inputIndex }))
    .sort((left, right) => {
      const byStart = (left.run.started_at ?? "").localeCompare(
        right.run.started_at ?? "",
      );
      if (byStart !== 0) return byStart;
      const byId = left.run.id.localeCompare(right.run.id);
      return byId !== 0 ? byId : left.inputIndex - right.inputIndex;
    });

  return ascending
    .map(({ run }, index) => ({ run, ordinal: index + 1 }))
    .reverse();
}

function counted(label: string, count: number): string {
  return count === 1 ? `1 ${label}` : `${count.toLocaleString()} ${label}s`;
}

function summarizeCounts(prefix: string, counts: Record<string, number>): string | null {
  const entries = Object.entries(counts).sort(([left], [right]) => left.localeCompare(right));
  if (entries.length === 0) return null;
  return `${prefix} include ${entries.map(([name, count]) => `${name}=${count}`).join(", ")}.`;
}

function buildVisibleEvidence(diagnostics: RunDiagnostics): string[] {
  const evidence: string[] = [];
  if (diagnostics.cancel_requested) {
    evidence.push("Cancellation has been requested; active work is draining toward a terminal cancelled status.");
  }
  const tokenCount = diagnostics.summary.token_count;
  if (tokenCount > 0) {
    evidence.push(`${counted("token", tokenCount)} ${tokenCount === 1 ? "is" : "are"} visible in the runtime trace.`);
    if (diagnostics.summary.preview_truncated) {
      evidence.push(`The preview is limited to the first ${counted("token", diagnostics.summary.preview_limit)}.`);
    }
  }
  const stateSummary = summarizeCounts("Node states", diagnostics.summary.state_counts);
  if (stateSummary) evidence.push(stateSummary);
  const operationSummary = summarizeCounts("Operation records", diagnostics.summary.operation_counts);
  if (operationSummary) evidence.push(operationSummary);
  if (evidence.length === 0) {
    evidence.push("No tokens or operations are visible yet.");
  }
  return evidence;
}

function buildPendingWorkingView(diagnostics: RunDiagnostics): RunDiagnosticsWorkingView {
  return {
    headline: "Reading current run evidence",
    evidence: buildVisibleEvidence(diagnostics),
    meaning: "The LLM is reading the same run records shown here and preparing a plain-English explanation.",
    next_steps: [],
  };
}

interface VisibleStateFailure {
  label: string;
  reason: string | null;
  cause: string | null;
  hint: string | null;
}

// Mirrors the repository's audit-export identifier boundary: one ASCII
// alphanumeric followed by at most 127 ASCII alphanumeric/code punctuation
// characters. Diagnostic identifiers are rendered verbatim, so whitespace,
// markup, control characters, and unbounded provider text are never admitted.
const DIAGNOSTIC_IDENTIFIER_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const TEXTRACT_S3_UNREADABLE_HINT_MAX_CHARS = 512;
const TEXTRACT_S3_UNREADABLE_HINT_PREFIX =
  "Amazon Textract could not read the S3 object. Most commonly the object is outside the ";
const TEXTRACT_S3_UNREADABLE_HINT =
  TEXTRACT_S3_UNREADABLE_HINT_PREFIX +
  "S3 read scope granted to the pipeline's AWS role (check the role's s3:GetObject prefix " +
  "against the document's bucket/key and, when version_field is configured, its " +
  "s3:GetObjectVersion permission); it can also mean the object does not exist or is stored " +
  "in a different region than the Textract endpoint. Verify access scope before suspecting " +
  "a corrupt or unsupported file.";

function safeDiagnosticIdentifier(value: unknown): string | null {
  return typeof value === "string" && DIAGNOSTIC_IDENTIFIER_PATTERN.test(value)
    ? value
    : null;
}

function safeTextractS3UnreadableHint({
  code,
  errorType,
  reason,
  cause,
  error,
}: {
  code: string | null;
  errorType: string | null;
  reason: string | null;
  cause: string | null;
  error: unknown;
}): string | null {
  if (
    code !== "InvalidS3ObjectException" ||
    errorType !== "service_error" ||
    reason !== "submit_failed" ||
    cause !== "s3_object_unreadable" ||
    typeof error !== "string" ||
    error.length > TEXTRACT_S3_UNREADABLE_HINT_MAX_CHARS ||
    !error.startsWith(TEXTRACT_S3_UNREADABLE_HINT_PREFIX) ||
    error !== TEXTRACT_S3_UNREADABLE_HINT
  ) {
    return null;
  }
  return error;
}

function visibleStateFailure(error: unknown): VisibleStateFailure | null {
  if (error === null || typeof error !== "object" || Array.isArray(error)) {
    return null;
  }

  const record = error as Record<string, unknown>;
  const code = safeDiagnosticIdentifier(record.code);
  const errorType = safeDiagnosticIdentifier(record.error_type);
  const reason = safeDiagnosticIdentifier(record.reason);
  const cause = safeDiagnosticIdentifier(record.cause);
  const hint = safeTextractS3UnreadableHint({
    code,
    errorType,
    reason,
    cause,
    error: record.error,
  });

  if (code === null && errorType === null && reason === null && cause === null && hint === null) {
    return null;
  }

  // State error objects can contain audit-only raw response previews,
  // offending values, and free-text provider detail. Render only the closed,
  // scalar identifiers needed by an operator. The one longer hint admitted
  // here is authored by ELSPETH for its exact S3-unreadable classification.
  return {
    label: code ?? errorType ?? reason ?? "recorded failure",
    reason,
    cause,
    hint,
  };
}

function RunStateFailureDetail({
  error,
  nodeId,
  stateId,
}: {
  error: unknown;
  nodeId: string;
  stateId: string;
}): JSX.Element | null {
  const failure = visibleStateFailure(error);
  if (failure === null) return null;

  return (
    <div
      className="run-diagnostics-state-failure"
      data-testid={`run-state-failure-${stateId}`}
    >
      <div>
        {nodeId} failed - {failure.label}
      </div>
      {failure.reason && <div>Reason: {failure.reason}</div>}
      {failure.cause && <div>Cause: {failure.cause}</div>}
      {failure.hint && <div>{failure.hint}</div>}
    </div>
  );
}

export function RunsHistoryDrawer({ onClose, runsOverride }: RunsHistoryDrawerProps): JSX.Element {
  const storeRuns = useExecutionStore((s) => s.runs);
  const runs = runsOverride ?? storeRuns;
  const diagnosticsByRunId = useExecutionStore((s) => s.diagnosticsByRunId);
  const diagnosticsLoadingByRunId = useExecutionStore((s) => s.diagnosticsLoadingByRunId);
  const diagnosticsEvaluatingByRunId = useExecutionStore((s) => s.diagnosticsEvaluatingByRunId);
  const diagnosticsErrorByRunId = useExecutionStore((s) => s.diagnosticsErrorByRunId);
  const diagnosticsExplanationByRunId = useExecutionStore((s) => s.diagnosticsExplanationByRunId);
  const diagnosticsWorkingViewByRunId = useExecutionStore((s) => s.diagnosticsWorkingViewByRunId);
  const loadRunDiagnostics = useExecutionStore((s) => s.loadRunDiagnostics);
  const evaluateRunDiagnostics = useExecutionStore((s) => s.evaluateRunDiagnostics);
  const cancel = useExecutionStore((s) => s.cancel);
  // Title-first convention (HeaderSessionSwitcher): never surface the raw
  // session UUID in user-facing chrome (elspeth-ef8c18a6cb).
  const activeSessionTitle = useSessionStore(
    (s) =>
      s.sessions.find((session) => session.id === s.activeSessionId)?.title ??
      null,
  );
  const [expandedRunId, setExpandedRunId] = useState<string | null>(null);
  const [cancelTargetRunId, setCancelTargetRunId] = useState<string | null>(null);
  const drawerRef = useRef<HTMLDivElement>(null);
  const numberedRuns = numberRunsNewestFirst(runs);

  // M08 (WCAG 2.4.3): the shared focus trap moves focus to the Close button on
  // open, wraps Tab/Shift+Tab, and — unlike the previous bespoke trap —
  // restores focus to the opener when the drawer unmounts.
  useFocusTrap(drawerRef, true, ".runs-history-drawer-header button");

  // useFocusTrap does not own Escape; keep the drawer's own close-on-Escape.
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        onClose();
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  return (
    <div
      ref={drawerRef}
      role="dialog"
      aria-modal="true"
      aria-label="Pipeline runs"
      className="runs-history-drawer"
    >
      <header className="runs-history-drawer-header">
        <h2>Run history · {runs.length}</h2>
        <Button
          variant="ghost"
          compact
          className="runs-history-close"
          aria-label="Close runs"
          onClick={onClose}
        >
          <span aria-hidden="true">×</span>
        </Button>
      </header>
      <div className="runs-history-drawer-body">
        {runs.length === 0 ? (
          <p>
            No prior runs for{" "}
            {activeSessionTitle ? `"${activeSessionTitle}"` : "this session"}.
          </p>
        ) : (
          <ul className="runs-history-list">
            {numberedRuns.map(({ run, ordinal }) => {
              const runLabel = labelRun(run, ordinal);
              return (
              <li key={run.id} className="runs-history-item">
                <div className="runs-history-item-summary">
                  <span className="runs-history-item-identity">
                    <span className="runs-history-item-label">{runLabel}</span>
                    <span className="runs-history-item-id">{run.id}</span>
                  </span>
                  <span className="runs-history-item-actions">
                  {/* ui/StatusBadge carries the a11y glyph map (⚠ / ∅) so
                      completed_with_failures and empty are not colour-only
                      distinctions (elspeth-e1c5ad0b53). */}
                  <StatusBadge status={run.status}>
                    {run.status.replace(/_/g, " ")}
                  </StatusBadge>
                  {/* Explicit per-run audit-integrity failure
                      (elspeth-d5578ccd98): the backend ships this marker
                      INSTEAD of accounting when the run's recorded token
                      outcomes fail canonical validation. Text marker, not
                      colour-only, matching the a11y stance above. */}
                  {run.accounting_corruption ? (
                    <span
                      className="runs-history-item-corruption"
                      role="alert"
                      title={run.accounting_corruption.violations.join("\n")}
                    >
                      ⚠ audit accounting corrupt
                    </span>
                  ) : null}
                  {/* REST-backed Cancel for live runs (elspeth-90db33baac):
                      works without the in-memory activeRunId/WebSocket that
                      gates ProgressView's Cancel, so a run stays cancellable
                      after a page reload. */}
                  {!isTerminalRunStatus(run.status) && (
                    <Button
                      variant="danger"
                      className="btn-small"
                      aria-label={`Cancel run ${run.id}: ${runLabel}`}
                      disabled={run.cancel_requested === true}
                      onClick={() => setCancelTargetRunId(run.id)}
                    >
                      {run.cancel_requested === true ? "Cancelling..." : "Cancel"}
                    </Button>
                  )}
                  <Button
                    aria-expanded={expandedRunId === run.id}
                    aria-controls={`run-history-diagnostics-${run.id}`}
                    aria-label={
                      expandedRunId === run.id
                        ? `Hide detail for ${run.id}: ${runLabel}`
                        : `Show detail for ${run.id}: ${runLabel}`
                    }
                    className="btn-small"
                    onClick={() => {
                      const nextRunId = expandedRunId === run.id ? null : run.id;
                      setExpandedRunId(nextRunId);
                      if (nextRunId) {
                        void loadRunDiagnostics(run.id);
                      }
                    }}
                  >
                    {expandedRunId === run.id ? "Hide detail" : "Show detail"}
                  </Button>
                  </span>
                </div>
                <div
                  id={`run-history-diagnostics-${run.id}`}
                  hidden={expandedRunId !== run.id}
                  className="run-diagnostics"
                >
                  {expandedRunId === run.id && (
                    <>
                      <RunDiagnosticsPanel
                        diagnostics={diagnosticsByRunId[run.id]}
                        error={diagnosticsErrorByRunId[run.id] ?? null}
                        explanation={diagnosticsExplanationByRunId[run.id] ?? null}
                        isEvaluating={diagnosticsEvaluatingByRunId[run.id] ?? false}
                        isLoading={diagnosticsLoadingByRunId[run.id] ?? false}
                        runError={run.error ?? null}
                        workingView={diagnosticsWorkingViewByRunId[run.id] ?? null}
                        onExplain={() => void evaluateRunDiagnostics(run.id)}
                        onRefresh={() => void loadRunDiagnostics(run.id)}
                      />
                      <RunOutputsPanel runId={run.id} />
                    </>
                  )}
                </div>
              </li>
              );
            })}
          </ul>
        )}
      </div>
      {cancelTargetRunId !== null && (
        <ConfirmDialog
          title="Cancel pipeline"
          message="Cancel the running pipeline? This cannot be undone."
          confirmLabel="Cancel pipeline"
          variant="danger"
          onConfirm={() => {
            void cancel(cancelTargetRunId);
            setCancelTargetRunId(null);
          }}
          onCancel={() => setCancelTargetRunId(null)}
        />
      )}
    </div>
  );
}

interface RunDiagnosticsPanelProps {
  diagnostics: RunDiagnostics | undefined;
  error: string | null;
  explanation: string | null;
  isEvaluating: boolean;
  isLoading: boolean;
  runError: string | null;
  workingView: RunDiagnosticsWorkingView | null;
  onExplain: () => void;
  onRefresh: () => void;
}

function RunDiagnosticsPanel({
  diagnostics,
  error,
  explanation,
  isEvaluating,
  isLoading,
  runError,
  workingView,
  onExplain,
  onRefresh,
}: RunDiagnosticsPanelProps): JSX.Element {
  const visibleWorkingView =
    workingView ?? (isEvaluating && diagnostics ? buildPendingWorkingView(diagnostics) : null);

  return (
    <div className="run-diagnostics-panel">
      <div className="run-diagnostics-panel-header">
        <span>
          {diagnostics
            ? `${diagnostics.summary.token_count.toLocaleString()} tokens`
            : isLoading
              ? "Loading diagnostics..."
              : "Diagnostics not loaded"}
          {diagnostics?.summary.preview_truncated ? `, first ${diagnostics.summary.preview_limit}` : ""}
        </span>
        <span className="run-diagnostics-actions">
          <Button className="btn-small" onClick={onRefresh} disabled={isLoading}>
            Refresh
          </Button>
          <Button
            className="btn-small"
            onClick={onExplain}
            disabled={isEvaluating || isLoading || !diagnostics}
          >
            {isEvaluating ? "Explaining..." : "Explain"}
          </Button>
        </span>
      </div>

      {error && <div role="alert">{error}</div>}

      {diagnostics?.failure_detail && (
        <div role="alert" data-testid="run-failure-detail" className="run-failure-detail">
          <div>
            {diagnostics.failure_detail.operation_type} failed - {diagnostics.failure_detail.node_id}
          </div>
          <pre>{diagnostics.failure_detail.error_message}</pre>
        </div>
      )}

      {runError !== null && runError.trim().length > 0 && !diagnostics?.failure_detail && (
        <div role="alert" data-testid="run-stored-failure-detail" className="run-failure-detail">
          <div>Stored failure cause</div>
          <pre>{runError}</pre>
        </div>
      )}

      {diagnostics && (
        <>
          {diagnostics.operations.length > 0 && (
            <div className="run-diagnostics-operations">
              {diagnostics.operations.map((operation) => (
                <span key={operation.operation_id}>
                  {operation.operation_type} {operation.status}
                </span>
              ))}
            </div>
          )}

          {diagnostics.tokens.length > 0 && (
            <div className="run-diagnostics-tokens">
              {diagnostics.tokens.map((token) => (
                <div key={token.token_id}>
                  <span>{token.token_id}</span>{" "}
                  <span>
                    row {token.row_index ?? "-"}
                    {token.terminal_outcome ? `, ${token.terminal_outcome}` : ""}
                    {token.states.map((state) => (
                      <span key={state.state_id}>
                        {" "}
                        {state.node_id} {state.status}
                      </span>
                    ))}
                  </span>
                  {token.states.map((state) =>
                    state.status === "failed" ? (
                      <RunStateFailureDetail
                        key={`${state.state_id}-failure`}
                        error={state.error}
                        nodeId={state.node_id}
                        stateId={state.state_id}
                      />
                    ) : null,
                  )}
                </div>
              ))}
            </div>
          )}
        </>
      )}

      {visibleWorkingView && (
        <div className="run-diagnostics-working-view">
          <div>{visibleWorkingView.headline}</div>
          {visibleWorkingView.evidence.length > 0 && (
            <ul>
              {visibleWorkingView.evidence.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          )}
          <div>{visibleWorkingView.meaning}</div>
          {visibleWorkingView.next_steps.length > 0 && (
            <ul>
              {visibleWorkingView.next_steps.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {!visibleWorkingView && explanation && (
        <div className="run-diagnostics-explanation">{explanation}</div>
      )}
    </div>
  );
}
