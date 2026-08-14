// ============================================================================
// InlineRunResults
//
// Mounts in the persistent Run artifact and renders the active or most-recent
// run's progress and outputs. Historical access lives in RunsHistoryDrawer.
// ============================================================================

import { useEffect, useState } from "react";
import { Button, Icon } from "@/components/ui";
import {
  type RunHistoryLoadOutcome,
  useExecutionStore,
} from "@/stores/executionStore";
import { useSessionStore } from "@/stores/sessionStore";
import { ProgressView } from "@/components/execution/ProgressView";
import { RunOutputsPanel } from "@/components/inspector/RunOutputsPanel";
import { NarrativeResults } from "@/components/composer/NarrativeResults";
import { useNarrativeMode } from "@/hooks/useNarrativeMode";
import {
  isTerminalRunStatus,
  type DiscardStageSummary,
  type DiscardSummary,
  type Run,
  type RunDiagnosticDiscard,
  type RunProgress,
  type RunStatus,
} from "@/types/index";
import { plural } from "@/utils/plural";
import { REQUEST_RUN_EVENT } from "@/lib/composer-events";
import { RunsHistoryDrawer } from "./RunsHistoryDrawer";

function statusLabel(status: RunStatus | null): string {
  if (!status) return "No run";
  switch (status) {
    case "completed":
      return "Completed";
    case "completed_with_failures":
      return "Completed with failures";
    case "failed":
      return "Failed";
    case "empty":
      return "Empty";
    case "cancelled":
      return "Cancelled";
    case "running":
      return "Running";
    case "pending":
      return "Pending";
  }
}

function runSummaryParts(
  status: RunStatus | null,
  progress: RunProgress | null,
  run: Run | null,
): string[] {
  const accounting = progress?.accounting ?? run?.accounting ?? null;
  const rows =
    progress?.source_rows_processed ?? accounting?.source.rows_processed ?? null;
  const succeeded =
    progress?.tokens_succeeded ?? accounting?.tokens.succeeded ?? null;
  const failed = progress?.tokens_failed ?? accounting?.tokens.failed ?? null;
  const parts = [statusLabel(status)];
  if (rows !== null) parts.push(`${rows} ${rows === 1 ? "row" : "rows"}`);
  if (succeeded !== null) {
    parts.push(`${succeeded} succeeded`);
  }
  if (failed !== null) {
    parts.push(`${failed} failed`);
  }
  const discardTotal = run?.discard_summary?.total ?? 0;
  if (discardTotal > 0) {
    parts.push(`${discardTotal} discarded`);
  }
  return parts;
}

function sourceValidationFallbackStage(summary: DiscardSummary): DiscardStageSummary | null {
  if (summary.validation_errors <= 0) return null;
  return {
    stage: "source_validation",
    node_id: null,
    count: summary.validation_errors,
  };
}

function transformValidationFallbackStage(summary: DiscardSummary): DiscardStageSummary | null {
  if (summary.transform_errors <= 0) return null;
  return {
    stage: "transform_validation",
    node_id: null,
    count: summary.transform_errors,
  };
}

function gateEvaluationFallbackStage(summary: DiscardSummary): DiscardStageSummary | null {
  if (summary.gate_errors <= 0) return null;
  return {
    stage: "gate_evaluation",
    node_id: null,
    count: summary.gate_errors,
  };
}

function sinkDiscardFallbackStage(summary: DiscardSummary): DiscardStageSummary | null {
  if (summary.sink_discards <= 0) return null;
  return {
    stage: "sink_discard",
    node_id: null,
    count: summary.sink_discards,
  };
}

function primaryDiscardStage(summary: DiscardSummary): DiscardStageSummary | null {
  const stages = summary.stages ?? [];
  return (
    stages.find((stage) => stage.stage === "source_validation") ??
    stages.find((stage) => stage.stage === "transform_validation") ??
    stages.find((stage) => stage.stage === "gate_evaluation") ??
    stages.find((stage) => stage.stage === "sink_discard") ??
    sourceValidationFallbackStage(summary) ??
    transformValidationFallbackStage(summary) ??
    gateEvaluationFallbackStage(summary) ??
    sinkDiscardFallbackStage(summary)
  );
}

function discardStageLabel(stage: DiscardStageSummary): string {
  switch (stage.stage) {
    case "source_validation":
      return "source validation";
    case "transform_validation":
      return "transform validation";
    case "gate_evaluation":
      return "gate evaluation";
    case "sink_discard":
      return "sink discard handling";
  }
}

