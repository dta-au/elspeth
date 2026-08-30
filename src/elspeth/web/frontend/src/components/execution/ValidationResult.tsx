// ============================================================================
// ValidationResult Banner
//
// Inline banner displayed between the inspector header and tab content.
// Renders Stage 2 validation results with per-component attribution.
//
// Pass: green banner collapsed to a one-line "Validation passed" summary,
// mirroring AuditReadinessPanel's collapse rule — expanded only when
// something is actionable (warnings, or a passed-but-advisory check like
// identity_node_advisory) or the user clicked to expand. The expanded view
// shows check details and warnings.
// Fail: red banner with per-component error list, component_id mapped to
// display name from CompositionState, and suggested fixes from backend.
//
// The Execute button enables/disables based on this result.
// ============================================================================

import { useMemo, useState } from "react";

import { Button, Icon } from "@/components/ui";
import { titleCaseLabel } from "@/components/catalog/pluginDisplayName";
import { UNKNOWN_COMPONENT_PHRASE } from "@/components/chat/guided/pipelineGloss";
import { stepLabelForNodeId } from "@/components/chat/interpretationStepLabel";
import { humaniseValidationMessage, makePhraseFor } from "@/lib/validationHumaniser";
import { useShowAdvanced } from "@/stores/preferencesStore";
import { useSessionStore } from "@/stores/sessionStore";
import {
  VALIDATION_ADVISORY_CHECK_NAMES,
  type ValidationResult as ValidationResultType,
  type ValidationWarning,
  type NodeSpec,
} from "@/types/index";

const ADVISORY_CHECK_NAME_SET: ReadonlySet<string> = new Set(
  VALIDATION_ADVISORY_CHECK_NAMES,
);

function validationCheckDisplayName(name: string): string {
  // ``advisor_signoff`` is the stable persistence/wire key. Its UI label must
  // describe the evidence-scoped completion advisory without implying a
  // whole-pipeline approval certificate.
  return name === "advisor_signoff" ? "Completion advisory review" : name;
}

interface ValidationResultProps {
  result: ValidationResultType;
  /** Nodes from CompositionState for mapping component_id to display name */
  nodes?: NodeSpec[];
  /** All graph components from CompositionState keyed by navigable component_id */
  componentNames?: Record<string, string>;
  /** Callback when user clicks an error/warning to navigate to that component */
  onComponentClick?: (componentId: string) => void;
}

/** The two registers of one errored component's name. */
interface ComponentLabel {
  /** Reader register — what the banner shows. Never a raw id, never a
   *  `type:id` pair. */
  name: string;
  /** Identifier register — the exact wire form, for `title`/`data-*` only.
   *  The structural `type:id` pair for a component still wired into the
   *  composition, else the bare component id. Null when there is no id at
   *  all. */
  raw: string | null;
}

/**
 * Resolve a component_id to the two registers above.
 *
 * Ladder for the NAME: an explicit componentNames map (built from the live
 * composition, SideRailValidationBanner's buildValidationComponentNames) wins
 * outright. Otherwise the shared plain-language resolver `phraseFor`
 * (elspeth-27efd1e801) — never a bare raw id.
 *
 * Two corrections this wave's review found:
 *
 *  * A component that IS still wired used to render its structural
 *    `type:id` pair verbatim ("transform:extract_invoice") — the engineer
 *    register leaking straight into the banner's bold prefix. The pair moves
 *    to `raw`; the visible name is the resolved phrase with the humanised
 *    node kind beside it, the `Name (Kind)` shape the run-consent dialog and
 *    the Spec card already use.
 *
 *  * `phraseFor`'s no-context answer is UNKNOWN_COMPONENT_PHRASE ("this
 *    step"), which is the SAME for every component — two errors on two
 *    different components read identically, where pre-wave the raw ids were
 *    ugly but distinct (ux M-1). Discriminated on the sentinel exactly as
 *    RunsHistoryDrawer's RunStateFailureDetail does, falling back to
 *    `titleCaseLabel` — `humaniseStepLabel`'s own unloaded-composition rung,
 *    so an unresolvable id still names the author's own word for the step
 *    rather than collapsing into a generic phrase.
 */
function resolveComponentLabel(
  componentId: string | null,
  nodes: NodeSpec[] | undefined,
  componentNames: Record<string, string> | undefined,
  phraseFor: (componentId: string | null) => string,
): ComponentLabel {
  if (!componentId) return { name: "unknown", raw: null };
  if (
    componentNames &&
    Object.prototype.hasOwnProperty.call(componentNames, componentId)
  ) {
    return { name: componentNames[componentId], raw: componentId };
  }
  const phrase = phraseFor(componentId);
  const named =
    phrase === UNKNOWN_COMPONENT_PHRASE ? titleCaseLabel(componentId) : phrase;
  const node = nodes?.find((n) => n.id === componentId);
  if (node === undefined) return { name: named, raw: componentId };
  return {
    name: `${named} (${titleCaseLabel(node.node_type)})`,
    raw: `${node.node_type}:${node.id}`,
  };
}

