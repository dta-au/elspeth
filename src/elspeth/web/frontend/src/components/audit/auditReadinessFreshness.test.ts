import { describe, expect, it } from "vitest";

import { makeValidationResult } from "@/test/composerFixtures";
import type { AuditReadinessSnapshot } from "@/types/api";
import { matchingAuditReadinessSnapshot } from "./auditReadinessFreshness";

function snapshot(
  sessionId: string,
  compositionVersion: number,
): AuditReadinessSnapshot {
  return {
    session_id: sessionId,
    composition_version: compositionVersion,
    checked_at: "2026-08-11T00:00:00Z",
    rows: [],
    validation_result: makeValidationResult(),
  };
}

describe("matchingAuditReadinessSnapshot", () => {
  it("returns only a snapshot matching the active session and composition version", () => {
    const current = snapshot("session-1", 7);

    expect(matchingAuditReadinessSnapshot(current, "session-1", 7)).toBe(
      current,
    );
    expect(
      matchingAuditReadinessSnapshot(current, "session-2", 7),
    ).toBeUndefined();
    expect(
      matchingAuditReadinessSnapshot(current, "session-1", 8),
    ).toBeUndefined();
    expect(
      matchingAuditReadinessSnapshot(undefined, "session-1", 7),
    ).toBeUndefined();
    expect(
      matchingAuditReadinessSnapshot(current, null, 7),
    ).toBeUndefined();
    expect(
      matchingAuditReadinessSnapshot(current, "session-1", null),
    ).toBeUndefined();
  });
});
