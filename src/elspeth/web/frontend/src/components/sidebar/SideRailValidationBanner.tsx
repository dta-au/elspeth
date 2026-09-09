import { useMemo, useState } from "react";
import { ValidationResultBanner } from "@/components/execution/ValidationResult";
import { Button } from "@/components/ui";
import { stepLabelForNodeId } from "@/components/chat/interpretationStepLabel";
import { useComposer } from "@/hooks/useComposer";
import { OPEN_GRAPH_MODAL_EVENT } from "@/lib/composer-events";
import { humaniseValidationMessage, makePhraseFor } from "@/lib/validationHumaniser";
import { useExecutionStore } from "@/stores/executionStore";
import { useSessionStore } from "@/stores/sessionStore";
import {
  COMPOSE_CONNECTING_MESSAGE,
  COMPOSE_UNAVAILABLE_MESSAGE,
} from "@/config/composer";
import type { CompositionState, ValidationEntryDTO } from "@/types/index";
import {
  sortedSourceEntries,
  sourceComponentId,
} from "@/utils/compositionState";

interface SuggestionListProps {
  suggestions: ValidationEntryDTO[];
  phraseFor: (componentId: string | null) => string;
  stepLabelFor: (componentId: string) => string | null;
}

function SuggestionList({
  suggestions,
  phraseFor,
  stepLabelFor,
}: SuggestionListProps): JSX.Element {
  const { sendMessage, isComposing } = useComposer();
  // Apply is a programmatic freeform sender (routes through useComposer →
  // runComposeWithTimeout). Hold it closed until the backend compose wall
  // clock has landed at boot, same as the main Send — a send started against
  // the stale default ceiling could be aborted before the backend's 422
  // (bootstrap race). The underlying runComposeWithTimeout also no-ops when
  // not ready; disabling the button keeps it from reading as a dead click.
  const composeTimeoutReady = useSessionStore((s) => s.composeTimeoutReady);
  const composerTimeoutUnavailable = useSessionStore(
    (s) => s.composerTimeoutUnavailable,
  );
  const applyDisabled = isComposing || !composeTimeoutReady;
  // Reason shown while Apply is held closed by the compose gate (not while
  // composing): the stuck "unavailable" state, else the transient boot window.
  const composeGateReason = composerTimeoutUnavailable
    ? COMPOSE_UNAVAILABLE_MESSAGE
    : COMPOSE_CONNECTING_MESSAGE;
  const [expanded, setExpanded] = useState(suggestions.length <= 2);

  function handleApply(suggestion: ValidationEntryDTO): void {
    if (applyDisabled) return;
    const prompt = `Please apply this suggestion to the pipeline:\n\n**${suggestion.component}:** ${suggestion.message}`;
    void sendMessage(prompt);
  }

  function handleKeyDown(event: React.KeyboardEvent): void {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      setExpanded((prev) => !prev);
    }
  }

  // Humanise once per suggestion (elspeth-27efd1e801): the raw dump moves
  // behind a Technical-details disclosure rendered OUTSIDE the <ul> — a
  // <details> nested inside a list item still contributes its (closed)
  // content to the list's textContent, so a reader that only wants the
  // list's own words would still see the raw dump. Keeping the disclosure a
  // sibling of the list, not a descendant, is what keeps it out.
  //
  // Splitting the row from its disclosure loses the DOM containment that
  // would otherwise tie them together, so each pair gets an explicit
  // association (review round 1): a stable per-row id feeds the Apply
  // button's aria-describedby, and the <summary> carries a per-row
  // humanised label (never the raw component id) — with 2+ suggestions
  // carrying raw text, an assistive-tech user needs both the announced
  // link AND a visually distinguishing label to tell whose dump is whose.
  const humanisedSuggestions = suggestions.map((s, i) => {
    const finding = humaniseValidationMessage(s.message, phraseFor, stepLabelFor);
    return {
      suggestion: s,
      finding,
      technicalId: finding.raw !== null ? `side-rail-suggestion-technical-${i}` : undefined,
    };
  });
  const hasTechnicalDetails = humanisedSuggestions.some(
    ({ finding }) => finding.raw !== null,
  );

  return (
    <div className="side-rail-suggestion-banner">
      <div
        className="side-rail-suggestion-header"
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        onClick={() => setExpanded((prev) => !prev)}
        onKeyDown={handleKeyDown}
      >
        <span>Suggestions ({suggestions.length})</span>
        <span className="side-rail-suggestion-chevron">
          {expanded ? "▴" : "▾"}
        </span>
      </div>
      {expanded && !isComposing && !composeTimeoutReady && (
        // Visible + announced reason the Apply buttons below are disabled. The
        // per-button title alone is not reliably read by screen readers. Matches
        // the main ChatInput affordance: polite role="status" for the transient
        // boot window, assertive role="alert" for the stuck "unavailable" state.
        <div
          role={composerTimeoutUnavailable ? "alert" : "status"}
          className={
            composerTimeoutUnavailable
              ? "side-rail-suggestion-unavailable"
              : "side-rail-suggestion-connecting"
          }
        >
          {composeGateReason}
        </div>
      )}
      {expanded && (
        <ul className="side-rail-suggestion-list" aria-label="Suggestions">
          {humanisedSuggestions.map(({ suggestion: s, finding, technicalId }, i) => (
            <li key={i} className="side-rail-suggestion-item">
              <span className="side-rail-suggestion-item-text">
                <strong>{phraseFor(s.component)}:</strong> {finding.headline}
              </span>
              <Button
                variant="bare"
                className="side-rail-suggestion-apply-btn"
                disabled={applyDisabled}
                title={
                  !isComposing && !composeTimeoutReady
                    ? composeGateReason
                    : undefined
                }
                aria-describedby={technicalId}
                onClick={() => handleApply(s)}
              >
                {isComposing ? "Applying..." : "Apply"}
              </Button>
            </li>
          ))}
        </ul>
      )}
      {expanded && hasTechnicalDetails && (
        <div className="side-rail-suggestion-technical">
          {humanisedSuggestions.map(({ suggestion: s, finding, technicalId }, i) =>
            finding.raw !== null ? (
              <details key={i} id={technicalId} className="validation-banner-technical">
                <summary>Technical details — {phraseFor(s.component)}</summary>
                <pre>{finding.raw}</pre>
              </details>
            ) : null,
          )}
        </div>
      )}
    </div>
  );
}

