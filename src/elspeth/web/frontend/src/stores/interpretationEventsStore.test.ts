// ============================================================================
// Tests for interpretationEventsStore (Phase 5b Task 3).
//
// Strategy: mock the API client module (consumer-side test — we're
// exercising the store wiring, not the network).  Pattern matches
// sessionStore.test.ts.
// ============================================================================

import { describe, it, expect, beforeEach, vi } from "vitest";
import {
  selectApprovedInterpretations,
  useInterpretationEventsStore,
} from "./interpretationEventsStore";
import { resetStore } from "@/test/store-helpers";
import type {
  InterpretationEvent,
  InterpretationOptOutResponse,
  InterpretationResolveResponse,
} from "@/types/interpretation";
import type { CompositionState } from "@/types/api";
import { compositionStateAuthorityFields } from "@/test/composerFixtures";

// Module-level mock — the store imports `* as api from "@/api/client"` and
// we replace the four methods with vi.fn()s the tests configure per-case.
vi.mock("@/api/client", () => ({
  listInterpretationEvents: vi.fn(),
  resolveInterpretation: vi.fn(),
  optOutOfInterpretations: vi.fn(),
  getInterpretationOptOutSummary: vi.fn(),
}));

// Re-import the mocked module so the tests can configure return values.
import * as api from "@/api/client";

// ── Fixtures ────────────────────────────────────────────────────────────────

function makePendingEvent(overrides: Partial<InterpretationEvent> = {}): InterpretationEvent {
  return {
    id: "evt-1",
    session_id: "sess-1",
    composition_state_id: "state-1",
    affected_node_id: "node-1",
    tool_call_id: "tool-1",
    user_term: "cool",
    kind: "vague_term",
    llm_draft: "interesting and engaging",
    accepted_value: null,
    choice: "pending",
    created_at: "2026-05-18T00:00:00Z",
    resolved_at: null,
    actor: "user:owner:u-1",
    interpretation_source: "user_approved",
    model_identifier: "anthropic/claude-opus-4-7",
    model_version: "20260518",
    provider: "anthropic",
    composer_skill_hash: "deadbeef",
    arguments_hash: null,
    hash_domain_version: null,
    runtime_model_identifier_at_resolve: null,
    runtime_model_version_at_resolve: null,
    resolved_prompt_template_hash: null,
    ...overrides,
  };
}

