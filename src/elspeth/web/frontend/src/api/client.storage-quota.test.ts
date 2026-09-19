import { describe, expect, it } from "vitest";
import { parseResponse } from "./client";

describe("storage quota refusal parsing", () => {
  it("retains server usage, cap and ceiling from the 413 envelope", async () => {
    const response = new Response(JSON.stringify({ detail: {
      error_type: "storage_quota_exceeded", detail: "Storage quota reached",
      dimension: "storage", cap: 2048, ceiling: 4096, usage: 1536,
    } }), { status: 413 });
    await expect(parseResponse(response)).rejects.toMatchObject({
      status: 413, error_type: "storage_quota_exceeded", storage_quota: { cap: 2048, ceiling: 4096, usage: 1536 },
    });
  });

  it("does not classify file-size 413 or malformed quota amounts as identity storage", async () => {
    const fileSize = new Response(JSON.stringify({ detail: "File exceeds limit" }), { status: 413 });
    await expect(parseResponse(fileSize)).rejects.toMatchObject({ storage_quota: undefined });
    const malformed = new Response(JSON.stringify({ detail: {
      error_type: "storage_quota_exceeded", dimension: "storage", cap: 2048, ceiling: null, usage: "1536",
    } }), { status: 413 });
    await expect(parseResponse(malformed)).rejects.toMatchObject({ storage_quota: undefined });
  });
});
