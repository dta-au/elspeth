import { describe, expect, it } from "vitest";

import { makeValidationResult } from "@/test/composerFixtures";
import type { AuditReadinessSnapshot } from "@/types/api";
import {
  projectAuditWorkspaceStatus,
  projectChecksWorkspaceStatus,
  projectValidationWorkspaceStatus,
} from "./workspaceStatus";

const SESSION_ID = "session-1";

function auditSnapshot(
  overrides: Partial<AuditReadinessSnapshot> = {},
): AuditReadinessSnapshot {
  return {
    session_id: SESSION_ID,
    composition_version: 4,
    checked_at: "2026-08-11T00:00:00Z",
    rows: [
      "validation",
      "plugin_trust",
      "provenance",
      "retention",
      "llm_interpretations",
      "secrets",
    ].map((id) => ({
      id,
      label: id,
      status: "ok",
      summary: "Ready",
      detail: null,
      component_ids: [],
    })) as AuditReadinessSnapshot["rows"],
    validation_result: makeValidationResult(),
    ...overrides,
  };
}

describe("workspace status projections", () => {
  it("projects the exact validation label, tone, and accessible label", () => {
    expect(projectValidationWorkspaceStatus(null)).toEqual({
      text: "Not checked",
      tone: "neutral",
      accessibleLabel: "Validation: Not checked",
      issueCount: 0,
    });
    expect(
      projectValidationWorkspaceStatus(makeValidationResult()),
    ).toEqual({
      text: "Passed",
      tone: "success",
      accessibleLabel: "Validation: Passed",
      issueCount: 0,
    });
    expect(
      projectValidationWorkspaceStatus(
        makeValidationResult({
          errors: [],
          is_valid: true,
          readiness: makeValidationResult().readiness,
        }),
      ),
    ).toMatchObject({ text: "Passed" });
    expect(
      projectValidationWorkspaceStatus({
        ...makeValidationResult(),
        warnings: [
          {
            component_id: "node-1",
            component_type: "transform",
            message: "Review this transform",
            suggestion: null,
          },
          {
            component_id: null,
            component_type: null,
            message: "Review retention",
            suggestion: null,
          },
        ],
      }),
    ).toEqual({
      text: "2 warnings",
      tone: "warning",
      accessibleLabel: "Validation: 2 warnings",
      issueCount: 2,
    });
    expect(
      projectValidationWorkspaceStatus(
        makeValidationResult({
          is_valid: false,
          errors: [
            {
              component_id: "node-1",
              component_type: "transform",
              message: "Invalid",
              suggestion: null,
            },
          ],
        }),
      ),
    ).toEqual({
      text: "1 error",
      tone: "error",
      accessibleLabel: "Validation: 1 error",
      issueCount: 1,
    });
  });

  it("uses the plural noun for more than one validation error", () => {
    expect(
      projectValidationWorkspaceStatus(
        makeValidationResult({
          is_valid: false,
          errors: [
            {
              component_id: "node-1",
              component_type: "transform",
              message: "Invalid",
              suggestion: null,
            },
            {
              component_id: "node-2",
              component_type: "transform",
              message: "Also invalid",
              suggestion: null,
            },
          ],
        }),
      ),
    ).toEqual({
      text: "2 errors",
      tone: "error",
      accessibleLabel: "Validation: 2 errors",
      issueCount: 2,
    });
  });

  it("uses the singular noun for exactly one validation warning", () => {
    expect(
      projectValidationWorkspaceStatus({
        ...makeValidationResult(),
        warnings: [
          {
            component_id: "node-1",
            component_type: "transform",
            message: "Review this transform",
            suggestion: null,
          },
        ],
      }),
    ).toEqual({
      text: "1 warning",
      tone: "warning",
      accessibleLabel: "Validation: 1 warning",
      issueCount: 1,
    });
  });

  it("fails closed when validation is invalid without structured errors", () => {
    expect(
      projectValidationWorkspaceStatus(
        makeValidationResult({ is_valid: false, errors: [] }),
      ),
    ).toEqual({
      text: "Failed",
      tone: "error",
      accessibleLabel: "Validation: Failed",
      issueCount: 0,
    });
  });

  it("projects validation request progress and failure without exposing raw details", () => {
    expect(projectValidationWorkspaceStatus(null, true, null)).toEqual({
      text: "Checking",
      tone: "busy",
      accessibleLabel: "Validation: Checking",
      issueCount: 0,
    });
    expect(
      projectValidationWorkspaceStatus(
        null,
        false,
        "sensitive upstream validation response",
      ),
    ).toEqual({
      text: "Check failed",
      tone: "error",
      accessibleLabel: "Validation: Check failed",
      issueCount: 0,
    });
  });

  it("never presents a stale or wrong-session audit snapshot as ready", () => {
    const current = auditSnapshot();
    const snapshots = { [SESSION_ID]: current };

    expect(
      projectAuditWorkspaceStatus({
        activeSessionId: SESSION_ID,
        compositionVersion: 4,
        snapshotsBySession: snapshots,
        errorBySession: {},
      }),
    ).toEqual({
      text: "Ready",
      tone: "success",
      accessibleLabel: "Audit: Ready",
      issueCount: 0,
    });
    expect(
      projectAuditWorkspaceStatus({
        activeSessionId: SESSION_ID,
        compositionVersion: 5,
        snapshotsBySession: snapshots,
        errorBySession: {},
      }),
    ).toMatchObject({ text: "Checking", tone: "busy" });
    expect(
      projectAuditWorkspaceStatus({
        activeSessionId: SESSION_ID,
        compositionVersion: 4,
        snapshotsBySession: {
          [SESSION_ID]: auditSnapshot({ session_id: "session-2" }),
        },
        errorBySession: {},
      }),
    ).toMatchObject({ text: "Checking", tone: "busy" });
  });

  it("projects an audit identity error as Error without exposing raw detail", () => {
    expect(
      projectAuditWorkspaceStatus({
        activeSessionId: SESSION_ID,
        compositionVersion: 4,
        snapshotsBySession: { [SESSION_ID]: auditSnapshot() },
        errorBySession: {
          [SESSION_ID]:
            "Audit readiness response did not match the requested composition.",
        },
      }),
    ).toEqual({
      text: "Error",
      tone: "error",
      accessibleLabel: "Audit: Error",
      issueCount: 0,
    });
  });

  it.each([
    ["incomplete", ["validation"]],
    ["duplicate", ["validation", "validation", "plugin_trust", "provenance", "retention", "llm_interpretations", "secrets"]],
  ])("fails closed for a %s readiness-row matrix", (_label, ids) => {
    const rows = ids.map((id) => ({
      id,
      label: id,
      status: "ok",
      summary: "Ready",
      detail: null,
      component_ids: [],
    })) as AuditReadinessSnapshot["rows"];
    expect(projectAuditWorkspaceStatus({
      activeSessionId: SESSION_ID,
      compositionVersion: 4,
      snapshotsBySession: { [SESSION_ID]: auditSnapshot({ rows }) },
      errorBySession: {},
    })).toMatchObject({ text: "Error", tone: "error" });
  });

  it("fails closed when validation semantics contradict an all-green row matrix", () => {
    expect(projectAuditWorkspaceStatus({
      activeSessionId: SESSION_ID,
      compositionVersion: 4,
      snapshotsBySession: {
        [SESSION_ID]: auditSnapshot({
          validation_result: makeValidationResult({ is_valid: false, errors: [] }),
        }),
      },
      errorBySession: {},
    })).toMatchObject({ text: "Error", tone: "error" });
  });

  it("counts warning and error audit rows as issues only on a fresh snapshot", () => {
    const withIssues = auditSnapshot({
      rows: auditSnapshot().rows.map((row) =>
        row.id === "validation"
          ? { ...row, status: "error" as const, summary: "Blocked" }
          : row.id === "provenance"
            ? { ...row, status: "warning" as const, summary: "Review" }
            : row,
      ),
      validation_result: makeValidationResult({ is_valid: false, errors: [] }),
    });

    expect(
      projectAuditWorkspaceStatus({
        activeSessionId: SESSION_ID,
        compositionVersion: 4,
        snapshotsBySession: { [SESSION_ID]: withIssues },
        errorBySession: {},
      }),
    ).toEqual({
      text: "2 issues",
      tone: "error",
      accessibleLabel: "Audit: 2 issues",
      // 1, not 2: the "validation" row restates the validation channel's own
      // failure, so it is excluded from the merge contribution while the
      // standalone text/tone still count it.
      issueCount: 1,
    });
  });

  it("contributes no merge count when the validation mirror row is the only issue", () => {
    const mirrorOnly = auditSnapshot({
      rows: auditSnapshot().rows.map((row) =>
        row.id === "validation"
          ? { ...row, status: "error" as const, summary: "Blocked" }
          : row,
      ),
      validation_result: makeValidationResult({ is_valid: false, errors: [] }),
    });

    expect(
      projectAuditWorkspaceStatus({
        activeSessionId: SESSION_ID,
        compositionVersion: 4,
        snapshotsBySession: { [SESSION_ID]: mirrorOnly },
        errorBySession: {},
      }),
    ).toEqual({
      text: "1 issue",
      tone: "error",
      accessibleLabel: "Audit: 1 issue",
      issueCount: 0,
    });
  });

  it("uses the singular noun for exactly one audit issue", () => {
    const withOneIssue = auditSnapshot({
      rows: auditSnapshot().rows.map((row) =>
        row.id === "provenance"
          ? { ...row, status: "warning" as const, summary: "Review" }
          : row,
      ),
    });

    expect(
      projectAuditWorkspaceStatus({
        activeSessionId: SESSION_ID,
        compositionVersion: 4,
        snapshotsBySession: { [SESSION_ID]: withOneIssue },
        errorBySession: {},
      }),
    ).toEqual({
      text: "1 issue",
      tone: "warning",
      accessibleLabel: "Audit: 1 issue",
      issueCount: 1,
    });
  });
});