function buildValidationComponentNames(
  compositionState: CompositionState | null,
): Record<string, string> {
  if (!compositionState) {
    return {};
  }

  const componentNames: Record<string, string> = {};
  for (const [sourceName] of sortedSourceEntries(compositionState)) {
    componentNames[sourceComponentId(sourceName)] = `source:${sourceName}`;
  }
  for (const node of compositionState.nodes) {
    componentNames[node.id] = `${node.node_type}:${node.id}`;
  }
  for (const output of compositionState.outputs) {
    componentNames[output.name] = `sink:${output.name}`;
  }
  return componentNames;
}

interface ComponentNavigationProps {
  onSelectComponent?: (componentId: string) => void;
}

export function SideRailValidationBanner({
  onSelectComponent,
}: ComponentNavigationProps = {}): JSX.Element | null {
  const compositionState = useSessionStore((s) => s.compositionState);
  const validationResult = useExecutionStore((s) => s.validationResult);
  const error = useExecutionStore((s) => s.error);
  const suggestions = compositionState?.validation_suggestions ?? [];
  const validationComponentNames =
    buildValidationComponentNames(compositionState);
  // Shared with SuggestionList (elspeth-27efd1e801) so its suggestion rows
  // name components and phrase messages identically to the ValidationResult
  // banner above it — hooks must run before the early-return below.
  const phraseFor = useMemo(() => makePhraseFor(compositionState), [compositionState]);
  const stepLabelFor = (componentId: string): string | null =>
    stepLabelForNodeId(compositionState, componentId);

  if (!error && !validationResult && suggestions.length === 0) {
    return null;
  }

  function handleValidationComponentClick(componentId: string): void {
    if (
      Object.prototype.hasOwnProperty.call(
        validationComponentNames,
        componentId,
      )
    ) {
      if (onSelectComponent !== undefined) {
        onSelectComponent(componentId);
        return;
      }
      useSessionStore.getState().selectNode(componentId);
      window.dispatchEvent(new CustomEvent(OPEN_GRAPH_MODAL_EVENT));
    }
  }

  return (
    <div className="side-rail-validation-banner">
      {error && (
        <div
          role="alert"
          className="validation-banner validation-banner-fail side-rail-error-banner"
        >
          {error}
        </div>
      )}
      {validationResult && (
        <ValidationResultBanner
          result={validationResult}
          nodes={compositionState?.nodes}
          componentNames={validationComponentNames}
          onComponentClick={handleValidationComponentClick}
        />
      )}
      {suggestions.length > 0 && (
        <SuggestionList
          suggestions={suggestions}
          phraseFor={phraseFor}
          stepLabelFor={stepLabelFor}
        />
      )}
    </div>
  );
}
