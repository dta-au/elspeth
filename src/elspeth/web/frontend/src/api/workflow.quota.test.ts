import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchIdentityQuota, fetchMyQuota, setIdentityQuota } from "./workflow";

describe("quota API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("uses authenticated quota paths and changes one dimension", async () => {
    const fetcher = vi.fn().mockImplementation(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetcher);
    await fetchMyQuota();
    await fetchIdentityQuota("id/member");
    await setIdentityQuota("id/member", "storage", 2048);
    expect(fetcher.mock.calls[0][0]).toBe("/api/workflow/quota/me");
    expect(fetcher.mock.calls[0][1]).toMatchObject({ cache: "no-store" });
    expect(fetcher.mock.calls[1][0]).toBe("/api/workflow/quota/identities/id%2Fmember");
    expect(fetcher.mock.calls[2][0]).toBe("/api/workflow/quota/identities/id%2Fmember");
    expect(JSON.parse(fetcher.mock.calls[2][1].body)).toEqual({ dimension: "storage", value: 2048 });
  });
});
