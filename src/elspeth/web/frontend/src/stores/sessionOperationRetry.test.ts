import { beforeEach, describe, expect, it } from "vitest";
import {
  acquireSessionOperationRetry,
  clearAllSessionOperationRetries,
  SESSION_OPERATION_RETRY_STORAGE_KEY,
} from "./sessionOperationRetry";

const SESSION_ID = "00000000-0000-4000-8000-000000000001";

describe("session operation retry custody", () => {
  beforeEach(() => {
    clearAllSessionOperationRetries();
    sessionStorage.clear();
  });

  it("reuses a fork operation id for the same request and conflicts on a different request", () => {
    const first = acquireSessionOperationRetry("session_fork", SESSION_ID, ["message-1", "new text"]);
    expect(first.status).toBe("acquired");
    const replay = acquireSessionOperationRetry("session_fork", SESSION_ID, ["message-1", "new text"]);
    expect(replay).toEqual(first);
    expect(acquireSessionOperationRetry("session_fork", SESSION_ID, ["message-1", "other text"]).status).toBe("conflict");
  });

  it("uses only neutral fork/revert kinds in its persisted envelope", () => {
    acquireSessionOperationRetry("state_revert", SESSION_ID, ["state-1"]);
    const encoded = sessionStorage.getItem(SESSION_OPERATION_RETRY_STORAGE_KEY);
    expect(encoded).toContain('"kind":"state_revert"');
    expect(encoded).not.toContain("guided");
  });
});
