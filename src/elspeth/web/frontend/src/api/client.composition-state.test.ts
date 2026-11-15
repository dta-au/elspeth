import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSessionStore } from "@/stores/sessionStore";
import { clearAllSessionOperationRetries } from "@/stores/sessionOperationRetry";
import fixture from "../../../../../../tests/fixtures/web/composer/composition_state_validation_errors.json";
import emptyStateResponse from "../../../../../../tests/fixtures/web/composer/empty_composition_state_response.json";

import { fetchCompositionState, fetchStateVersions, importCompositionYaml, revertToVersion } from "./client";
import { decodeCompositionState, decodeCompositionStateVersions } from "./compositionDecoder";

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
  it.each([51, 52])("refetches a milestone after the head advances from 50 to %s", async (latest) => {
    const versions = Array.from({ length: latest }, (_, index) => ({
      ...stateWithErrors(null), version: index + 1, is_milestone: index === 50,
    }));
    let headReads = 0;
    const offsets: number[] = [];
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/state/head")) {
        headReads += 1;
        return new Response(JSON.stringify({ state_id: "state-head", version: headReads === 1 ? 50 : latest }));
      }
      const offset = Number(url.searchParams.get("offset"));
      offsets.push(offset);
      return new Response(JSON.stringify(versions.slice(offset, offset + 50)));
    });
    const rows = await fetchStateVersions("session-1");
    expect(offsets).toEqual([0, latest - 50]);
    expect(rows.find((row) => row.version === 51)?.is_milestone).toBe(true);
    expect(rows[rows.length - 1]?.version).toBe(latest);
  });

  it("marks a bounded history read as refreshing while the head keeps moving", async () => {
    let headVersion = 50;
    const moving = vi.fn();
    vi.mocked(fetch).mockImplementation(async (input) => {
      if (String(input).endsWith("/state/head")) {
        return new Response(JSON.stringify({ state_id: "state-head", version: headVersion++ }));
      }
      return new Response("[]");
    });
    expect(await fetchStateVersions("session-1", moving)).toEqual([]);
    expect(moving).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledTimes(9);
  });

  it("loads the newest 50 rows into the store for a history longer than 50", async () => {
    const versions = Array.from({ length: 55 }, (_, index) => ({
      ...stateWithErrors(null), version: index + 1, is_milestone: index === 53,
    }));
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/state/head")) {
        return new Response(JSON.stringify({ state_id: "state-55", version: 55 }));
      }
    const offset = Number(url.searchParams.get("offset") ?? 0);
      return new Response(JSON.stringify(versions.slice(offset, offset + 50)));
    });
    useSessionStore.setState({ activeSessionId: "session-1", stateVersions: [] });
    await useSessionStore.getState().loadStateVersions();
    expect(useSessionStore.getState().stateVersions.map((row) => row.version)).toEqual(
      Array.from({ length: 50 }, (_, index) => index + 6),
    );
    expect(useSessionStore.getState().stateVersions[48].is_milestone).toBe(true);
  });

  it("requires the milestone boolean only on history rows", () => {
    const state = stateWithErrors(null);
    expect(() => decodeCompositionStateVersions([state])).toThrow(/is_milestone/);
    expect(() => decodeCompositionStateVersions([{ ...state, is_milestone: "true" }])).toThrow(/is_milestone/);
    expect(decodeCompositionStateVersions([{ ...state, is_milestone: false }])[0].is_milestone).toBe(false);
    expect(() => decodeCompositionState({ ...state, is_milestone: true })).toThrow();
  });

  it.each([0, 1, 50])("uses offset zero for a head at version %s", async (version) => {
    vi.mocked(fetch).mockImplementation(async (input) => new Response(
      String(input).endsWith("/state/head")
        ? JSON.stringify({ state_id: version === 0 ? null : "state-1", version })
        : "[]",
    ));
    expect(await fetchStateVersions("session-1")).toEqual([]);
    expect(fetch).toHaveBeenCalledWith(
      "/api/sessions/session-1/state/versions?limit=50&offset=0", expect.anything(),
    );
  });

  it.each([null, {}, { state_id: null, version: -1 }, { state_id: null, version: "55" },
    { state_id: 7, version: 1 }, { state_id: null, version: 1, unexpected: true }])(
    "rejects a malformed head before requesting history", async (head) => {
      vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify(head)));
      await expect(fetchStateVersions("session-1")).rejects.toThrow(/composition_head/);
      expect(fetch).toHaveBeenCalledTimes(1);
    },
  );

  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    localStorage.clear();
    clearAllSessionOperationRetries();
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

  it("preserves the explicitly unavailable graph's scalar identity", async () => {
    const head = { state_id: "44444444-4444-4444-8444-444444444444", version: 7 };
    vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify({ detail: {
      error_type: "composition_state_unavailable", detail: "Saved graph unavailable", ...head,
    } }), { status: 409 }));
    await expect(fetchCompositionState("session-1")).rejects.toMatchObject({
      status: 409, error_type: "composition_state_unavailable", unavailable_state_head: head,
    });
  });

  it.each([
    { state_id: "invalid", version: 7 },
    { state_id: "44444444-4444-4444-8444-444444444444", version: 0 },
    { state_id: "44444444-4444-4444-8444-444444444444", version: 1.5 },
    { state_id: null, version: 7 },
  ])("refuses malformed unavailable graph identity %j", async (head) => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify({ detail: {
      error_type: "composition_state_unavailable", detail: "Saved graph unavailable", ...head,
    } }), { status: 409 }));
    await expect(fetchCompositionState("session-1")).rejects.toMatchObject({ unavailable_state_head: undefined });
  });

  it.each([401, 403, 500])("does not admit graph recovery for status %s", async (status) => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify({ detail: {
      error_type: "composition_state_unavailable", detail: "Unavailable",
      state_id: "44444444-4444-4444-8444-444444444444", version: 7,
    } }), { status }));
    await expect(fetchCompositionState("session-1")).rejects.toMatchObject({ unavailable_state_head: undefined });
  });

  it("decodes full state history records using the same contract", async () => {
    const states = fixture.versions;
    vi.mocked(fetch).mockImplementation(async (input) => new Response(
      String(input).endsWith("/state/head") ? JSON.stringify({ state_id: "state-1", version: 1 }) : JSON.stringify(states),
    ));
    expect(await fetchStateVersions("session-1")).toEqual(states);
  });

  it("rejects legacy strings inside a state history row", async () => {
    vi.mocked(fetch).mockImplementation(async (input) => new Response(
      String(input).endsWith("/state/head")
        ? JSON.stringify({ state_id: "state-1", version: 1 })
        : JSON.stringify([{ ...stateWithErrors(["legacy"]), is_milestone: false }]),
    ));
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
    let revertCount = 0;
    vi.mocked(fetch).mockImplementation(async (input) => {
      if (String(input).endsWith("/state/revert")) {
        revertCount += 1;
        return new Response(revertCount === 1 ? JSON.stringify(current) : JSON.stringify(stateWithErrors(["legacy"])));
      }
      return new Response(JSON.stringify({ events: [] }));
    });
    await useSessionStore.getState().revertToVersion("state-1");
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/state/revert"),
      expect.objectContaining({ method: "POST" }),
    );
    expect(useSessionStore.getState().compositionState).toEqual(current);
    await useSessionStore.getState().revertToVersion("state-2");
    expect(useSessionStore.getState().compositionState).toEqual(current);
    expect(useSessionStore.getState().error).toContain("Failed to revert");
  });
});
