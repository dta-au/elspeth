// src/components/chat/ComposingIndicator.tsx

import { useEffect, useId, useRef, useState } from "react";

import { Button } from "@/components/ui";
import type {
  ComposerProgressSnapshot,
  CompositionState,
  ToolCall,
} from "@/types/api";
import {
  COMPLETION_OUTCOME_LABELS,
  type CompletionOutcome,
} from "./completionOutcome";
import {
  TOOL_CALL_DESCRIPTIONS,
  liveToolCallLabel,
} from "./toolCallDescriptions";
import { hasSources } from "@/utils/compositionState";
import { plural } from "@/utils/plural";

interface ComposingIndicatorProps {
  latestRequest?: string | null;
  compositionState?: CompositionState | null;
  composerProgress?: ComposerProgressSnapshot | null;
  /**
   * Honest terminal-badge state for a successful completion
   * (elspeth-bf9c296ee5), derived by the parent from the run gate's own
   * signals (useCompletionOutcome). Only consulted when phase is
   * "complete" — failed/cancelled keep their own labels. Omitted/null
   * falls back to the legacy "Updated" badge (snapshot-only reloads where
   * the per-turn mutation verdict is unknowable).
   */
  completionOutcome?: CompletionOutcome | null;
  /**
   * Tool calls of the mid-flight tail turn (elspeth-3c2caf56a7), derived by
   * ChatPanel from the same inflight-messages poll that feeds the bubbles.
   * The atomic-reveal gate hides the incomplete turn's bubble, so this list
   * is the only mid-turn surface naming what the composer is doing. Empty
   * once the genuine reply lands (the entries roll into the bubble's
   * "Tool calls (N)" disclosure).
   */
  liveToolCalls?: ToolCall[];
}

interface RequestFocus {
  headline: string;
  focus: string;
  nextMove: string;
}

interface WorkingView {
  headline: string;
  evidence: string[];
  likelyNext: string;
  /**
   * Provenance of the view (elspeth-b189b5b3b8 part c): "backend" views carry
   * evidence the server actually reported for this compose request;
   * "estimated" views are keyword-guessed from the user's message text and
   * must not read as if ELSPETH reported them. Rendering italicises estimated
   * views and appends a visible "(estimated)" marker to the headline.
   */
  source: "backend" | "estimated";
}

function isTerminalPhase(
  phase: ComposerProgressSnapshot["phase"] | undefined,
): boolean {
  return phase === "complete" || phase === "failed" || phase === "cancelled";
}

function terminalStatusLabel(
  phase: ComposerProgressSnapshot["phase"] | undefined,
  completionOutcome: CompletionOutcome | null | undefined,
): string {
  if (phase === "failed") return "Failed";
  if (phase === "cancelled") return "Stopped";
  // A successful completion must not claim more than the run gate would
  // honour: the outcome distinguishes Response ready / Pipeline updated /
  // Review required / Pipeline ready (elspeth-bf9c296ee5). "Updated" remains
  // only as the no-signal fallback.
  if (completionOutcome != null) {
    return COMPLETION_OUTCOME_LABELS[completionOutcome];
  }
  return "Updated";
}

function setupCount(count: number, singular: string, pluralLabel = `${singular}s`): string {
  if (count === 0) {
    return `no ${pluralLabel}`;
  }
  return plural(count, singular, pluralLabel);
}

function describeCurrentSetup(compositionState: CompositionState | null | undefined): string {
  const input = hasSources(compositionState) ? "input configured" : "no input yet";
  const steps = setupCount(compositionState?.nodes.length ?? 0, "processing step");
  const outputs = setupCount(compositionState?.outputs.length ?? 0, "output");
  return `Current setup: ${input}, ${steps}, ${outputs}.`;
}

