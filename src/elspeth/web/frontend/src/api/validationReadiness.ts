import type { ValidationReadiness } from "../types/index";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Shared admission for readiness returned by live validation and reload. */
export function isValidationReadiness(value: unknown): value is ValidationReadiness {
  return (
    isRecord(value) &&
    typeof value.authoring_valid === "boolean" &&
    typeof value.execution_ready === "boolean" &&
    typeof value.completion_ready === "boolean" &&
    Array.isArray(value.blockers) &&
    value.blockers.every((blocker: unknown) =>
      isRecord(blocker) &&
      typeof blocker.code === "string" &&
      (typeof blocker.component_id === "string" || blocker.component_id === null) &&
      (typeof blocker.component_type === "string" || blocker.component_type === null) &&
      typeof blocker.detail === "string" &&
      (typeof blocker.suggestion === "string" || blocker.suggestion === null) &&
      (typeof blocker.note === "string" || blocker.note === null),
    )
  );
}
