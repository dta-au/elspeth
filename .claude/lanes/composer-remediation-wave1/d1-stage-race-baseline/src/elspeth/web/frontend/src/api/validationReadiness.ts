import type { SafeRequestRejection, ValidationCheck, ValidationReadiness } from "../types/index";
import { VALIDATION_CHECK_OUTCOME_CODE_VALUES } from "../types/index";

const VALIDATION_CHECK_OUTCOME_CODES = new Set<string>(VALIDATION_CHECK_OUTCOME_CODE_VALUES);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function isSafeRequestRejection(value: unknown): value is SafeRequestRejection {
  if (!isRecord(value)) return false;
  const code = value.reason_code;
  const disposition = value.reason_disposition;
  const source = value.reason_source;
  const known = code === "invalid_request" || code === "context_length_exceeded" || code === "unsupported_parameter";
  return (
    Object.keys(value).length === 7 &&
    value.category === "request_rejected" && value.exception_type === "BadRequestError" &&
    (known || code === null) &&
    (disposition === "retained" || disposition === "absent" || disposition === "unrecognized" || disposition === "unavailable") &&
    (source === "sdk_body" || source === "http_response" || source === "unavailable") &&
    (disposition === "retained") === known && !(known && source === "unavailable") &&
    (value.diagnostic_reference === null || typeof value.diagnostic_reference === "string" &&
      /^provider-failure:[0-9a-f]{64}$/.test(value.diagnostic_reference)) && value.retryable === false
  );
}

/** Shared check admission for live validation and audit reload. */
export function isValidationCheck(check: unknown): check is ValidationCheck {
  return (
    isRecord(check) &&
    typeof check.name === "string" &&
    typeof check.passed === "boolean" &&
    typeof check.detail === "string" &&
    (!("request_rejection" in check) || isSafeRequestRejection(check.request_rejection)) &&
    Array.isArray(check.affected_nodes) &&
    check.affected_nodes.every((node) => typeof node === "string") &&
    (check.outcome_code === null ||
      typeof check.outcome_code === "string" && VALIDATION_CHECK_OUTCOME_CODES.has(check.outcome_code))
  );
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
      (!("request_rejection" in blocker) || isSafeRequestRejection(blocker.request_rejection)) &&
      (typeof blocker.suggestion === "string" || blocker.suggestion === null) &&
      (typeof blocker.note === "string" || blocker.note === null),
    )
  );
}