function describeRequestFocus(latestRequest: string | null | undefined): RequestFocus {
  const normalized = latestRequest?.toLocaleLowerCase() ?? "";

  if (normalized.includes("html") && normalized.includes("json")) {
    return {
      headline: "Working on: convert HTML into JSON",
      focus: "Request focus: turn HTML content into structured JSON.",
      nextMove: "Likely next move: choose an input, extract the useful fields, then save structured JSON.",
    };
  }

  if (/\b(database|sql|table|query)\b/.test(normalized)) {
    return {
      headline: "Working on: database-backed data flow",
      focus: "Request focus: read data from a database source.",
      nextMove: "Likely next move: identify the input query, shape the records, then send them to an output.",
    };
  }

  if (/\b(scrape|website|web page|url|fetch)\b/.test(normalized)) {
    return {
      headline: "Working on: web content pipeline",
      focus: "Request focus: fetch or parse web content.",
      nextMove: "Likely next move: choose a web input, extract the useful content, then structure the result.",
    };
  }

  if (/\b(output|save|export|write|artifact)\b/.test(normalized)) {
    return {
      headline: "Working on: saved output",
      focus: "Request focus: produce or update saved output.",
      nextMove: "Likely next move: check the current pipeline shape and wire the final output.",
    };
  }

  if (/\b(file|csv|excel|upload|input)\b/.test(normalized)) {
    return {
      headline: "Working on: file input pipeline",
      focus: "Request focus: use a supplied file as input.",
      nextMove: "Likely next move: connect the file, inspect its fields, then add the needed processing steps.",
    };
  }

  return {
    headline: "Working through your request",
    focus: "Request focus: update the pipeline from your latest message.",
    nextMove: "Likely next move: compare your request with the current setup, then update the graph or explain what is missing.",
  };
}

function backendWorkingView(
  composerProgress: ComposerProgressSnapshot | null | undefined,
): WorkingView | null {
  if (!composerProgress || composerProgress.phase === "idle") {
    return null;
  }

  return {
    headline: composerProgress.headline,
    evidence:
      composerProgress.evidence.length > 0
        ? composerProgress.evidence
        : ["ELSPETH has accepted the compose request for this session."],
    likelyNext:
      composerProgress.likely_next ??
      "ELSPETH will continue through the visible composer workflow.",
    source: "backend",
  };
}

function heuristicWorkingView(
  latestRequest: string | null | undefined,
  compositionState: CompositionState | null | undefined,
): WorkingView {
  const requestFocus = describeRequestFocus(latestRequest);
  return {
    headline: requestFocus.headline,
    evidence: [
      requestFocus.focus,
      describeCurrentSetup(compositionState),
    ],
    likelyNext: requestFocus.nextMove,
    source: "estimated",
  };
}

