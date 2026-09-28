import { describe, expect, it, vi } from "vitest";

import {
  FREEFORM_BUILD_ACTION_NAMES,
  finishTutorialAndVerifyGraduation,
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

  function graduationPage(hash: string, clickError?: Error) {
    const click = vi.fn(async () => {
      if (clickError !== undefined) throw clickError;
    });
    const waitFor = vi.fn(async () => undefined);
    const page = {
      getByRole: vi.fn(() => ({ click, waitFor })),
      getByLabel: vi.fn(() => ({ waitFor })),
      waitForURL: vi.fn(async (matches: (url: URL) => boolean) => {
        if (!matches(new URL(`https://staging.example/${hash}`))) throw new Error("wrong session landing");
      }),
    };
    return { page, click, waitFor };
  }

  it("proves the final click, same-session landing, and persisted completion", async () => {
    const { page, click, waitFor } = graduationPage(`#/${sessionId}`);
    const loadPreferences = vi.fn(async () => ({
      tutorial_completed_at: "2026-09-28T00:00:00Z",
      tutorial_stage: null,
      tutorial_session_id: null,
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
    }));

    await expect(finishTutorialAndVerifyGraduation(page as never, sessionId, loadPreferences)).resolves.toEqual({
      completed_at: "2026-09-28T00:00:00Z",
      landed_session_id: sessionId,
    });
    expect(click).toHaveBeenCalledOnce();
    expect(waitFor).toHaveBeenCalled();
    expect(page.waitForURL).toHaveBeenCalledOnce();
    expect(loadPreferences).toHaveBeenCalledOnce();
  });

  it("fails when the final click fails, the landing changes session, or completion did not persist", async () => {
    const persisted = async () => ({ tutorial_completed_at: "2026-09-28T00:00:00Z", tutorial_stage: null, tutorial_session_id: null, tutorial_run_id: null, tutorial_source_data_hash: null });
    const failedClick = graduationPage(`#/${sessionId}`, new Error("rename failed"));
    await expect(finishTutorialAndVerifyGraduation(failedClick.page as never, sessionId, persisted)).rejects.toThrow("rename failed");

    const wrongSession = graduationPage("#/some-other-session");
    await expect(finishTutorialAndVerifyGraduation(wrongSession.page as never, sessionId, persisted)).rejects.toThrow("wrong session landing");

    const missingCompletion = graduationPage(`#/${sessionId}`);
    await expect(finishTutorialAndVerifyGraduation(missingCompletion.page as never, sessionId, async () => ({ tutorial_completed_at: null, tutorial_stage: "graduation", tutorial_session_id: sessionId, tutorial_run_id: null, tutorial_source_data_hash: null }))).rejects.toThrow(/completion.*persist/i);
  });
});