function makeCompositionState(version = 2): CompositionState {
  return {
    id: `state-${version}`,
    ...compositionStateAuthorityFields,
    version,
    sources: {},
    nodes: [],
    edges: [],
    outputs: [],
    metadata: { name: null, description: null },
  };
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe("interpretationEventsStore", () => {
  beforeEach(() => {
    resetStore(useInterpretationEventsStore);
    vi.mocked(api.listInterpretationEvents).mockReset();
    vi.mocked(api.resolveInterpretation).mockReset();
    vi.mocked(api.optOutOfInterpretations).mockReset();
  });

  describe("initial state", () => {
    it("starts with empty per-session maps", () => {
      const state = useInterpretationEventsStore.getState();
      expect(state.pendingBySession).toEqual({});
      expect(state.resolvedCountBySession).toEqual({});
      expect(state.resolvedBySession).toEqual({});
      expect(state.optedOutBySession).toEqual({});
    });
  });

  describe("selectApprovedInterpretations (elspeth-3a8a843c47)", () => {
    it("admits exactly the two operator-approval choices", () => {
      const accepted = makePendingEvent({
        id: "evt-accepted",
        choice: "accepted_as_drafted",
        resolved_at: "2026-05-18T00:01:00Z",
      });
      const amended = makePendingEvent({
        id: "evt-amended",
        choice: "amended",
        accepted_value: "captivating",
        resolved_at: "2026-05-18T00:02:00Z",
      });
      // The dangerous shape: a surface-specific auto_interpreted_opt_out
      // row is CHECK-required to carry a non-null user_term and
      // tool_call_id — field presence must never classify it as approved.
      const autoBaked = makePendingEvent({
        id: "evt-auto-baked",
        choice: "opted_out",
        interpretation_source: "auto_interpreted_opt_out",
        accepted_value: "interesting and engaging",
        resolved_at: "2026-05-18T00:03:00Z",
        actor: "composer-llm",
      });
      const pending = makePendingEvent({ id: "evt-pending" });
      const abandoned = makePendingEvent({
        id: "evt-abandoned",
        choice: "abandoned",
        resolved_at: "2026-05-18T00:04:00Z",
      });

      const approved = selectApprovedInterpretations([
        accepted,
        amended,
        autoBaked,
        pending,
        abandoned,
      ]);

      expect(approved.map((event) => event.id)).toEqual([
        "evt-accepted",
        "evt-amended",
      ]);
    });
  });

  describe("refreshPending", () => {
    it("calls the API with status='pending' and fills pendingBySession", async () => {
      const evt = makePendingEvent();
      vi.mocked(api.listInterpretationEvents).mockResolvedValue([evt]);

      await useInterpretationEventsStore.getState().refreshPending("sess-1");

      expect(api.listInterpretationEvents).toHaveBeenCalledWith("sess-1", "pending");
      const pending = useInterpretationEventsStore.getState().pendingBySession["sess-1"];
      expect(pending).toEqual({ "evt-1": evt });
    });

    it("does not touch resolvedCountBySession on refreshPending", async () => {
      vi.mocked(api.listInterpretationEvents).mockResolvedValue([]);

      await useInterpretationEventsStore.getState().refreshPending("sess-1");

      const counts = useInterpretationEventsStore.getState().resolvedCountBySession["sess-1"];
      expect(counts).toBeUndefined();
    });

    it("does not mutate the store on API error", async () => {
      vi.mocked(api.listInterpretationEvents).mockRejectedValue(
        new Error("network down"),
      );

      await expect(
        useInterpretationEventsStore.getState().refreshPending("sess-1"),
      ).rejects.toThrow("network down");

      expect(useInterpretationEventsStore.getState().pendingBySession).toEqual({});
    });
  });

  describe("refreshAll", () => {
    it("calls the API with status='all' and partitions into pending + resolved counts", async () => {
      const pendingEvt = makePendingEvent({ id: "evt-pending" });
      const acceptedEvt = makePendingEvent({
        id: "evt-accepted",
        choice: "accepted_as_drafted",
        accepted_value: "ok",
        resolved_at: "2026-05-18T00:01:00Z",
      });
      const amendedEvt1 = makePendingEvent({
        id: "evt-amended-1",
        choice: "amended",
        accepted_value: "thoughtful",
        resolved_at: "2026-05-18T00:02:00Z",
      });
      const amendedEvt2 = makePendingEvent({
        id: "evt-amended-2",
        choice: "amended",
        accepted_value: "witty",
        resolved_at: "2026-05-18T00:03:00Z",
      });

      vi.mocked(api.listInterpretationEvents).mockResolvedValue([
        pendingEvt,
        acceptedEvt,
        amendedEvt1,
        amendedEvt2,
      ]);

      await useInterpretationEventsStore.getState().refreshAll("sess-1");

      expect(api.listInterpretationEvents).toHaveBeenCalledWith("sess-1", "all");
      const state = useInterpretationEventsStore.getState();
      expect(state.pendingBySession["sess-1"]).toEqual({ "evt-pending": pendingEvt });
      expect(state.resolvedCountBySession["sess-1"]).toEqual({
        accepted_as_drafted: 1,
        amended: 2,
        opted_out: 0,
      });
      // resolvedBySession captures the same non-pending events in API order
      // — Phase 6B NarrativeResults overlay relies on this slice.
      expect(state.resolvedBySession["sess-1"]).toEqual([
        acceptedEvt,
        amendedEvt1,
        amendedEvt2,
      ]);
    });

    it("populates resolvedBySession for the opt-out history case", async () => {
      const optedOutEvt = makePendingEvent({
        id: "evt-optout",
        choice: "opted_out",
        resolved_at: "2026-05-18T00:04:00Z",
        interpretation_source: "auto_interpreted_opt_out",
        kind: null,
      });
      vi.mocked(api.listInterpretationEvents).mockResolvedValue([optedOutEvt]);

      await useInterpretationEventsStore.getState().refreshAll("sess-1");

      const state = useInterpretationEventsStore.getState();
      expect(state.resolvedBySession["sess-1"]).toEqual([optedOutEvt]);
    });

    it("rehydrates opt-out from history and suppresses stale pending entries", async () => {
      const stalePendingEvt = makePendingEvent({ id: "evt-stale-pending" });
      const optedOutEvt = makePendingEvent({
        id: "evt-optout",
        choice: "opted_out",
        resolved_at: "2026-05-18T00:04:00Z",
        interpretation_source: "auto_interpreted_opt_out",
        kind: null,
      });
      vi.mocked(api.listInterpretationEvents).mockResolvedValue([
        stalePendingEvt,
        optedOutEvt,
      ]);

      await useInterpretationEventsStore.getState().refreshAll("sess-1");

      const state = useInterpretationEventsStore.getState();
      expect(state.optedOutBySession["sess-1"]).toBe(true);
      expect(state.pendingBySession["sess-1"]).toEqual({});
      expect(state.resolvedCountBySession["sess-1"]).toEqual({
        accepted_as_drafted: 0,
        amended: 0,
        opted_out: 1,
      });
    });

    it("namespaces by session — multiple sessions keep distinct projections", async () => {
      vi.mocked(api.listInterpretationEvents)
        .mockResolvedValueOnce([makePendingEvent({ id: "a", session_id: "sess-A" })])
        .mockResolvedValueOnce([makePendingEvent({ id: "b", session_id: "sess-B" })]);

      await useInterpretationEventsStore.getState().refreshAll("sess-A");
      await useInterpretationEventsStore.getState().refreshAll("sess-B");

      const state = useInterpretationEventsStore.getState();
      expect(Object.keys(state.pendingBySession["sess-A"])).toEqual(["a"]);
      expect(Object.keys(state.pendingBySession["sess-B"])).toEqual(["b"]);
    });
  });

  describe("resolveEvent", () => {
    it("calls the API, removes the event from pending, increments the matching counter, returns new_state", async () => {
      // Seed the store with a pending event.
      const evt = makePendingEvent();
      vi.mocked(api.listInterpretationEvents).mockResolvedValue([evt]);
      await useInterpretationEventsStore.getState().refreshPending("sess-1");

      // Mock the resolve response.
      const newState = makeCompositionState();
      const resolveResponse: InterpretationResolveResponse = {
        event: makePendingEvent({
          choice: "accepted_as_drafted",
          accepted_value: "interesting and engaging",
          resolved_at: "2026-05-18T00:01:00Z",
        }),
        new_state: newState,
      };
      vi.mocked(api.resolveInterpretation).mockResolvedValue(resolveResponse);

      const result = await useInterpretationEventsStore
        .getState()
        .resolveEvent("sess-1", "evt-1", { choice: "accepted_as_drafted" });

      expect(api.resolveInterpretation).toHaveBeenCalledWith(
        "sess-1",
        "evt-1",
        { choice: "accepted_as_drafted" },
      );
      expect(result).toEqual({ new_state: newState });

      const state = useInterpretationEventsStore.getState();
      expect(state.pendingBySession["sess-1"]).toEqual({});
      expect(state.resolvedCountBySession["sess-1"]).toEqual({
        accepted_as_drafted: 1,
        amended: 0,
        opted_out: 0,
      });
    });

    it("does not mutate the store on API error (atomicity)", async () => {
      const evt = makePendingEvent();
      vi.mocked(api.listInterpretationEvents).mockResolvedValue([evt]);
      await useInterpretationEventsStore.getState().refreshPending("sess-1");

      vi.mocked(api.resolveInterpretation).mockRejectedValue(
        Object.assign(new Error("conflict"), { status: 409 }),
      );

      await expect(
        useInterpretationEventsStore
          .getState()
          .resolveEvent("sess-1", "evt-1", { choice: "accepted_as_drafted" }),
      ).rejects.toThrow("conflict");

      const state = useInterpretationEventsStore.getState();
      // Pending unchanged — the wire write didn't happen.
      expect(state.pendingBySession["sess-1"]).toEqual({ "evt-1": evt });
      // No counter bump.
      expect(state.resolvedCountBySession["sess-1"]).toBeUndefined();
    });

    it("appends the resolved event to resolvedBySession so the overlay can render without a refreshAll round-trip", async () => {
      // Seed with a pending event via refreshPending — note this does NOT
      // touch resolvedBySession (only refreshAll seeds it). The resolved
      // slice starts empty for this session.
      const evt = makePendingEvent();
      vi.mocked(api.listInterpretationEvents).mockResolvedValue([evt]);
      await useInterpretationEventsStore.getState().refreshPending("sess-1");
      expect(
        useInterpretationEventsStore.getState().resolvedBySession["sess-1"],
      ).toBeUndefined();

      // Resolve produces the new (non-pending) event row.
      const resolvedRow = makePendingEvent({
        choice: "accepted_as_drafted",
        accepted_value: "interesting and engaging",
        resolved_at: "2026-05-18T00:01:00Z",
      });
      vi.mocked(api.resolveInterpretation).mockResolvedValue({
        event: resolvedRow,
        new_state: makeCompositionState(),
      });

      await useInterpretationEventsStore
        .getState()
        .resolveEvent("sess-1", "evt-1", { choice: "accepted_as_drafted" });

      const state = useInterpretationEventsStore.getState();
      expect(state.resolvedBySession["sess-1"]).toEqual([resolvedRow]);
    });

    it("preserves prior resolved events when a new resolve lands (append, not replace)", async () => {
      // Seed the resolved slice via refreshAll.
      const prior = makePendingEvent({
        id: "evt-prior",
        choice: "amended",
        accepted_value: "first",
        resolved_at: "2026-05-18T00:00:30Z",
      });
      vi.mocked(api.listInterpretationEvents).mockResolvedValue([
        prior,
        makePendingEvent({ id: "evt-active" }),
      ]);
      await useInterpretationEventsStore.getState().refreshAll("sess-1");
      expect(
        useInterpretationEventsStore.getState().resolvedBySession["sess-1"],
      ).toEqual([prior]);

      // Resolve the still-pending event.
      const fresh = makePendingEvent({
        id: "evt-active",
        choice: "accepted_as_drafted",
        accepted_value: "second",
        resolved_at: "2026-05-18T00:01:00Z",
      });
      vi.mocked(api.resolveInterpretation).mockResolvedValue({
        event: fresh,
        new_state: makeCompositionState(),
      });

      await useInterpretationEventsStore
        .getState()
        .resolveEvent("sess-1", "evt-active", { choice: "accepted_as_drafted" });

      const state = useInterpretationEventsStore.getState();
      expect(state.resolvedBySession["sess-1"]).toEqual([prior, fresh]);
    });

    // ── elspeth-292505e1c3: refreshAll snapshot vs resolveEvent append ──────
    //
    // refreshAll is fired void from compose-completion paths while resolves
    // fire from click handlers; the backend GET runs unlocked on a separate
    // thread-pool worker, so BOTH response orderings are schedulable. The
    // resolved slice must be write-monotonic under either interleaving:
    // resolution is terminal server-side (audit rows are never deleted), so
    // a row present locally but missing from a snapshot is NEWER than the
    // snapshot's GET, not gone.

    it("a stale refreshAll snapshot cannot erase a resolve that landed while its GET was in flight, nor resurrect its pending card (elspeth-292505e1c3 interleaving B)", async () => {
      // Seed the pending card the operator is looking at.
      const pendingX = makePendingEvent({ id: "evt-x" });
      vi.mocked(api.listInterpretationEvents).mockResolvedValueOnce([pendingX]);
      await useInterpretationEventsStore.getState().refreshPending("sess-1");

      // A refreshAll whose GET is serviced PRE-commit: hold its response.
      let releaseStaleGet!: (rows: InterpretationEvent[]) => void;
      vi.mocked(api.listInterpretationEvents).mockImplementationOnce(
        () =>
          new Promise<InterpretationEvent[]>((resolve) => {
            releaseStaleGet = resolve;
          }),
      );
      const staleRefresh = useInterpretationEventsStore
        .getState()
        .refreshAll("sess-1");

      // The operator resolves the card; the confirmation is now on screen.
      const resolvedX = makePendingEvent({
        id: "evt-x",
        choice: "accepted_as_drafted",
        accepted_value: "interesting and engaging",
        resolved_at: "2026-05-18T00:01:00Z",
      });
      vi.mocked(api.resolveInterpretation).mockResolvedValue({
        event: resolvedX,
        new_state: makeCompositionState(),
      });
      await useInterpretationEventsStore
        .getState()
        .resolveEvent("sess-1", "evt-x", { choice: "accepted_as_drafted" });

      // The stale snapshot (taken before the commit) now lands.
      releaseStaleGet([pendingX]);
      await staleRefresh;

      const state = useInterpretationEventsStore.getState();
      // The approval survives, exactly once.
      expect(
        state.resolvedBySession["sess-1"].map((event) => event.id),
      ).toEqual(["evt-x"]);
      // The pending card does not resurrect — re-clicking it would 409
      // (InterpretationEventAlreadyResolvedError) against the backend.
      expect(state.pendingBySession["sess-1"]).toEqual({});
      expect(state.resolvedCountBySession["sess-1"]).toEqual({
        accepted_as_drafted: 1,
        amended: 0,
        opted_out: 0,
      });
    });

    it("does not duplicate a row that a post-commit refreshAll snapshot already delivered (elspeth-292505e1c3 interleaving A)", async () => {
      // A refreshAll GET serviced in the post-commit window already carries
      // the resolved row, and its set() lands before the POST response's.
      const resolvedX = makePendingEvent({
        id: "evt-x",
        choice: "accepted_as_drafted",
        accepted_value: "interesting and engaging",
        resolved_at: "2026-05-18T00:01:00Z",
      });
      vi.mocked(api.listInterpretationEvents).mockResolvedValueOnce([
        resolvedX,
      ]);
      await useInterpretationEventsStore.getState().refreshAll("sess-1");

      vi.mocked(api.resolveInterpretation).mockResolvedValue({
        event: resolvedX,
        new_state: makeCompositionState(),
      });
      await useInterpretationEventsStore
        .getState()
        .resolveEvent("sess-1", "evt-x", { choice: "accepted_as_drafted" });

      const state = useInterpretationEventsStore.getState();
      // Exactly one copy — a duplicate doubles the "Got it" bubble and
      // collides React keys (tail confirmations key on event.id).
      expect(
        state.resolvedBySession["sess-1"].map((event) => event.id),
      ).toEqual(["evt-x"]);
      // And exactly one count — the snapshot pass already counted it.
      expect(state.resolvedCountBySession["sess-1"]).toEqual({
        accepted_as_drafted: 1,
        amended: 0,
        opted_out: 0,
      });
    });

    it("increments the 'amended' counter on amended resolution", async () => {
      vi.mocked(api.listInterpretationEvents).mockResolvedValue([makePendingEvent()]);
      await useInterpretationEventsStore.getState().refreshPending("sess-1");

      vi.mocked(api.resolveInterpretation).mockResolvedValue({
        event: makePendingEvent({
          choice: "amended",
          accepted_value: "edited",
          resolved_at: "2026-05-18T00:01:00Z",
        }),
        new_state: makeCompositionState(),
      });

      await useInterpretationEventsStore
        .getState()
        .resolveEvent("sess-1", "evt-1", {
          choice: "amended",
          amended_value: "edited",
        });

      const counts =
        useInterpretationEventsStore.getState().resolvedCountBySession["sess-1"];
      expect(counts).toEqual({
        accepted_as_drafted: 0,
        amended: 1,
        opted_out: 0,
      });
    });
  });

  describe("optOut", () => {
    it("calls the API, flips opt-out flag, clears pending, bumps opted_out counter", async () => {
      // Seed with two pending events.
      const e1 = makePendingEvent({ id: "evt-a" });
      const e2 = makePendingEvent({ id: "evt-b" });
      vi.mocked(api.listInterpretationEvents).mockResolvedValue([e1, e2]);
      await useInterpretationEventsStore.getState().refreshPending("sess-1");
      expect(
        Object.keys(
          useInterpretationEventsStore.getState().pendingBySession["sess-1"],
        ),
      ).toHaveLength(2);

      const optOutResponse: InterpretationOptOutResponse = {
        session_id: "sess-1",
        interpretation_review_disabled: true,
        opted_out_at: "2026-05-18T00:05:00Z",
      };
      vi.mocked(api.optOutOfInterpretations).mockResolvedValue(optOutResponse);

      await useInterpretationEventsStore.getState().optOut("sess-1");

      expect(api.optOutOfInterpretations).toHaveBeenCalledWith("sess-1");
      const state = useInterpretationEventsStore.getState();
      expect(state.optedOutBySession["sess-1"]).toBe(true);
      expect(state.pendingBySession["sess-1"]).toEqual({});
      expect(state.resolvedCountBySession["sess-1"]).toEqual({
        accepted_as_drafted: 0,
        amended: 0,
        opted_out: 1,
      });
    });

    it("does not mutate the store on API error (atomicity)", async () => {
      const evt = makePendingEvent();
      vi.mocked(api.listInterpretationEvents).mockResolvedValue([evt]);
      await useInterpretationEventsStore.getState().refreshPending("sess-1");

      vi.mocked(api.optOutOfInterpretations).mockRejectedValue(
        new Error("backend down"),
      );

      await expect(
        useInterpretationEventsStore.getState().optOut("sess-1"),
      ).rejects.toThrow("backend down");

      const state = useInterpretationEventsStore.getState();
      expect(state.optedOutBySession["sess-1"]).toBeUndefined();
      expect(state.pendingBySession["sess-1"]).toEqual({ "evt-1": evt });
    });
  });

  describe("addPendingEvent", () => {
    it("adds an event to pendingBySession without an API call", () => {
      const evt = makePendingEvent({ id: "evt-inline" });

      useInterpretationEventsStore.getState().addPendingEvent("sess-1", evt);

      const pending =
        useInterpretationEventsStore.getState().pendingBySession["sess-1"];
      expect(pending).toEqual({ "evt-inline": evt });
      expect(api.listInterpretationEvents).not.toHaveBeenCalled();
    });

    it("is idempotent — re-adding the same id overwrites the entry", () => {
      const evt1 = makePendingEvent({ id: "evt-1", user_term: "v1" });
      const evt2 = makePendingEvent({ id: "evt-1", user_term: "v2" });

      useInterpretationEventsStore.getState().addPendingEvent("sess-1", evt1);
      useInterpretationEventsStore.getState().addPendingEvent("sess-1", evt2);

      const pending =
        useInterpretationEventsStore.getState().pendingBySession["sess-1"];
      expect(pending["evt-1"].user_term).toBe("v2");
    });

    it("preserves prior pending events for the same session when adding a new one", () => {
      const evtA = makePendingEvent({ id: "evt-A" });
      const evtB = makePendingEvent({ id: "evt-B" });

      useInterpretationEventsStore.getState().addPendingEvent("sess-1", evtA);
      useInterpretationEventsStore.getState().addPendingEvent("sess-1", evtB);

      const pending =
        useInterpretationEventsStore.getState().pendingBySession["sess-1"];
      expect(Object.keys(pending).sort()).toEqual(["evt-A", "evt-B"]);
    });
  });

  describe("cross-session isolation", () => {
    it("pending events for a non-selected session are not reset on subsequent session activity", async () => {
      // Seed session A.
      vi.mocked(api.listInterpretationEvents).mockResolvedValueOnce([
        makePendingEvent({ id: "evt-A", session_id: "sess-A" }),
      ]);
      await useInterpretationEventsStore.getState().refreshAll("sess-A");

      // Refresh session B with no events.
      vi.mocked(api.listInterpretationEvents).mockResolvedValueOnce([]);
      await useInterpretationEventsStore.getState().refreshAll("sess-B");

      // Session A's pending event survives.
      const state = useInterpretationEventsStore.getState();
      expect(state.pendingBySession["sess-A"]["evt-A"]).toBeDefined();
      expect(state.pendingBySession["sess-B"]).toEqual({});
    });
  });
});
