import { describe, expect, it } from "vitest";
import { isSafeRequestRejection, isValidationReadiness } from "./validationReadiness";

const rejection = {
  category: "request_rejected", exception_type: "BadRequestError", reason_code: "invalid_request",
  reason_disposition: "retained", reason_source: "http_response", diagnostic_reference: null, retryable: false,
};

describe("safe request rejection admission", () => {
  it("retains structured reasons in live or reloaded readiness", () => {
    const readiness = { authoring_valid: true, execution_ready: true, completion_ready: false,
      blockers: [{ code: "advisor_signoff_blocked", component_id: "pipeline", component_type: "pipeline",
        detail: "Review request rejected", suggestion: null, note: null, request_rejection: rejection }] };
    const reloaded = JSON.parse(JSON.stringify(readiness));
    expect(isValidationReadiness(reloaded)).toBe(true);
    expect(reloaded.blockers[0].request_rejection).toEqual(rejection);
  });

  it("accepts historical blockers without claiming a reason was retained", () => {
    const historical = { authoring_valid: true, execution_ready: true, completion_ready: false,
      blockers: [{ code: "advisor_signoff_blocked", component_id: "pipeline", component_type: "pipeline",
        detail: "Review unavailable", suggestion: null, note: null }] };
    expect(isValidationReadiness(historical)).toBe(true);
    expect("request_rejection" in historical.blockers[0]).toBe(false);
  });

  it("distinguishes absent and unrecognized reasons without accepting raw prose", () => {
    expect(isSafeRequestRejection({ ...rejection, reason_code: null, reason_disposition: "absent" })).toBe(true);
    expect(isSafeRequestRejection({ ...rejection, reason_code: null, reason_disposition: "unrecognized" })).toBe(true);
    expect(isSafeRequestRejection({ ...rejection, reason_code: "PRIVATE_PROVIDER_VALUE" })).toBe(false);
    expect(isSafeRequestRejection({ ...rejection, message: "PRIVATE_PROVIDER_VALUE" })).toBe(false);
    expect(isSafeRequestRejection({ ...rejection, diagnostic_reference: "PRIVATE_PROVIDER_VALUE" })).toBe(false);
    expect(isSafeRequestRejection({ ...rejection, reason_disposition: "absent" })).toBe(false);
    expect(isSafeRequestRejection({ ...rejection, retryable: 0 })).toBe(false);
  });
});
