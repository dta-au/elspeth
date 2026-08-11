import { describe, expect, it } from "vitest";

import { makeValidationResult } from "@/test/composerFixtures";
import type { AuditReadinessSnapshot } from "@/types/api";
import {
  projectAuditWorkspaceStatus,
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
    });
    expect(
      projectValidationWorkspaceStatus(makeValidationResult()),
    ).toEqual({
      text: "Passed",
      tone: "success",
      accessibleLabel: "Validation: Passed",
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
    });
  });

  it("projects validation request progress and failure without exposing raw details", () => {
    expect(projectValidationWorkspaceStatus(null, true, null)).toEqual({
      text: "Checking",
      tone: "busy",
      accessibleLabel: "Validation: Checking",
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
    });
  });
});
