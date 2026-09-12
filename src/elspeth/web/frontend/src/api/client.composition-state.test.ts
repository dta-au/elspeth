import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSessionStore } from "@/stores/sessionStore";
import { clearAllGuidedRetries } from "@/stores/guidedOperationRetry";
import fixture from "../../../../../../tests/fixtures/web/composer/composition_state_validation_errors.json";
import emptyStateResponse from "../../../../../../tests/fixtures/web/composer/empty_composition_state_response.json";

import { fetchCompositionState, fetchStateVersions, importCompositionYaml, revertToVersion } from "./client";
import { decodeCompositionState } from "./guidedDecoder";

function stateWithErrors(validation_errors: unknown) {
  return {
    id: "state-1", session_id: "session-1", version: 1,
    sources: {}, nodes: [], edges: [], outputs: [],
    metadata: { name: null, description: null }, is_valid: false,
    validation_errors, validation_warnings: null, validation_suggestions: null,
    derived_from_state_id: null, created_at: "2026-09-13T00:00:00Z",
    composer_meta: null, plugin_policy_findings: [],
  };
}

const coded = { message: "Readable diagnosis", error_code: "schema_contract_violation", component: "step" };

describe("composition state HTTP admission", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    localStorage.clear();
    clearAllGuidedRetries();
  });

  const operations = [
    ["fetch", () => fetchCompositionState("session-1")],
    ["revert", () => revertToVersion("session-1", "state-1", "operation-1")],
    ["import", () => importCompositionYaml("session-1", "sources: {}")],
  ] as const;

  for (const [name, operation] of operations) {
    it.each(Object.entries(fixture.states))(`${name} decodes producer-backed state %s`, async (_case, state) => {
      vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify(state)));
      expect(await operation()).toEqual(state);
    });
    it.each([null, [], [coded], [{ message: "Message only", error_code: null, component: null }]].map((errors) => ({ errors })))(
      `${name} preserves the structured error collection $errors`, async ({ errors }) => {
        vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify(stateWithErrors(errors))));
        expect((await operation())?.validation_errors).toEqual(errors);
      },
    );

    it.each([
      ["legacy string"], [{ message: "missing fields" }],
      [{ ...coded, extra: "forbidden" }], [{ ...coded, component: 4 }],
      [{ ...coded, error_code: false }], [{ ...coded, message: null }],
      coded,
    ].map((errors) => ({ errors })))(`${name} rejects malformed errors $errors`, async ({ errors }) => {
      vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify(stateWithErrors(errors))));
      await expect(operation()).rejects.toThrow(/validation_errors/);
    });
  }

  it("keeps warnings and suggestions as their distinct record types", () => {
    const state = {
      ...stateWithErrors([coded]),
      validation_warnings: [{ message: "warning", severity: "warning", component: "step" }],
      validation_suggestions: [{ message: "suggestion", severity: "suggestion", component: "step", error_code: null }],
    };
    expect(decodeCompositionState(state)).toEqual(state);
  });

  it("returns null for an absent state without decoding an error body", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response("not a state", { status: 404 }));
    expect(await fetchCompositionState("session-1")).toBeNull();
  });

  it("decodes full state history records using the same contract", async () => {
    const states = fixture.versions;
    vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify(states)));
    expect(await fetchStateVersions("session-1")).toEqual(states);
  });

  it("rejects legacy strings inside a state history row", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify([stateWithErrors(["legacy"])])));
    await expect(fetchStateVersions("session-1")).rejects.toThrow(/composition_states\[0\].validation_errors/);
  });

  it("preserves the actual empty-session HTTP producer's successful null response", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify(emptyStateResponse.body), {
      status: emptyStateResponse.status,
    }));
    expect(await fetchCompositionState("session-1")).toBeNull();
  });

  it.each([{}, [], false, "not a state"])("rejects malformed non-null fetched state %j", async (body) => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify(body)));
    await expect(fetchCompositionState("session-1")).rejects.toThrow();
  });

  it("rejects a missing successful response body", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response(""));
    await expect(fetchCompositionState("session-1")).rejects.toThrow();
  });

  it("does not widen the required-state decoder for other endpoints", async () => {
    expect(() => decodeCompositionState(null)).toThrow(/composition_state/);
    for (const operation of [operations[1][1], operations[2][1]]) {
      vi.mocked(fetch).mockResolvedValueOnce(new Response("null"));
      await expect(operation()).rejects.toThrow(/composition_state/);
    }
  });

  it("stores a decoded revert response and refuses a malformed replacement", async () => {
    const current = decodeCompositionState(fixture.states.coded);
    useSessionStore.setState({ activeSessionId: "00000000-0000-4000-8000-000000000001", compositionState: null, error: null });
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response(JSON.stringify(current)))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "No guided state" }), { status: 400 }))
      .mockImplementation(async () => new Response(JSON.stringify({ events: [] })));
    await useSessionStore.getState().revertToVersion("state-1");
    expect(useSessionStore.getState().compositionState).toEqual(current);
    vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify(stateWithErrors(["legacy"]))));
    await useSessionStore.getState().revertToVersion("state-2");
    expect(useSessionStore.getState().compositionState).toEqual(current);
    expect(useSessionStore.getState().error).toContain("Failed to revert");
  });
});
