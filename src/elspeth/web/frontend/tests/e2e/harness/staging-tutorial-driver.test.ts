import { describe, expect, it } from "vitest";

import {
  FREEFORM_BUILD_ACTION_NAMES,
  isAuditRequest,
  isComposeRequest,
  isRunRequest,
} from "../../../scripts/staging-tutorial-driver.mjs";

describe("standalone staging tutorial driver contract", () => {
  const sessionId = "00000000-0000-4000-8000-000000000000";

  it("drives the ordinary freeform Build and explicit Run gestures", () => {
    expect(FREEFORM_BUILD_ACTION_NAMES).toEqual(["Send tutorial brief", "Continue to Run", "Run"]);
  });

  it("treats composer messages, not a removed guided route, as the compose step", () => {
    expect(
      isComposeRequest(
        `https://staging.example/api/sessions/${sessionId}/guided/respond`,
        "POST",
      ),
    ).toBe(false);
    expect(
      isComposeRequest(
        `https://staging.example/api/sessions/${sessionId}/messages`,
        "POST",
      ),
    ).toBe(true);
  });

  it("identifies the tutorial run request independently", () => {
    expect(
      isRunRequest("https://staging.example/api/tutorial/run", "POST"),
    ).toBe(true);
    expect(
      isRunRequest("https://staging.example/api/tutorial/run", "GET"),
    ).toBe(false);
  });

  it("identifies the evidence-backed audit story read", () => {
    expect(isAuditRequest(`https://staging.example/api/sessions/${sessionId}/runs/run-1/audit-story`, "GET")).toBe(true);
    expect(isAuditRequest(`https://staging.example/api/sessions/${sessionId}/runs/run-1/audit-story`, "POST")).toBe(false);
  });
});
