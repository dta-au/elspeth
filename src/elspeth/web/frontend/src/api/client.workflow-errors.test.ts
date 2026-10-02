import { describe, expect, it } from "vitest";
import { parseResponse } from "./client";

describe("workflow error envelopes", () => {
  it("preserves a concurrent decision's current state", async () => {
    const response = new Response(JSON.stringify({ detail: {
      error_type: "approval_already_decided", detail: "Another approver decided", current_state: "rejected",
    } }), { status: 409, headers: { "Content-Type": "application/json" } });
    await expect(parseResponse(response)).rejects.toMatchObject({
      status: 409, error_type: "approval_already_decided", current_state: "rejected",
    });
  });

  it("keeps the admin refusal discriminator", async () => {
    const response = new Response(JSON.stringify({ detail: { refusal: "role_required", detail: "Admin role required" } }), {
      status: 403, headers: { "Content-Type": "application/json" },
    });
    await expect(parseResponse(response)).rejects.toMatchObject({ status: 403, error_type: "role_required" });
  });

  it("preserves named library source refusals without accepting malformed source arrays", async () => {
    const response = new Response(JSON.stringify({ detail: {
      error_type: "library_entry_needs_profile_bound_source", detail: "Use profile-bound input", sources: ["uploaded-csv"],
    } }), { status: 409, headers: { "Content-Type": "application/json" } });
    await expect(parseResponse(response)).rejects.toMatchObject({ sources: ["uploaded-csv"] });

    const malformed = new Response(JSON.stringify({ detail: {
      error_type: "library_entry_needs_profile_bound_source", detail: "Use profile-bound input", sources: ["uploaded-csv", 7],
    } }), { status: 409, headers: { "Content-Type": "application/json" } });
    await expect(parseResponse(malformed)).rejects.toMatchObject({ sources: undefined });
  });
});
