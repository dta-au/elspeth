import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchMailboxSummary, decideApproval, requestApproval, fetchWorkflowInspect, markApprovalSeen } from "./workflow";

describe("workflow API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("uses the authenticated summary and request-scoped inspect paths", async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ governance: "on", roles: [], approvals_to_decide: 0, reviews_to_attest: 0, decisions_unseen: 0 }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ session_id: "s/1", state_id: "t/1", access_log_id: "a", composition_snapshot: {}, yaml: "", attestations: [] }), { status: 200 }));
    vi.stubGlobal("fetch", fetcher);
    await fetchMailboxSummary();
    await fetchWorkflowInspect("s/1", "t/1");
    expect(fetcher.mock.calls[0][0]).toBe("/api/workflow/mailbox/summary");
    expect(fetcher.mock.calls[1][0]).toBe("/api/workflow/inspect/s%2F1/t%2F1");
    expect(fetcher.mock.calls[1][1]).toMatchObject({ cache: "no-store" });
  });

  it("sends bounded plain notes as JSON and stamps a sent decision without a bearer link", async () => {
    const fetcher = vi.fn().mockImplementation(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetcher);
    await requestApproval("s/1", { state_id: "t", approver_identity_id: "bob", note: "   " });
    await decideApproval("a/1", "rejected", "reason");
    await markApprovalSeen("a/1");
    expect(fetcher.mock.calls[0][0]).toBe("/api/sessions/s%2F1/approvals");
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toMatchObject({ note: null, approver_identity_id: "bob" });
    expect(fetcher.mock.calls[1][0]).toBe("/api/approvals/a%2F1/decide");
    expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({ decision: "rejected", note: "reason" });
    expect(fetcher.mock.calls[2][0]).toBe("/api/workflow/mailbox/a%2F1/seen");
  });
});