function isNavigableComponent(
  componentId: string | null,
  nodes: NodeSpec[] | undefined,
  componentNames: Record<string, string> | undefined,
): componentId is string {
  if (!componentId) return false;
  if (
    componentNames &&
    Object.prototype.hasOwnProperty.call(componentNames, componentId)
  ) {
    return true;
  }
  return nodes?.some((n) => n.id === componentId) ?? false;
}

export function ValidationResultBanner({
  result,
  nodes,
  componentNames,
  onComponentClick,
}: ValidationResultProps) {
  // Mirrors AuditReadinessPanel: expansion is the user's explicit intent;
  // warnings (actionable) force the expanded view regardless. Passed checks
  // that are advisory (e.g. identity_node_advisory) carry the same kind of
  // actionable guidance as a warning despite passed=true, so they force
  // expansion too — otherwise the guidance sits inert behind a collapsed
  // "Validation passed" banner. The banner unmounts when the validation
  // result is cleared (session switch, new composition version), so a fresh
  // result starts collapsed again.
  const [userExpanded, setUserExpanded] = useState(false);
  const showAdvanced = useShowAdvanced();
  const compositionState = useSessionStore((s) => s.compositionState);
  const phraseFor = useMemo(() => makePhraseFor(compositionState), [compositionState]);
  const stepLabelFor = (componentId: string): string | null =>
    stepLabelForNodeId(compositionState, componentId);

  if (result.is_valid) {
    const warnings = result.warnings ?? [];
    const advisoryChecks = result.checks.filter(
      (check) => check.passed && ADVISORY_CHECK_NAME_SET.has(check.name),
    );
    const failedAdvisorChecks = result.checks.filter(
      (check) => !check.passed && check.name === "advisor_signoff",
    );
    const hasForcedGuidance =
      warnings.length > 0 ||
      advisoryChecks.length > 0 ||
      failedAdvisorChecks.length > 0;
    const showExpanded = hasForcedGuidance || userExpanded;
    // "checks" here can span more than authoring/execution validity — e.g. a
    // failed advisor_signoff (completion-readiness) check can sit alongside
    // is_valid=true — so the standard-view summary must count, not assume
    // (review round 1: "All N checks passed" was unconditional and read as
    // fabricated on a result carrying a genuinely failed check). The slot
    // stays present either way (global constraint: every gated item needs a
    // plain summary in its place), just truthful about the count.
    const passedChecksCount = result.checks.filter((check) => check.passed).length;

    if (!showExpanded) {
      return (
        <div
          role="status"
          className="validation-banner validation-banner-pass validation-banner--collapsed"
        >
          <Button
            variant="bare"
            className="validation-banner-summary-btn"
            onClick={() => setUserExpanded(true)}
            aria-expanded={false}
            aria-label="Validation passed. Show details."
          >
            <Icon name="check" />
            <span className="validation-banner-summary">
              {result.summary ?? "Validation passed"}
            </span>
            {result.checks.length > 0 && (
              <span className="validation-banner-summary-meta">
                {result.checks.length} checks
              </span>
            )}
          </Button>
        </div>
      );
    }

    return (
      <div
        role="status"
        className="validation-banner validation-banner-pass validation-banner-content"
      >
        <div className="validation-banner-header">
          <Icon name="check" />
          <span className="validation-banner-summary">
            {result.summary ?? "Validation passed"}
          </span>
          {!hasForcedGuidance && (
            <Button
              variant="bare"
              className="validation-banner-collapse-btn"
              onClick={() => setUserExpanded(false)}
              aria-expanded={true}
              aria-label="Collapse validation details"
            >
              Collapse
            </Button>
          )}
        </div>
        {result.checks.length > 0 && !showAdvanced && (
          <p className="validation-banner-checks-summary">
            {passedChecksCount === result.checks.length
              ? `All ${result.checks.length} checks passed.`
              : `${passedChecksCount} of ${result.checks.length} checks passed.`}
          </p>
        )}
        {result.checks.length > 0 && showAdvanced && (
          <ul className="validation-banner-checks">
            {result.checks.map((check, i) => (
              <li key={i} className="validation-banner-check-item" title={check.name}>
                <Icon name={check.passed ? "check" : "cross"} />{" "}
                {check.name === "advisor_signoff"
                  ? `${validationCheckDisplayName(check.name)}: ${check.detail}`
                  : check.detail}
              </li>
            ))}
          </ul>
        )}
        {!showAdvanced && (advisoryChecks.length > 0 || failedAdvisorChecks.length > 0) && (
          <ul className="validation-banner-checks">
            {[...advisoryChecks, ...failedAdvisorChecks].map((check, i) => (
              <li key={i} className="validation-banner-check-item" title={check.name}>
                <Icon name={check.passed ? "check" : "cross"} />{" "}
                {validationCheckDisplayName(check.name)}: {check.detail}
              </li>
            ))}
          </ul>
        )}
        {result.warnings && result.warnings.length > 0 && (
          <div className="validation-banner-warnings-section">
            <div className="validation-banner-warnings-title">
              Warnings ({result.warnings.length}):
            </div>
            <ul className="validation-banner-warnings-list">
              {result.warnings.map((warn: ValidationWarning, i: number) => {
                const isClickable =
                  Boolean(onComponentClick) &&
                  isNavigableComponent(warn.component_id, nodes, componentNames);
                const finding = humaniseValidationMessage(
                  warn.message,
                  phraseFor,
                  stepLabelFor,
                );
                // The warning TEXT is what the button navigates from; the
                // suggestion is a sibling note. See the error list below for
                // why the two must not share one element (elspeth-7bcc3d5233).
                const label = resolveComponentLabel(
                  warn.component_id,
                  nodes,
                  componentNames,
                  phraseFor,
                );
                // The identifier register lives on the prefix element, never
                // in its text: `title` is the sighted-mouse channel and
                // `data-component-id` the forensic one a support export or a
                // test can read without a hover.
                const warningText = (
                  <>
                    <strong
                      title={label.raw ?? undefined}
                      data-component-id={label.raw ?? undefined}
                    >
                      {label.name}:
                    </strong>{" "}
                    {finding.headline}
                  </>
                );

                return (
                  <li key={i} className="validation-banner-warn-item">
                    {isClickable ? (
                      <Button
                        variant="bare"
                        onClick={() => {
                          if (warn.component_id) {
                            onComponentClick?.(warn.component_id);
                          }
                        }}
                        className="validation-banner-component-btn validation-banner-component-btn--warning"
                        title={`Click to select ${warn.component_id} in the pipeline view`}
                      >
                        {warningText}
                      </Button>
                    ) : (
                      warningText
                    )}
                    {warn.suggestion && (
                      <div className="validation-banner-suggestion">
                        Suggestion: {warn.suggestion}
                      </div>
                    )}
                    {finding.raw !== null && (
                      <details className="validation-banner-technical">
                        <summary>Technical details</summary>
                        <pre>{finding.raw}</pre>
                      </details>
                    )}
                  </li>
                );
              })}
            </ul>
          </div>
        )}
      </div>
    );
  }

  return (
    <div
      role="alert"
      className="validation-banner validation-banner-fail"
    >
      <div className="validation-banner-fail-title">
        Validation failed
      </div>
      <ul className="validation-banner-fail-list">
        {result.errors.map((err, i) => {
          const isClickable =
            Boolean(onComponentClick) &&
            isNavigableComponent(err.component_id, nodes, componentNames);
          const finding = humaniseValidationMessage(err.message, phraseFor, stepLabelFor);
          // elspeth-7bcc3d5233. The suggestion is a SIBLING of the button,
          // never its child: a block-level <div> inside a <button> is invalid
          // (flow content in a button), and it dragged the button's underline
          // — a deliberate affordance on a real control, shared.css:855-874 —
          // across the helper note, made a four-line block one hit target, and
          // pushed the list marker down beside the "Suggestion:" line. The
          // underline itself stays; only the thing it underlines changes.
          const label = resolveComponentLabel(
            err.component_id,
            nodes,
            componentNames,
            phraseFor,
          );
          const errorText = (
            <>
              <strong
                title={label.raw ?? undefined}
                data-component-id={label.raw ?? undefined}
              >
                {label.name}:
              </strong>{" "}
              {finding.headline}
            </>
          );

          return (
            <li key={i} className="validation-banner-error-item">
              {isClickable ? (
                <Button
                  variant="bare"
                  onClick={() => {
                    if (err.component_id) {
                      onComponentClick?.(err.component_id);
                    }
                  }}
                  className="validation-banner-component-btn validation-banner-component-btn--error"
                  title={`Click to select ${err.component_id} in the pipeline view`}
                >
                  {errorText}
                </Button>
              ) : (
                errorText
              )}
              {err.suggestion && (
                <div className="validation-banner-suggestion">
                  Suggestion: {err.suggestion}
                </div>
              )}
              {finding.raw !== null && (
                <details className="validation-banner-technical">
                  <summary>Technical details</summary>
                  <pre>{finding.raw}</pre>
                </details>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
