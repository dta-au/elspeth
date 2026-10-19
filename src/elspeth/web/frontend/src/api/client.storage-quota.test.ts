import { describe, expect, it } from "vitest";
import { parseResponse } from "./client";

describe("storage quota refusal parsing", () => {
  it("retains server usage, cap and ceiling from the 413 envelope", async () => {
    const response = new Response(JSON.stringify({ detail: {
      error_type: "storage_quota_exceeded", detail: "Storage quota reached",
      dimension: "storage", scope: "identity", limit: 2048, cap: 2048, ceiling: 4096, usage: 1536,
    } }), { status: 413 });
    await expect(parseResponse(response)).rejects.toMatchObject({
      status: 413, error_type: "storage_quota_exceeded", storage_quota: { scope: "identity", limit: 2048, cap: 2048, ceiling: 4096, usage: 1536 },
    });
  });

  it("does not classify file-size 413 or malformed quota amounts as identity storage", async () => {
    const fileSize = new Response(JSON.stringify({ detail: "File exceeds limit" }), { status: 413 });
    await expect(parseResponse(fileSize)).rejects.toMatchObject({ storage_quota: undefined });
    const malformed = new Response(JSON.stringify({ detail: {
      error_type: "storage_quota_exceeded", dimension: "storage", cap: 2048, ceiling: null, usage: "1536.5",
    } }), { status: 413 });
    await expect(parseResponse(malformed)).rejects.toMatchObject({ storage_quota: undefined });
  });

  it("retains full-range decimal strings exactly", async () => {
    const response = new Response(JSON.stringify({ detail: {
      error_type: "storage_quota_exceeded", dimension: "storage",
      scope: "identity", limit: "9223372036854775807", cap: "9223372036854775807", ceiling: null, usage: "9007199254740993",
    } }), { status: 413 });
    await expect(parseResponse(response)).rejects.toMatchObject({ storage_quota: {
      scope: "identity", limit: "9223372036854775807", cap: "9223372036854775807", ceiling: null, usage: "9007199254740993",
    } });
  });

  it("binds aggregate usage to the shared limit even when the personal cap is lower", async () => {
    const response = new Response(JSON.stringify({ detail: {
      error_type: "storage_quota_exceeded", dimension: "storage", scope: "container",
      cap: "100", ceiling: "1000", usage: "995", limit: "1000",
    } }), { status: 413 });
    await expect(parseResponse(response)).rejects.toMatchObject({ storage_quota: {
      scope: "container", cap: "100", ceiling: "1000", usage: "995", limit: "1000",
    } });
  });

  it("does not invent a scope for legacy or contradictory refusals", async () => {
    for (const fields of [{}, { scope: "container", limit: "100" }]) {
      const response = new Response(JSON.stringify({ detail: {
        error_type: "storage_quota_exceeded", dimension: "storage", cap: "100", ceiling: "1000", usage: "995", ...fields,
      } }), { status: 413 });
      await expect(parseResponse(response)).rejects.toMatchObject({ storage_quota: undefined });
    }
  });
});