/** Format elapsed whole seconds as a mm:ss readout (65 → "01:05"). */
export function formatElapsed(totalSeconds: number): string {
  const clamped = Math.max(0, Math.floor(totalSeconds));
  const minutes = Math.floor(clamped / 60);
  const seconds = clamped % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

/**
 * Elapsed-time readout for the in-flight compose card (elspeth-b189b5b3b8
 * part a): a slow turn must not read identically to a stalled request.
 * Counts from the moment the indicator becomes active (non-terminal) and
 * stops when a terminal phase lands.
 *
 * The ticking readout is aria-hidden: the indicator sits in a role="status"
 * live region and a once-per-second text mutation would spam screen readers
 * with announcements. Sighted users get the timer; AT users get the phase
 * headline changes, which already convey progress.
 *
 * Exported for the guided pending strip (GuidedPendingStrip.tsx), which
 * shares the same aria-hidden/mount-reset semantics.
 */
export function ElapsedReadout() {
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const startRef = useRef<number>(Date.now());

  useEffect(() => {
    const timer = setInterval(() => {
      setElapsedSeconds(Math.floor((Date.now() - startRef.current) / 1000));
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  return (
    <span className="composing-elapsed" aria-hidden="true">
      {formatElapsed(elapsedSeconds)}
    </span>
  );
}

/**
 * Animated three-dot composing indicator shown while the backend
 * is processing the LLM tool-use loop. Uses the .composing-dot CSS
 * class from styles/animations.css for staggered bounce animation.
 *
 * This component carries its own non-interactive role="status" summary and is
 * mounted OUTSIDE ChatPanel's role="log" messages container
 * (elspeth-76a0cc485e): nesting a status region inside an aria-live log risks
 * double announcements on AT that honours both regions.
 */
export function ComposingIndicator({
  latestRequest = null,
  compositionState = null,
  composerProgress = null,
  completionOutcome = null,
  liveToolCalls = [],
}: ComposingIndicatorProps) {
  const workingView =
    backendWorkingView(composerProgress) ??
    heuristicWorkingView(latestRequest, compositionState);
  const isTerminal = isTerminalPhase(composerProgress?.phase);
  const isEstimated = workingView.source === "estimated";
  const progressKey = latestRequest ?? composerProgress?.request_id ?? "idle";
  const toolLogCaptionId = `${useId()}-tool-log`;
  const [detailsOpen, setDetailsOpen] = useState(isTerminal);

  useEffect(() => {
    setDetailsOpen(isTerminal);
  }, [isTerminal, progressKey]);

  return (
    <div
      className={`composing-indicator composing-row${isTerminal ? " composing-indicator--terminal" : ""}`}
    >
      <div className="composing-bubble">
        {isTerminal ? (
          <div className="composing-terminal-mark" aria-hidden="true">
            {terminalStatusLabel(composerProgress?.phase, completionOutcome)}
          </div>
        ) : (
          <div className="composing-pulse" aria-hidden="true">
            <span className="composing-dot" />
            <span className="composing-dot" />
            <span className="composing-dot" />
          </div>
        )}
        <div
          className={`composing-working-view${isEstimated ? " composing-working-view--estimated" : ""}`}
        >
          <div className="composing-status-summary" role="status">
            <div className="composing-label">
              {isTerminal ? (
                "Last composer update"
              ) : (
                <>
                  Working on...
                  {/* Mount lifecycle doubles as the reset: the readout only
                      renders while non-terminal, so a terminal phase unmounts
                      it and the next compose remounts it from 00:00. */}
                  <ElapsedReadout />
                </>
              )}
            </div>
            <div className="composing-title">
              {workingView.headline}
              {isEstimated && (
                <span className="composing-estimated-tag"> (estimated)</span>
              )}
            </div>
          </div>

          {/* Live tool-call log: always visible (not behind Show details) and
              deliberately a SIBLING of the role="status" summary above — an
              append-per-poll list inside a polite live region would announce
              every 1.5s poll tick to AT (the aria-hidden ElapsedReadout
              precedent, WCAG 4.1.3). */}
          {liveToolCalls.length > 0 && (
            <>
              {/* The list had no name of any kind: nothing told the operator
                  that "Running: set_source" was a log of composer actions
                  rather than status noise. Visible caption (not just an
                  aria-label) because it doubles as the plain-language anchor
                  for the Ran/Running/Looked up vocabulary, and it labels the
                  list for AT via aria-labelledby. */}
              <div className="composing-tool-log-caption" id={toolLogCaptionId}>
                Composer actions in this turn
              </div>
              <ul
                className="composing-tool-log"
                aria-labelledby={toolLogCaptionId}
              >
                {liveToolCalls.map((call) => {
                  const name = call.function.name;
                  // The audience-facing sentence was bound to `title` —
                  // mouse-only, so during the very wait this surface exists to
                  // fill, a non-engineer got motion without meaning. It is now
                  // the visually primary line and the machine identifier is
                  // demoted to the secondary one.
                  //
                  // The evidential prefix STAYS attached to the tool name and
                  // is never dropped: it is the honesty contract of this log
                  // (Failed / Attempted (not applied) / Cancelled), and a
                  // failed mutation rendered as a bare description would claim
                  // the mutation happened. Unmapped names have no honest
                  // description — describeToolCall's fallback is the generic
                  // "Composer tool call." — so they keep the bare label rather
                  // than gaining a line that says nothing.
                  const description: string | undefined =
                    TOOL_CALL_DESCRIPTIONS[name];
                  return (
                    <li key={call.id}>
                      {description !== undefined && (
                        <span className="composing-tool-log-what">
                          {description}
                        </span>
                      )}
                      <span className="composing-tool-log-call">
                        {liveToolCallLabel(name, call.outcome)}
                      </span>
                    </li>
                  );
                })}
              </ul>
            </>
          )}

          <Button
            variant="bare"
            className="composing-details-toggle"
            aria-expanded={detailsOpen}
            onClick={() => setDetailsOpen((open) => !open)}
          >
            {detailsOpen ? "Hide details" : "Show details"}
          </Button>

          {detailsOpen && (
            <div className="composing-details">
              <div className="composing-section">
                <div className="composing-label">
                  {isEstimated ? "Best guess from your request" : "What ELSPETH can see"}
                </div>
                <ul className="composing-evidence">
                  {workingView.evidence.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
              <div className="composing-section">
                <div className="composing-label">Likely next</div>
                <div className="composing-text">{workingView.likelyNext}</div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
