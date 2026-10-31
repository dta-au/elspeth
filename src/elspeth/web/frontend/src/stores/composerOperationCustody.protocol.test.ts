import { beforeEach, describe, expect, it } from "vitest";
import {
  acquireComposerOperationCustody,
  authenticateComposerCustody,
  COMPOSER_CUSTODY_KEY,
  composerCustodyRecoveryNotice,
  composerProtocolVersion,
  findComposerOperationCustody,
  purgeComposerCustody,
  recordComposerProtocolVersion,
} from "./composerOperationCustody";
import type { SubmittedCustody } from "@/types/composerOperations";

const scope = { principalId: "principal-1", authProvider: "local" };
const sid = "11111111-1111-4111-8111-111111111111";
const id = "22222222-2222-4222-8222-222222222222";
function submitted(extra: Record<string, unknown> = {}): SubmittedCustody {
  return { mode: "submitted", scope, sessionId: sid, operationId: id, kind: "compose_message", createdAt: Date.now(), body: { operation_id: id, content: "hello", state_id: null }, ...extra } as SubmittedCustody;
}
function persistedWith(descriptor: Record<string, unknown>): void {
  sessionStorage.setItem(COMPOSER_CUSTODY_KEY, JSON.stringify({ schema: "composer-operations.v1", entries: [{ foreground: descriptor, unresolvedSubmissions: [] }] }));
}
beforeEach(() => { purgeComposerCustody(); sessionStorage.clear(); });

describe("Composer-Operation-Version in durable custody (F-9)", () => {
  it("hydrates a pre-upgrade record with no version as version 1", () => {
    persistedWith({ ...submitted() });
    authenticateComposerCustody(scope);
    const foreground = findComposerOperationCustody(scope, sid).foreground;
    expect(foreground).not.toBeNull();
    expect(composerProtocolVersion(foreground!)).toBe(1);
    expect(composerCustodyRecoveryNotice()).toBeNull();
  });
  it("keeps a persisted version 2 across reload", () => {
    persistedWith({ ...submitted(), protocolVersion: 2 });
    authenticateComposerCustody(scope);
    expect(composerProtocolVersion(findComposerOperationCustody(scope, sid).foreground!)).toBe(2);
  });
  it("quarantines an unknown persisted version instead of guessing", () => {
    persistedWith({ ...submitted(), protocolVersion: 3 });
    authenticateComposerCustody(scope);
    expect(findComposerOperationCustody(scope, sid).foreground).toBeNull();
    expect(composerCustodyRecoveryNotice()).not.toBeNull();
  });
  it("records the server's echoed version, not the requested one, and persists it", () => {
    authenticateComposerCustody(scope);
    const admitted = acquireComposerOperationCustody(submitted({ protocolVersion: 2 }));
    const echoed = recordComposerProtocolVersion(admitted, 1);
    expect(composerProtocolVersion(echoed)).toBe(1);
    expect(composerProtocolVersion(findComposerOperationCustody(scope, sid).foreground!)).toBe(1);
    expect(JSON.parse(sessionStorage.getItem(COMPOSER_CUSTODY_KEY)!).entries[0].foreground.protocolVersion).toBe(1);
  });
});
