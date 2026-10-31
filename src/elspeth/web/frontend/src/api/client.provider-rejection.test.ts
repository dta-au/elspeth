import { afterEach, describe, expect, it, vi } from "vitest";
import { apiErrorFromBody, parseResponse } from "./client";
import { decodeComposerOperationSnapshot } from "./composerOperationDecoder";
import { decodeComposerOperationSnapshotV2 } from "./composerOperationDecoderV2";
import type { SafeRequestRejection } from "@/types";

const sessionId = "11111111-1111-4111-8111-111111111111";
const operationId = "22222222-2222-4222-8222-222222222222";
const reference = `provider-failure:${"a".repeat(64)}`;
const rejection: SafeRequestRejection = {
  category: "request_rejected", exception_type: "BadRequestError",
  reason_code: "context_length_exceeded", reason_disposition: "retained",
  reason_source: "http_response", diagnostic_reference: reference, retryable: false,
};
const safeDetail = "LLM request rejected (BadRequestError)";
const privateCanary = "PRIVATE_PRIMARY_DEBUG_BODY_CANARY";

function envelope(descriptor: unknown, nested: boolean) {
  const detail = {
    error_type: "llm_unavailable", detail: safeDetail,
    provider_detail: privateCanary, provider_status_code: 400,
    request_rejection: descriptor,
  };
  return nested ? { detail } : detail;
}

async function httpFailure(body: unknown): Promise<unknown> {
  try {
    await parseResponse(new Response(JSON.stringify(body), { status: 502, statusText: "Bad Gateway" }));
  } catch (error) {
    return error;
  }
  throw new Error("The real HTTP client accepted a rejected response");
}

afterEach(() => { vi.unstubAllGlobals(); });

describe("primary provider rejection at the real shared API-error boundary", () => {
  it.each([false, true])("retains the exact seven-field descriptor from HTTP and durable bodies (nested %s)", async (nested) => {
    const body = envelope(rejection, nested);
    const admitted = apiErrorFromBody(502, body);
    const received = await httpFailure(body);
    for (const error of [admitted, received]) {
      expect(error).toMatchObject({ status: 502, error_type: "llm_unavailable", detail: safeDetail, request_rejection: rejection });
      expect(error).toHaveProperty("request_rejection.diagnostic_reference", reference);
      expect(error).toHaveProperty("provider_detail", privateCanary);
      expect(error).toHaveProperty("provider_status_code", 400);
    }
  });

  it.each([1, 2])("retains rejection after the actual V%s terminal-body decoder", (version) => {
    const error = { http_status: 502, body: envelope(rejection, true), error_type: "llm_unavailable", failure_code: "http_error", diagnostic_id: null };
    const common = { operation_id: operationId, kind: "compose_message", status: "failed", cancel_requested: false, poll_after_ms: 1000, result: null, error };
    const decoded = version === 1
      ? decodeComposerOperationSnapshot({ ...common, deadline_at: "2026-10-31T00:00:00Z", deadline_remaining_ms: 0 }, sessionId, operationId)
      : decodeComposerOperationSnapshotV2({ ...common, protocol_version: 2, phase: "settled", status_revision: 9, created_at: "2026-10-31T00:00:00Z", observed_at: "2026-10-31T00:00:00Z", segment: null }, sessionId, operationId);
    expect(decoded.error).not.toBeNull();
    if (decoded.error === null) throw new Error("The real durable decoder dropped the terminal failure");
    expect(apiErrorFromBody(decoded.error.http_status, decoded.error.body)).toMatchObject({ request_rejection: rejection, error_type: "llm_unavailable" });
    expect(decoded.error.failure_code).toBe("http_error");
  });

  it("retains a valid present descriptor whose diagnostic reference is unavailable", async () => {
    const descriptor = { ...rejection, diagnostic_reference: null };
    const body = envelope(descriptor, true);
    expect(apiErrorFromBody(502, body)).toMatchObject({ request_rejection: descriptor });
    expect(await httpFailure(body)).toMatchObject({ request_rejection: descriptor });
  });

  it.each([false, true])("keeps historical absence without inventing an absent-reason descriptor (nested %s)", async (nested) => {
    const inner = { error_type: "llm_unavailable", detail: safeDetail };
    const body = nested ? { detail: inner } : inner;
    for (const error of [apiErrorFromBody(502, body), await httpFailure(body)]) {
      expect(error).toMatchObject({ status: 502, error_type: "llm_unavailable", detail: safeDetail });
      expect(error).not.toHaveProperty("request_rejection.reason_disposition");
    }
  });

  const missingReference = { ...rejection } as Partial<SafeRequestRejection>;
  delete missingReference.diagnostic_reference;
  const malformed = [
    { label: "null", value: null },
    { label: "array", value: [] },
    { label: "extra private field", value: { ...rejection, message: privateCanary } },
    { label: "missing reference field", value: missingReference },
    { label: "wrong category", value: { ...rejection, category: "provider_error" } },
    { label: "wrong exception", value: { ...rejection, exception_type: "RuntimeError" } },
    { label: "unknown reason", value: { ...rejection, reason_code: privateCanary } },
    { label: "invalid reference", value: { ...rejection, diagnostic_reference: "request-id-is-not-provider-evidence" } },
    { label: "retryable request", value: { ...rejection, retryable: true } },
    { label: "retained without code", value: { ...rejection, reason_code: null } },
    { label: "absent with code", value: { ...rejection, reason_disposition: "absent" } },
    { label: "retained unavailable source", value: { ...rejection, reason_source: "unavailable" } },
  ];

  describe.each([false, true])("strict present descriptor (nested %s)", (nested) => {
    it.each(malformed)("refuses $label through the durable shared decoder", ({ value }) => {
      expect(() => apiErrorFromBody(502, envelope(value, nested))).toThrow(Error);
    });
    it.each(malformed)("refuses $label through HTTP without swallowing it into status text", async ({ value }) => {
      await expect(httpFailure(envelope(value, nested))).resolves.toBeInstanceOf(Error);
    });
  });

  it("admits equal independently decoded top-level and nested evidence", async () => {
    const body = { request_rejection: structuredClone(rejection), detail: envelope(structuredClone(rejection), false) };
    expect(apiErrorFromBody(502, body)).toMatchObject({ request_rejection: rejection });
    expect(await httpFailure(body)).toMatchObject({ request_rejection: rejection });
  });

  it("refuses conflicting top-level and nested descriptors instead of choosing one", async () => {
    const body = { request_rejection: { ...rejection, reason_code: "unsupported_parameter" }, detail: envelope(rejection, false) };
    expect(() => apiErrorFromBody(502, body)).toThrow(Error);
    expect(await httpFailure(body)).toBeInstanceOf(Error);
  });

  it("refuses a present undefined descriptor in the in-memory durable path", () => {
    expect(() => apiErrorFromBody(502, envelope(undefined, true))).toThrow(Error);
  });
});
