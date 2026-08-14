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

import { useState } from "react";

import { Button, Icon } from "@/components/ui";
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

/**
 * Resolve a component_id to a human-readable display name.
 * Falls back to the raw component_id if no matching node is found.
 */
function resolveComponentName(
  componentId: string | null,
  nodes: NodeSpec[] | undefined,
  componentNames: Record<string, string> | undefined,
): string {
  if (!componentId) return "unknown";
  if (
    componentNames &&
    Object.prototype.hasOwnProperty.call(componentNames, componentId)
  ) {
    return componentNames[componentId];
  }
  if (!nodes) return componentId;
  const node = nodes.find((n) => n.id === componentId);
  return node ? `${node.node_type}:${node.id}` : componentId;
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
        {result.checks.length > 0 && (
          <ul className="validation-banner-checks">
            {result.checks.map((check, i) => (
              <li key={i} className="validation-banner-check-item">
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
                // The warning TEXT is what the button navigates from; the
                // suggestion is a sibling note. See the error list below for
                // why the two must not share one element (elspeth-7bcc3d5233).
                const warningText = (
                  <>
                    <strong>
                      [{warn.component_type ?? "unknown"}]{" "}
                      {resolveComponentName(
                        warn.component_id,
                        nodes,
                        componentNames,
                      )}:
                    </strong>{" "}
                    {warn.message}
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
          // elspeth-7bcc3d5233. The suggestion is a SIBLING of the button,
          // never its child: a block-level <div> inside a <button> is invalid
          // (flow content in a button), and it dragged the button's underline
          // — a deliberate affordance on a real control, shared.css:855-874 —
          // across the helper note, made a four-line block one hit target, and
          // pushed the list marker down beside the "Suggestion:" line. The
          // underline itself stays; only the thing it underlines changes.
          const errorText = (
            <>
              <strong>
                [{err.component_type ?? "unknown"}]{" "}
                {resolveComponentName(err.component_id, nodes, componentNames)}:
              </strong>{" "}
              {err.message}
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
            </li>
          );
        })}
      </ul>
    </div>
  );
}
