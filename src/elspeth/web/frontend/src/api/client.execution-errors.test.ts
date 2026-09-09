import { describe, expect, it } from "vitest";

import { parseResponse } from "./client";
import type { ApiError } from "@/types/index";

async function parseApiError(
  nestedDetail: Record<string, unknown>,
  status: number,
  statusText: string,
): Promise<ApiError> {
  const response = {
    ok: false,
    status,
    statusText,
    json: async () => ({ detail: nestedDetail }),
  } as Response;

  try {
    await parseResponse(response);
  } catch (error) {
    return error as ApiError;
  }
  throw new Error("parseResponse unexpectedly accepted an error response");
}

describe("parseResponse execution error envelopes", () => {
  it("normalises the backend blob-source-path envelope to useful ApiError detail", async () => {
    const message =
      "Composer-stored blob source path is not structurally valid for the bound blob. " +
      "This indicates a bug in composer persistence; the operator must investigate the captured composition state.";

    const error = await parseApiError(
      {
        kind: "blob_source_path_mismatch",
        issue: "retired-ticket-reference",
        message,
      },
      500,
      "Internal Server Error",
    );

    expect(error).toMatchObject({
      status: 500,
      error_type: "blob_source_path_mismatch",
      detail: message,
    });
    expect(error.detail).not.toBe("Internal Server Error");
  });

  it("normalises the backend unresolved-interpretation envelope to useful ApiError detail", async () => {
    const message =
      "Unresolved interpretation review(s) — {{interpretation:cool}} in LLM transform 'rate_node'. " +
      "Resolve the review before running the pipeline.";

    const error = await parseApiError(
      {
        kind: "interpretation_placeholder_unresolved",
        message,
        placeholders: [{ node_id: "rate_node", term: "cool" }],
        interpretation_sites: [
          {
            component_id: "rate_node",
            component_type: "transform",
            kind: "vague_term",
            user_term: "cool",
          },
        ],
      },
      422,
      "Unprocessable Entity",
    );

    expect(error).toMatchObject({
      status: 422,
      error_type: "interpretation_placeholder_unresolved",
      detail: message,
    });
    expect(error.detail).toContain("{{interpretation:cool}}");
  });

  it.each([
    [
      "semantic contract",
      "semantic_contract_violation",
      {
        component: "node:explode",
        message: "Semantic contract violation: 'scrape' -> 'explode'.",
        severity: "high",
      },
    ],
    [
      "pipeline validation",
      "pipeline_validation_failure",
      {
        component_id: "rate",
        component_type: "transform",
        message: "Graph validation failed: 'rate' requires field 'content' not emitted upstream",
        suggestion: "Wire an upstream node that emits 'content'.",
        error_code: null,
      },
    ],
  ])("preserves %s errors and uses their message when the legacy envelope has no detail", async (_label, kind, entry) => {
    const error = await parseApiError(
      { kind, errors: [entry] },
      422,
      "Unprocessable Entity",
    );

    expect(error.error_type).toBe(kind);
    expect(error.detail).toBe(entry.message);
    expect(error.errors).toEqual([entry]);
  });

  it("carries request_id from the fail-closed audit-integrity envelope (F-4)", async () => {
    // app.py's AuditIntegrityError handler emits request_id at the top level
    // of the body (not nested under detail) — the banner names it as the
    // support reference.
    const response = {
      ok: false,
      status: 500,
      statusText: "Internal Server Error",
      json: async () => ({
        error_type: "audit_integrity_error",
        detail:
          "ELSPETH stopped before replying because it could not verify this session's audit trail.",
        diagnostic: "no_failed_turn_metadata",
        reason: "originated outside compose-loop annotation scope",
        request_id: "req-abc123",
      }),
    } as Response;

    let error: ApiError | null = null;
    try {
      await parseResponse(response);
    } catch (raised) {
      error = raised as ApiError;
    }

    expect(error).toMatchObject({
      status: 500,
      error_type: "audit_integrity_error",
      request_id: "req-abc123",
    });
  });

  it("carries failure_code from a guided terminal-failure envelope (F13-D)", async () => {
    const error = await parseApiError(
      {
        error_type: "guided_operation_terminal_failure",
        failure_code: "policy_blocked",
        detail:
          "This pipeline is blocked by a deployment policy and cannot be built as configured. " +
          "Change the highlighted component — retrying will fail the same way.",
      },
      422,
      "Unprocessable Entity",
    );

    expect(error).toMatchObject({
      status: 422,
      error_type: "guided_operation_terminal_failure",
      failure_code: "policy_blocked",
    });
    expect(error.detail).toContain("blocked by a deployment policy");
  });

  it("drops malformed structured entries instead of exposing a false typed contract", async () => {
    const valid = {
      component_id: "rate",
      message: "Graph validation failed: rate is missing content",
    };
    const error = await parseApiError(
      {
        kind: "pipeline_validation_failure",
        errors: [null, "not-an-object", { message: 7 }, valid],
      },
      422,
      "Unprocessable Entity",
    );

    expect(error.errors).toEqual([valid]);
    expect(error.detail).toBe(valid.message);
  });
});
