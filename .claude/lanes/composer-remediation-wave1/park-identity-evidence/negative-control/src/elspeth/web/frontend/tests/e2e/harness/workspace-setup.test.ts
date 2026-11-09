import { describe, expect, it, vi } from "vitest";

import { setupWorkspaceScenario } from "../helpers/workspace-setup";

describe("setupWorkspaceScenario", () => {
  it("cleans the created session when setup fails before ownership reaches the caller", async () => {
    const setupError = new Error("graph failed to settle");
    const cleanup = vi.fn().mockResolvedValue(undefined);

    await expect(
      setupWorkspaceScenario(
        vi.fn().mockResolvedValue("session-1"),
        vi.fn().mockRejectedValue(setupError),
        cleanup,
      ),
    ).rejects.toBe(setupError);
    expect(cleanup).toHaveBeenCalledExactlyOnceWith("session-1");
  });

  it("does not clean a successful setup before the caller's finally block", async () => {
    const cleanup = vi.fn().mockResolvedValue(undefined);

    await expect(
      setupWorkspaceScenario(
        vi.fn().mockResolvedValue("session-1"),
        vi.fn().mockResolvedValue("ready"),
        cleanup,
      ),
    ).resolves.toEqual({ sessionId: "session-1", value: "ready" });
    expect(cleanup).not.toHaveBeenCalled();
  });
});