describe("merged checks projection", () => {
  const validationPassed = projectValidationWorkspaceStatus(
    makeValidationResult(),
  );
  const auditReady = projectAuditWorkspaceStatus({
    activeSessionId: SESSION_ID,
    compositionVersion: 4,
    snapshotsBySession: { [SESSION_ID]: auditSnapshot() },
    errorBySession: {},
  });

  function validationWithErrors(count: number) {
    return projectValidationWorkspaceStatus(
      makeValidationResult({
        is_valid: false,
        errors: Array.from({ length: count }, (_, i) => ({
          component_id: `node-${i}`,
          component_type: "transform",
          message: "Invalid",
          suggestion: null,
        })),
      }),
    );
  }

  function auditWithIssues(ids: readonly string[]) {
    return projectAuditWorkspaceStatus({
      activeSessionId: SESSION_ID,
      compositionVersion: 4,
      snapshotsBySession: {
        [SESSION_ID]: auditSnapshot({
          rows: auditSnapshot().rows.map((row) =>
            ids.includes(row.id)
              ? { ...row, status: "warning" as const, summary: "Review" }
              : row,
          ),
        }),
      },
      errorBySession: {},
    });
  }

  it("is Ready only when both channels succeed", () => {
    expect(
      projectChecksWorkspaceStatus(validationPassed, auditReady),
    ).toEqual({
      text: "Ready",
      tone: "success",
      accessibleLabel: "Checks: Ready",
      issueCount: 0,
    });
  });

  it("counts a validation failure once even though the audit snapshot mirrors it as a row", () => {
    // The e2e "validation-audit-issues" shape: the audit snapshot's
    // "validation" row is mechanically tied to validation_result.is_valid, so
    // summing both channels naively counts every validation failure twice —
    // once in full via the validation channel, once more as the mirror row.
    const failingAudit = projectAuditWorkspaceStatus({
      activeSessionId: SESSION_ID,
      compositionVersion: 4,
      snapshotsBySession: {
        [SESSION_ID]: auditSnapshot({
          rows: auditSnapshot().rows.map((row) =>
            row.id === "validation"
              ? { ...row, status: "error" as const, summary: "Blocked" }
              : row.id === "plugin_trust"
                ? { ...row, status: "warning" as const, summary: "Review" }
                : row,
          ),
          validation_result: makeValidationResult({ is_valid: false, errors: [] }),
        }),
      },
      errorBySession: {},
    });

    expect(
      projectChecksWorkspaceStatus(validationWithErrors(2), failingAudit),
    ).toEqual({
      text: "3 issues",
      tone: "error",
      accessibleLabel: "Checks: 3 issues",
      issueCount: 3,
    });
  });

  it("sums both channels' issues and takes the worse tone (validation errors dominate audit warnings)", () => {
    expect(
      projectChecksWorkspaceStatus(
        validationWithErrors(2),
        auditWithIssues(["provenance"]),
      ),
    ).toEqual({
      text: "3 issues",
      tone: "error",
      accessibleLabel: "Checks: 3 issues",
      issueCount: 3,
    });
  });

  it("sums both channels' issues and takes the worse tone (audit warnings surface past a passing validation)", () => {
    expect(
      projectChecksWorkspaceStatus(
        validationPassed,
        auditWithIssues(["provenance", "retention"]),
      ),
    ).toEqual({
      text: "2 issues",
      tone: "warning",
      accessibleLabel: "Checks: 2 issues",
      issueCount: 2,
    });
  });

  it("uses the singular noun for exactly one merged issue", () => {
    expect(
      projectChecksWorkspaceStatus(validationPassed, auditWithIssues(["provenance"])),
    ).toMatchObject({ text: "1 issue", tone: "warning", issueCount: 1 });
  });

  it("reports Checking while either channel is still in flight", () => {
    expect(
      projectChecksWorkspaceStatus(
        projectValidationWorkspaceStatus(null, true, null),
        auditReady,
      ),
    ).toMatchObject({
      text: "Checking",
      tone: "busy",
      accessibleLabel: "Checks: Checking",
    });
    expect(
      projectChecksWorkspaceStatus(
        projectValidationWorkspaceStatus(null),
        projectAuditWorkspaceStatus({
          activeSessionId: SESSION_ID,
          compositionVersion: 5,
          snapshotsBySession: { [SESSION_ID]: auditSnapshot() },
          errorBySession: {},
        }),
      ),
    ).toMatchObject({ text: "Checking", tone: "busy" });
  });

  it("stays Not checked when validation never ran and nothing is in flight", () => {
    expect(
      projectChecksWorkspaceStatus(
        projectValidationWorkspaceStatus(null),
        auditReady,
      ),
    ).toEqual({
      text: "Not checked",
      tone: "neutral",
      accessibleLabel: "Checks: Not checked",
      issueCount: 0,
    });
  });

  it("projects a zero-count failure as Check failed, never as 0 issues", () => {
    expect(
      projectChecksWorkspaceStatus(
        projectValidationWorkspaceStatus(null, false, "boom"),
        auditReady,
      ),
    ).toEqual({
      text: "Check failed",
      tone: "error",
      accessibleLabel: "Checks: Check failed",
      issueCount: 0,
    });
    expect(
      projectChecksWorkspaceStatus(
        validationPassed,
        projectAuditWorkspaceStatus({
          activeSessionId: SESSION_ID,
          compositionVersion: 4,
          snapshotsBySession: { [SESSION_ID]: auditSnapshot() },
          errorBySession: { [SESSION_ID]: "identity mismatch" },
        }),
      ),
    ).toMatchObject({ text: "Check failed", tone: "error", issueCount: 0 });
  });

  it("an error tone with a real count keeps the count as the text", () => {
    expect(
      projectChecksWorkspaceStatus(validationWithErrors(1), auditReady),
    ).toMatchObject({ text: "1 issue", tone: "error", issueCount: 1 });
  });
});
