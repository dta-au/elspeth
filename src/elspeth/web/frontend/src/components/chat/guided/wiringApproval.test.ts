import { describe, expect, it } from "vitest";

import { approvalStopReason } from "./wiringApproval";

const CLEAN = { can_confirm: true, blockers: [], warnings: [] };
const NO_CLIENT_BLOCKERS = { pendingAcknowledgements: 0, validationIssues: 0 };

describe("approvalStopReason", () => {
  it("returns null when the wiring is clean, so a one-click approval may confirm it unseen", () => {
    expect(approvalStopReason(CLEAN, NO_CLIENT_BLOCKERS)).toBeNull();
  });

  it("stops on a server blocker", () => {
    const reason = approvalStopReason(
      { ...CLEAN, blockers: [{ code: "pipeline_invalid" }] },
      NO_CLIENT_BLOCKERS,
    );
    expect(reason).not.toBeNull();
  });

  it("stops when the server says the wiring cannot be confirmed", () => {
    expect(
      approvalStopReason({ ...CLEAN, can_confirm: false }, NO_CLIENT_BLOCKERS),
    ).not.toBeNull();
  });

  // The operator's explicit call: a warning is advisory for the normal
  // Confirm button, but it must stop a one-click approval — approving unseen
  // is exactly the case where an unread warning would go unread forever.
  it("stops on warnings even though they never block the normal confirm", () => {
    const reason = approvalStopReason(
      { ...CLEAN, warnings: [{ message: "output never receives data" }] },
      NO_CLIENT_BLOCKERS,
    );
    expect(reason).not.toBeNull();
    expect(reason).toContain("1 warning");
  });

  it("counts multiple warnings in the reason", () => {
    const reason = approvalStopReason(
      { ...CLEAN, warnings: [{ a: 1 }, { b: 2 }, { c: 3 }] },
      NO_CLIENT_BLOCKERS,
    );
    expect(reason).toContain("3 warnings");
  });

  it("stops on pending acknowledgements the transition surfaced", () => {
    const reason = approvalStopReason(CLEAN, {
      pendingAcknowledgements: 2,
      validationIssues: 0,
    });
    expect(reason).not.toBeNull();
    expect(reason).toContain("2 decisions");
  });

  it("stops on client-known validation issues", () => {
    expect(
      approvalStopReason(CLEAN, {
        pendingAcknowledgements: 0,
        validationIssues: 1,
      }),
    ).not.toBeNull();
  });

  // Severity order: a pipeline that cannot be confirmed at all must not be
  // described as merely carrying warnings.
  it("reports the blocking problem ahead of a co-occurring warning", () => {
    const reason = approvalStopReason(
      {
        can_confirm: false,
        blockers: [{ code: "pipeline_invalid" }],
        warnings: [{ message: "advisory" }],
      },
      NO_CLIENT_BLOCKERS,
    );
    expect(reason).not.toContain("warning");
  });
});
