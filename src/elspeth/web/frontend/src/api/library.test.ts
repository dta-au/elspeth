import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { curateLibraryEntry, fetchLibrary, forkLibraryEntry, publishLibraryEntry } from "./library";

vi.mock("./client", () => ({ authHeaders: vi.fn(() => ({ Authorization: "Bearer test" })), parseResponse: vi.fn(async () => ({})) }));

describe("library API", () => {
  beforeEach(() => { vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 }))); });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("uses authenticated, uncached accepted and queue reads", async () => {
    await fetchLibrary("accepted");
    await fetchLibrary("queue");
    expect(fetch).toHaveBeenNthCalledWith(1, "/api/library?view=accepted", { headers: { Authorization: "Bearer test" }, cache: "no-store" });
    expect(fetch).toHaveBeenNthCalledWith(2, "/api/library?view=queue", { headers: { Authorization: "Bearer test" }, cache: "no-store" });
  });

  it("sends publish, curation and fork to the matching state routes", async () => {
    await publishLibraryEntry("session/id", "  Invoice triage  ");
    await curateLibraryEntry("entry/id", "reject", "  Needs work  ");
    await forkLibraryEntry("entry/id");
    expect(fetch).toHaveBeenNthCalledWith(1, "/api/sessions/session%2Fid/library/publish", expect.objectContaining({ method: "POST", body: JSON.stringify({ title: "Invoice triage" }) }));
    expect(fetch).toHaveBeenNthCalledWith(2, "/api/library/entry%2Fid/reject", expect.objectContaining({ method: "POST", body: JSON.stringify({ note: "Needs work" }) }));
    expect(fetch).toHaveBeenNthCalledWith(3, "/api/library/entry%2Fid/fork", expect.objectContaining({ method: "POST" }));
  });
});
