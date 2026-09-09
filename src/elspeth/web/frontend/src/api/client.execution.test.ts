import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { executePipeline, validatePipeline } from "./client";

describe("api/client execution state binding", () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    fetchSpy.mockRestore();
  });

  it("sends the reviewed state id when validating a pipeline", async () => {
    fetchSpy.mockResolvedValue({
      ok: true,
      json: async () => ({
        is_valid: true,
        checks: [],
        errors: [],
        warnings: [],
        readiness: {
          authoring_valid: true,
          execution_ready: true,
          completion_ready: true,
          blockers: [],
        },
      }),
    } as Response);

    await validatePipeline("session-1", "state-1");

    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/sessions/session-1/validate?state_id=state-1",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("sends the reviewed state id and preserves fanout acknowledgement body", async () => {
    fetchSpy.mockResolvedValue({
      ok: true,
      json: async () => ({ run_id: "run-1" }),
    } as Response);

    await executePipeline(
      "session-1",
      { accepted: true, token: "ack-token" },
      undefined,
      "state-1",
    );

    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/sessions/session-1/execute?state_id=state-1",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ fanout_ack_token: "ack-token" }),
      }),
    );
  });

  it("carries the secret acknowledgement token alongside the fanout token", async () => {
    fetchSpy.mockResolvedValue({
      ok: true,
      json: async () => ({ run_id: "run-1" }),
    } as Response);

    await executePipeline(
      "session-1",
      { accepted: true, token: "fanout-token" },
      { accepted: true, token: "secret-token" },
      "state-1",
    );

    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/sessions/session-1/execute?state_id=state-1",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          fanout_ack_token: "fanout-token",
          secret_ack_token: "secret-token",
        }),
      }),
    );
  });

  it("sends the secret acknowledgement token on its own", async () => {
    fetchSpy.mockResolvedValue({
      ok: true,
      json: async () => ({ run_id: "run-1" }),
    } as Response);

    await executePipeline(
      "session-1",
      undefined,
      { accepted: true, token: "secret-token" },
      "state-1",
    );

    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/sessions/session-1/execute?state_id=state-1",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ secret_ack_token: "secret-token" }),
      }),
    );
  });
});
