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

  const safeRejection = {
    category: "request_rejected", exception_type: "BadRequestError", reason_code: "invalid_request",
    reason_disposition: "retained", reason_source: "sdk_body", diagnostic_reference: null, retryable: false,
  };

  function validationBody(rejection: unknown) {
    return {
      is_valid: false, errors: [], warnings: [],
      checks: [{ name: "advisor_signoff", passed: false, detail: "Review rejected.",
        affected_nodes: [], outcome_code: null, request_rejection: rejection }],
      readiness: { authoring_valid: true, execution_ready: true, completion_ready: false, blockers: [] },
    };
  }

  it("admits and preserves a closed live check rejection", async () => {
    fetchSpy.mockResolvedValue(new Response(JSON.stringify(validationBody(safeRejection)), { status: 200 }));
    const result = await validatePipeline("session-1");
    expect(result.checks[0].request_rejection).toEqual(safeRejection);
  });

  it.each([
    null, { message: "PRIVATE_PROVIDER_TEXT" }, { ...safeRejection, reason_code: "PRIVATE_UNKNOWN" },
    { ...safeRejection, reason_source: "unavailable" }, { ...safeRejection, diagnostic_reference: "PRIVATE_REFERENCE" },
    { ...safeRejection, retryable: true }, { ...safeRejection, raw_body: "PRIVATE_PROVIDER_TEXT" },
  ])("refuses malformed live check rejection %j", async (rejection) => {
    fetchSpy.mockResolvedValue(new Response(JSON.stringify(validationBody(rejection)), { status: 200 }));
    await expect(validatePipeline("session-1")).rejects.toMatchObject({
      status: 200, detail: "Unexpected validation checks from validate endpoint",
    });
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

  it.each([undefined, 7, {}, true])("rejects malformed live blocker suggestion %s", async (suggestion) => {
    fetchSpy.mockResolvedValue(new Response(JSON.stringify({
      is_valid: true, checks: [], errors: [],
      readiness: { authoring_valid: true, execution_ready: true, completion_ready: false,
        blockers: [{ code: "advisor_signoff_blocked", component_id: "pipeline",
          component_type: "pipeline", detail: "Review pending.", suggestion,
          note: null }] },
    }), { status: 200 }));
    await expect(validatePipeline("session-1")).rejects.toMatchObject({
      detail: "Unexpected readiness shape from validate endpoint",
    });
  });

  it.each([null, "Retry advisory review."])("preserves live blocker suggestion %s", async (suggestion) => {
    fetchSpy.mockResolvedValue(new Response(JSON.stringify({
      is_valid: true, checks: [], errors: [],
      readiness: { authoring_valid: true, execution_ready: true, completion_ready: false,
        blockers: [{ code: "advisor_signoff_blocked", component_id: "pipeline",
          component_type: "pipeline", detail: "Review pending.", suggestion,
          note: null }] },
    }), { status: 200 }));
    const result = await validatePipeline("session-1");
    expect(result.readiness.blockers[0].suggestion).toBe(suggestion);
  });

  it.each([null, "The merge step needs a failure route."])("preserves live blocker note %s", async (note) => {
    fetchSpy.mockResolvedValue(new Response(JSON.stringify({
      is_valid: true, checks: [], errors: [],
      readiness: { authoring_valid: true, execution_ready: true, completion_ready: false,
        blockers: [{ code: "advisor_signoff_blocked", component_id: "pipeline",
          component_type: "pipeline", detail: "Review pending.", suggestion: null, note }] },
    }), { status: 200 }));
    const result = await validatePipeline("session-1");
    expect(result.readiness.blockers[0].note).toBe(note);
  });

  it.each([
    { label: "missing", note: undefined },
    { label: "numeric", note: 7 },
    { label: "array", note: ["Reviewer text"] },
    { label: "object", note: { text: "Reviewer text" } },
    { label: "boolean", note: true },
  ])("rejects $label live blocker note", async ({ note }) => {
    fetchSpy.mockResolvedValue(new Response(JSON.stringify({
      is_valid: true, checks: [], errors: [],
      readiness: { authoring_valid: true, execution_ready: true, completion_ready: false,
        blockers: [{ code: "advisor_signoff_blocked", component_id: "pipeline",
          component_type: "pipeline", detail: "Review pending.", suggestion: null, note }] },
    }), { status: 200 }));
    await expect(validatePipeline("session-1")).rejects.toMatchObject({
      detail: "Unexpected readiness shape from validate endpoint",
    });
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
