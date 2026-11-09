import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchIdentityQuota, fetchMyQuota, revokeIdentityQuota, setIdentityQuota } from "./workflow";

const quota = {
  identity_id: "member", policy_state: "active", can_edit: true,
  tokens_per_day: "9223372036854775807", storage_bytes: "5368709120",
  container_tokens_per_day: null, container_storage_bytes: null,
  tokens_used_today: "0", storage_bytes_used: "0",
  container_tokens_used_today: null, container_storage_bytes_used: "0",
};

describe("quota API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("uses authenticated quota paths and changes one dimension", async () => {
    const fetcher = vi.fn().mockImplementation(async () => new Response(JSON.stringify(quota), { status: 200 }));
    vi.stubGlobal("fetch", fetcher);
    await fetchMyQuota();
    await fetchIdentityQuota("id/member");
    await setIdentityQuota("id/member", "storage", "9223372036854775807");
    await revokeIdentityQuota("id/member");
    expect(fetcher.mock.calls[0][0]).toBe("/api/workflow/quota/me");
    expect(fetcher.mock.calls[0][1]).toMatchObject({ cache: "no-store" });
    expect(fetcher.mock.calls[1][0]).toBe("/api/workflow/quota/identities/id%2Fmember");
    expect(fetcher.mock.calls[2][0]).toBe("/api/workflow/quota/identities/id%2Fmember");
    expect(JSON.parse(fetcher.mock.calls[2][1].body)).toEqual({ dimension: "storage", value: "9223372036854775807" });
    expect(fetcher.mock.calls[3][0]).toBe("/api/workflow/quota/identities/id%2Fmember/revoke");
  });

  it("rejects rounded numeric server quantities instead of publishing them", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async () => new Response(JSON.stringify({ ...quota, tokens_per_day: 2 ** 53 }), { status: 200 })));
    await expect(fetchMyQuota()).rejects.toThrow("Invalid quota quantity");
  });
});