function discardCauseText(stage: DiscardStageSummary): string {
  switch (stage.stage) {
    case "source_validation":
      return "Common causes: source schema declares fields the input data does not contain; CSV header mismatch; on_validation_failure: \"discard\" is dropping rows.";
    case "transform_validation":
      return "Common causes: transform output did not match its declared schema; error routing sends invalid rows to discard; upstream fields changed shape.";
    case "gate_evaluation":
      return "Common causes: a gate expression compared incompatible runtime types or accessed a missing key; on_error: \"discard\" handled the failed row.";
    case "sink_discard":
      return "Common causes: sink write failure handling routed rows to discard before an output artifact was produced.";
  }
}

// A row discarded at source validation never becomes a token, so its reason
// lives ONLY in the diagnostics `discards` section (elspeth-43f52d69a4) —
// unlike every other stage, whose reason sits on a failed token node state.
const MAX_RECORDED_REASONS = 3;

function uniqueDiscardReasons(discards: RunDiagnosticDiscard[]): string[] {
  const seen = new Set<string>();
  const reasons: string[] = [];
  for (const entry of discards) {
    if (seen.has(entry.error)) continue;
    seen.add(entry.error);
    reasons.push(entry.error);
  }
  return reasons;
}

function DiscardSummaryWarning({ run }: { run: Run | null }): JSX.Element | null {
  const summary = run?.discard_summary ?? null;
  const stage = summary && summary.total > 0 ? primaryDiscardStage(summary) : null;
  const wantRecordedReasons = run !== null && stage?.stage === "source_validation";
  const diagnostics = useExecutionStore((s) =>
    run ? (s.diagnosticsByRunId[run.id] ?? null) : null,
  );
  const diagnosticsError = useExecutionStore((s) =>
    run ? (s.diagnosticsErrorByRunId[run.id] ?? null) : null,
  );
  const loadRunDiagnostics = useExecutionStore((s) => s.loadRunDiagnostics);

  useEffect(() => {
    if (!wantRecordedReasons || !run || diagnostics) return;
    void loadRunDiagnostics(run.id);
  }, [wantRecordedReasons, run, diagnostics, loadRunDiagnostics]);

  if (!run || !summary || summary.total <= 0 || !stage) return null;
  const nodeSuffix = stage.node_id ? ` (${stage.node_id})` : "";
  const terminalNote =
    run.status === "empty"
      ? "Run terminated empty."
      : "Run completed with discarded rows.";
  const allReasons = wantRecordedReasons ? uniqueDiscardReasons(diagnostics?.discards ?? []) : [];
  const shownReasons = allReasons.slice(0, MAX_RECORDED_REASONS);
  const hiddenReasonCount = allReasons.length - shownReasons.length;
  return (
    <div role="alert" className="discard-summary-warning">
      <strong>
        {plural(stage.count, "row")} discarded at {discardStageLabel(stage)}
        {nodeSuffix}. {terminalNote}
      </strong>
      {shownReasons.length > 0 ? (
        <span className="discard-summary-reasons">
          Recorded rejection {shownReasons.length === 1 ? "reason" : "reasons"}:{" "}
          {shownReasons.join(" · ")}
          {hiddenReasonCount > 0 ? ` (+${hiddenReasonCount} more in run diagnostics)` : ""}
        </span>
      ) : wantRecordedReasons ? (
        <span>
          {discardCauseText(stage)}{" "}
          {diagnosticsError !== null
            ? "See run diagnostics for the recorded rejection reasons."
            : "Loading recorded rejection reasons…"}
        </span>
      ) : (
        <span>{discardCauseText(stage)} View diagnostics for the first failed row's error message.</span>
      )}
    </div>
  );
}

export interface InlineRunResultsProps {
  showEmptyState?: boolean;
  /**
   * True only when the REQUEST_RUN_EVENT policy owner (ExecuteButton inside
   * CompletionBar) is mounted — App threads capabilities.completion through
   * ArtifactWorkspace; the tutorial shell (completion: false) leaves it
   * unset. Gates the empty-state Run affordance so it can never dispatch
   * into a zero-listener surface (App.tsx single-emitter doctrine,
   * elspeth-553a6fb81d).
   */
  runAvailable?: boolean;
}

interface InitialRunLoadState {
  sessionId: string | null;
  status: Exclude<RunHistoryLoadOutcome, "stale"> | "loading";
}

