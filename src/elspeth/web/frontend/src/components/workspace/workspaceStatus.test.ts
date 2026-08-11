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
    rows: [],
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
      text: "1 errors",
      tone: "error",
      accessibleLabel: "Validation: 1 errors",
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
      }),
    ).toMatchObject({ text: "Checking", tone: "busy" });
    expect(
      projectAuditWorkspaceStatus({
        activeSessionId: SESSION_ID,
        compositionVersion: 4,
        snapshotsBySession: {
          [SESSION_ID]: auditSnapshot({ session_id: "session-2" }),
        },
      }),
    ).toMatchObject({ text: "Checking", tone: "busy" });
  });

  it("counts warning and error audit rows as issues only on a fresh snapshot", () => {
    const withIssues = auditSnapshot({
      rows: [
        {
          id: "validation",
          label: "Validation",
          status: "error",
          summary: "Blocked",
          detail: null,
          component_ids: [],
        },
        {
          id: "provenance",
          label: "Provenance",
          status: "warning",
          summary: "Review",
          detail: null,
          component_ids: [],
        },
        {
          id: "retention",
          label: "Retention",
          status: "ok",
          summary: "Ready",
          detail: null,
          component_ids: [],
        },
      ],
    });

    expect(
      projectAuditWorkspaceStatus({
        activeSessionId: SESSION_ID,
        compositionVersion: 4,
        snapshotsBySession: { [SESSION_ID]: withIssues },
      }),
    ).toEqual({
      text: "2 issues",
      tone: "error",
      accessibleLabel: "Audit: 2 issues",
    });
  });
});