export function InlineRunResults({
  showEmptyState = false,
  runAvailable = false,
}: InlineRunResultsProps = {}): JSX.Element | null {
  const activeRunId = useExecutionStore((s) => s.activeRunId);
  const progress = useExecutionStore((s) => s.progress);
  const runs = useExecutionStore((s) => s.runs);
  const loadRuns = useExecutionStore((s) => s.loadRuns);
  // Run admission fact for the empty-state affordance: REQUEST_RUN_EVENT's
  // owner (ExecuteButton) drops the event when the run is inadmissible, so
  // an enabled button here would silently no-op. Mirror the same readiness
  // gate the owner enforces and fall back to directional copy instead.
  const executionReady = useExecutionStore(
    (s) => s.validationResult?.readiness?.execution_ready === true,
  );
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const [showHistory, setShowHistory] = useState(false);
  const [isCollapsed, setIsCollapsed] = useState(false);
  const [historyRetrySequence, setHistoryRetrySequence] = useState(0);
  const [initialRunLoad, setInitialRunLoad] = useState<InitialRunLoadState>(
    () => ({
      sessionId: activeSessionId,
      status: activeSessionId === null ? "loaded" : "loading",
    }),
  );

  useEffect(() => {
    if (!activeSessionId) {
      setInitialRunLoad({ sessionId: null, status: "loaded" });
      return;
    }
    const sessionId = activeSessionId;
    let cancelled = false;
    setInitialRunLoad({ sessionId, status: "loading" });
    const settle = (outcome: RunHistoryLoadOutcome): void => {
      if (cancelled || outcome === "stale") return;
      setInitialRunLoad((current) =>
        current.sessionId === sessionId
          ? { sessionId, status: outcome }
          : current,
      );
    };
    void loadRuns(sessionId).then(settle, () => settle("unavailable"));
    return () => {
      cancelled = true;
    };
  }, [activeSessionId, historyRetrySequence, loadRuns]);

  const visibleRuns = activeSessionId
    ? runs.filter((run) => !run.session_id || run.session_id === activeSessionId)
    : runs;
  const hasActiveOrPendingRun =
    visibleRuns.some((run) => run.status === "pending" || run.status === "running") ||
    (progress !== null && !isTerminalRunStatus(progress.status));

  useEffect(() => {
    if (!activeSessionId || !hasActiveOrPendingRun) return;
    const timer = window.setInterval(() => {
      void loadRuns(activeSessionId);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [activeSessionId, hasActiveOrPendingRun, loadRuns]);

  const activeRun = activeRunId
    ? (visibleRuns.find((run) => run.id === activeRunId) ?? null)
    : null;
  const mostRecentRun = !activeRunId ? (visibleRuns[0] ?? null) : null;
  const displayRun = activeRun ?? mostRecentRun;
  // Drawer contents: all terminal runs, including the one already displayed,
  // PLUS any live (pending/running) run this browser tab is NOT attached to.
  // An attached live run already exposes Cancel through ProgressView in the
  // persistent Run artifact; an unattached one (reload where rehydration
  // raced, run started from another browser tab) must reach history so its
  // REST-backed Cancel is the guaranteed fallback (elspeth-90db33baac).
  const drawerRuns = visibleRuns.filter((run) => {
    if (isTerminalRunStatus(run.status)) {
      return true;
    }
    const attachedToThisTab =
      activeRunId !== null && progress !== null && run.id === activeRunId;
    return !attachedToThisTab;
  });
  const progressBelongsToActiveRun = activeRunId !== null && progress !== null;
  const displayStatus = progressBelongsToActiveRun ? progress.status : displayRun?.status ?? null;
  const showProgress = progressBelongsToActiveRun;
  const outputRunId =
    activeRunId && progress && isTerminalRunStatus(progress.status)
      ? activeRunId
      : displayRun && displayStatus && isTerminalRunStatus(displayStatus)
        ? displayRun.id
        : null;
  const hasDrawerRuns = drawerRuns.length > 0;
  const summaryParts = runSummaryParts(
    displayStatus,
    progressBelongsToActiveRun ? progress : null,
    displayRun,
  );
  const currentSessionHistoryStatus =
    activeSessionId === null
      ? "loaded"
      : initialRunLoad.sessionId === activeSessionId
        ? initialRunLoad.status
        : "loading";

  // Empty and loading states render the designed `.empty-state` primitive
  // (styles/shared.css), the same one YamlView and GraphView use. They
  // previously carried `.artifact-empty`, which NO stylesheet defined, so the
  // first states a new user meets shipped as bare UA paragraphs one tab away
  // from the correct treatment (elspeth-0ee1973592). The primitive centres a
  // SINGLE flex child, so the two message-plus-action states below wrap their
  // children in one unclassed block — that keeps the action under the message
  // (inheriting the primitive's text-align) rather than beside it, without
  // inventing a class or freelancing an inline layout.
  if (!showProgress && !outputRunId && !hasDrawerRuns) {
    if (showEmptyState && currentSessionHistoryStatus === "loading") {
      return (
        <p className="empty-state" role="status" aria-live="polite">
          Loading runs…
        </p>
      );
    }
    if (showEmptyState && currentSessionHistoryStatus === "unavailable") {
      return (
        <div className="empty-state">
          <div>
            <p role="status" aria-live="polite">
              Run history unavailable.
            </p>
            <Button
              compact
              onClick={() => setHistoryRetrySequence((current) => current + 1)}
            >
              Retry
            </Button>
          </div>
        </div>
      );
    }
    if (!showEmptyState) return null;
    if (!runAvailable) {
      return <p className="empty-state">No runs yet.</p>;
    }
    // Inline run affordance (elspeth-553a6fb81d): route through the
    // REQUEST_RUN_EVENT single policy owner (ExecuteButton's gating
    // predicate + egress disclosure) — never a second ExecuteButton, never
    // a direct execute() call. Rendered only when that owner is mounted AND
    // the run is admissible (execution_ready): the owner's handler drops
    // inadmissible events, so the button would otherwise be a silent
    // dead control.
    return executionReady ? (
      <div className="empty-state">
        <div>
          <p>No runs yet. Run the pipeline to see its results here.</p>
          <Button
            compact
            onClick={() => window.dispatchEvent(new CustomEvent(REQUEST_RUN_EVENT))}
          >
            Run pipeline
          </Button>
        </div>
      </div>
    ) : (
      // Names the CONTROL, not a place. The run control lives in the sticky
      // action bar at the bottom of this same pane at every width, so the
      // earlier "run it from the sidebar" pointed at a surface that does not
      // exist — the same defect class ("prose naming a control the user
      // cannot find") the verb-vocabulary work exists to excise. Location
      // words rot when the layout moves; "Run pipeline" is the one name that
      // control carries on every surface.
      <p className="empty-state">
        No runs yet. Once validation passes, use Run pipeline to start one.
      </p>
    );
  }

  return (
    <section
      className={`inline-run-results${isCollapsed ? " inline-run-results--collapsed" : ""}`}
      aria-label="Pipeline run results"
    >
      <div className="inline-run-results-toolbar">
        <div className="inline-run-results-heading">
          <span className="inline-run-results-title">Run results</span>
          {isCollapsed && (
            <span className="inline-run-results-summary">
              {summaryParts.join(" · ")}
            </span>
          )}
        </div>
        <div className="inline-run-results-actions">
          <Button
            compact
            onClick={() => setIsCollapsed((value) => !value)}
            className="inline-run-results-collapse-btn"
            aria-label={isCollapsed ? "Show run results" : "Hide run results"}
            title={isCollapsed ? "Show run results" : "Hide run results"}
          >
            {/* Direction semantics preserved from the text-glyph era: the
                collapsed toolbar points up (expand reveals above-the-toolbar
                content), expanded points down. */}
            <Icon name={isCollapsed ? "chevron-up" : "chevron-down"} />
          </Button>
          {hasDrawerRuns && (
            <Button
              compact
              onClick={() => setShowHistory(true)}
              className="inline-run-results-history-btn"
            >
              Runs ({drawerRuns.length})
            </Button>
          )}
        </div>
      </div>

      {!isCollapsed && <DiscardSummaryWarning run={displayRun} />}
      {!isCollapsed && showProgress && <ProgressView />}
      {!isCollapsed && outputRunId && <NarrativeResultsBranch runId={outputRunId} />}

      {showHistory && (
        <RunsHistoryDrawer
          onClose={() => setShowHistory(false)}
          runsOverride={drawerRuns}
        />
      )}
    </section>
  );
}

/**
 * NarrativeResultsBranch — Phase 6B Task 7 dispatcher.
 *
 * XOR switch between the narrative-mode `NarrativeResults` panel and the
 * default tabular `RunOutputsPanel`, based on `useNarrativeMode`. Per
 * plan 19b:365 the literal pattern is
 * `return narrativeMode ? <NarrativeResults /> : <ExistingTablePreview />`
 * and per plan 19b:359 the rendered output is `<NarrativeResults />`
 * **rather than** the existing table preview — the two views are
 * mutually exclusive at the composition level, not stacked.
 *
 * Per plan §"Scope boundaries": narrative mode is binary at the
 * composition level. If any pipeline plugin carries the
 * `"narrative-summary"` capability tag (per Phase 6A Task 8), the
 * narrative panel renders; otherwise the existing tabular view renders.
 */
function NarrativeResultsBranch({ runId }: { runId: string }): JSX.Element {
  const { narrativeMode } = useNarrativeMode();
  return narrativeMode ? <NarrativeResults /> : <RunOutputsPanel runId={runId} />;
}
