// Pure checks for the freeform tutorial's durable per-transition provider ledger.
import { describe, expect, it } from "vitest";

import {
  attributeFreshRows,
  classifyTransitionRequest,
  ledgerTotals,
  ledgerViolations,
  renderLedgerMarkdown,
  sessionIdFromTransitionUrl,
  summarizeLlmAuditRows,
  transitionViolations,
  unavailableTransitionEvidence,
  TRANSITION_LEDGER_SCHEMA,
  type LlmAuditRow,
  type TransitionEvidence,
  type TransitionLedger,
  type TransitionLedgerEntry,
} from "./transition-ledger";

const SID = "0f6f0b8e-1f2a-4c3d-9e8f-7a6b5c4d3e2f";
const BASE = `https://elspeth.example.test/api/sessions/${SID}`;

function auditMessage(id: string, envelope: Record<string, unknown>): Record<string, unknown> {
  return { id, role: "audit", content: "", tool_calls: [envelope] };
}

function row(id: string, overrides: Partial<LlmAuditRow> = {}): LlmAuditRow {
  return { id, kind: "llm_call", planner_call_ordinal: null, status: "success", latency_ms: 1_000, phase: null, ...overrides };
}

function evidence(overrides: Partial<TransitionEvidence> = {}): TransitionEvidence {
  return {
    status: "complete", reason: null, provider_calls: 0, planner_calls: 0, planner_runs: 0,
    failed_calls: 0, model_latency_ms: 0, attempt_phases: [], row_ids: [],
    includes_rows_from_unavailable_transition: false, ...overrides,
  };
}

function entry(
  ordinal: number,
  endpoint: TransitionLedgerEntry["endpoint"],
  overrides: Partial<Omit<TransitionLedgerEntry, "response" | "evidence" | "violations">> & {
    response?: Partial<TransitionLedgerEntry["response"]>;
    evidence?: TransitionEvidence;
  } = {},
): TransitionLedgerEntry {
  const { response, evidence: attributed, ...rest } = overrides;
  const gestures = overrides.gestures ?? [{ label: endpoint === "tutorial/run" ? "Run" : "Send tutorial brief", at_ms: ordinal * 10_000 }];
  const base: Omit<TransitionLedgerEntry, "violations"> = {
    ordinal, endpoint, gesture: gestures.at(-1)?.label ?? null, gestures, gesture_count: gestures.length,
    phase_before: endpoint === "tutorial/run" ? "Run" : "Build",
    request: { chat_message_chars: endpoint === "freeform/compose" ? 500 : null },
    response: { status: 200, next_turn_type: null, new_turn_occurrence: false, run_id: null, ...response },
    requested_at_ms: ordinal * 10_000 + 100, responded_at_ms: ordinal * 10_000 + 2_100,
    wall_clock_ms: 2_000, since_previous_ms: null, error: null, evidence: attributed ?? evidence(), ...rest,
  };
  return { ...base, violations: transitionViolations(base) };
}

describe("classifyTransitionRequest", () => {
  it("recognises POST freeform compose and tutorial run, not reads or removed routes", () => {
    expect(classifyTransitionRequest(`${BASE}/messages`, "POST")).toBe("freeform/compose");
    expect(classifyTransitionRequest("https://elspeth.example.test/api/tutorial/run", "POST")).toBe("tutorial/run");
    expect(classifyTransitionRequest(`${BASE}/messages`, "GET")).toBeNull();
    expect(classifyTransitionRequest(`${BASE}/guided/respond`, "POST")).toBeNull();
    expect(classifyTransitionRequest(`${BASE}/state`, "POST")).toBeNull();
  });

  it("extracts a session id only from the compose endpoint", () => {
    expect(sessionIdFromTransitionUrl(`${BASE}/messages?x=1`)).toBe(SID);
    expect(sessionIdFromTransitionUrl(`${BASE}/guided/respond`)).toBeNull();
    expect(sessionIdFromTransitionUrl("https://elspeth.example.test/api/tutorial/run")).toBeNull();
  });
});

describe("durable audit attribution", () => {
  it("reduces audit envelopes and skips conversation rows", () => {
    expect(summarizeLlmAuditRows([
      { id: "m1", role: "user", content: "hi", tool_calls: null },
      auditMessage("a1", { _kind: "llm_call_audit", call: { planner_call_ordinal: 1, status: "success", latency_ms: 4_000 } }),
      auditMessage("a2", { _kind: "planner_attempt_audit", attempt: { planner_call_ordinal: 1, phase: "candidate" } }),
    ])).toEqual([
      row("a1", { planner_call_ordinal: 1, latency_ms: 4_000 }),
      row("a2", { kind: "planner_attempt", planner_call_ordinal: 1, status: null, latency_ms: null, phase: "candidate" }),
    ]);
  });

  it("fails closed on unknown or malformed audit data", () => {
    expect(() => summarizeLlmAuditRows([auditMessage("a", { _kind: "mystery" })])).toThrow(/unknown value mystery/);
    expect(() => summarizeLlmAuditRows({ invalid: true })).toThrow(/must be an array/);
  });

  it("attributes only fresh row IDs to this transition", () => {
    const attributed = attributeFreshRows(new Set(["old"]), [
      row("old", { planner_call_ordinal: 1 }),
      row("new", { planner_call_ordinal: 1, latency_ms: 4_000 }),
      row("attempt", { kind: "planner_attempt", phase: "candidate", status: null, latency_ms: null }),
    ], { afterUnavailable: false });
    expect(attributed).toEqual(evidence({
      provider_calls: 1, planner_calls: 1, planner_runs: 1,
      model_latency_ms: 4_000, attempt_phases: ["candidate"], row_ids: ["new", "attempt"],
    }));
    expect(attributeFreshRows(new Set(), [row("late")], { afterUnavailable: true })
      .includes_rows_from_unavailable_transition).toBe(true);
  });
});

describe("per-transition provider invariant", () => {
  it.each(["freeform_state", "freeform_proposal"])(
    "rejects a %s published without a planner provider call on that same request",
    (structure) => {
      const bypass = entry(1, "freeform/compose", {
        response: { next_turn_type: structure, new_turn_occurrence: true },
        evidence: evidence({ provider_calls: 1, planner_calls: 0 }),
      });
      expect(bypass.violations).toEqual([expect.stringMatching(/zero planner provider calls attributed to it/)]);
    },
  );

  it("accepts a paid structure transition and does not grade the runtime run", () => {
    const paid = entry(1, "freeform/compose", {
      response: { next_turn_type: "freeform_proposal", new_turn_occurrence: true },
      evidence: evidence({ provider_calls: 2, planner_calls: 1, planner_runs: 1 }),
    });
    expect(paid.violations).toEqual([]);
    expect(entry(2, "tutorial/run", { evidence: unavailableTransitionEvidence("runtime audit elsewhere") }).violations).toEqual([]);
  });

  it("rejects unreadable durable evidence for a compose transition", () => {
    expect(entry(1, "freeform/compose", { evidence: unavailableTransitionEvidence("audit read failed") }).violations)
      .toEqual(["transition 1 (freeform/compose, Send tutorial brief): durable provider-call evidence unavailable: audit read failed"]);
  });
});

describe("ledger totals and rendering", () => {
  const walk = () => [
    entry(1, "freeform/compose", {
      response: { next_turn_type: "freeform_state", new_turn_occurrence: true },
      evidence: evidence({ provider_calls: 2, planner_calls: 1, planner_runs: 1, model_latency_ms: 4_000, row_ids: ["a1"] }),
    }),
    entry(2, "tutorial/run", { requested_at_ms: 20_100, responded_at_ms: 50_100, wall_clock_ms: 30_000 }),
  ];

  it("measures first Build gesture through explicit Run and detects unattributed provider rows", () => {
    const totals = ledgerTotals(walk(), [{ label: "Continue (audit story)", at_ms: 60_000 }], [
      row("a1", { planner_call_ordinal: 1 }), row("stray", { planner_call_ordinal: 1 }),
    ]);
    expect(totals).toMatchObject({
      transitions: 2, gestures: 3, gestures_to_run: 2,
      provider_calls: 2, planner_calls: 1, authoring_wall_clock_ms: 2_000,
      wall_clock_to_run_ms: 10_100, run_wall_clock_ms: 30_000,
      unattributed_provider_calls: 1, unattributed_planner_calls: 1,
    });
    expect(ledgerViolations(walk(), totals, { status: "complete", reason: null }, 0))
      .toContain("1 planner provider call(s) were recorded outside every observed transition");
  });

  it("cannot pass with an unreadable final audit or an in-flight request", () => {
    const entries = walk();
    expect(ledgerViolations(entries, ledgerTotals(entries, [], []), { status: "unavailable", reason: "timeout" }, 1))
      .toEqual(["final durable read unavailable: timeout", "1 transition(s) still in flight when the ledger was finalized"]);
  });

  it("renders the freeform build/run sequence with its provider attribution", () => {
    const entries = walk();
    const totals = ledgerTotals(entries, [], []);
    const ledger: TransitionLedger = {
      schema: TRANSITION_LEDGER_SCHEMA, deployment: { bundle: "/assets/index-test.js" }, session_id: SID,
      entries, post_gestures: [], final_read: { status: "complete", reason: null },
      in_flight_at_finalize: 0, totals, violations: [],
    };
    const markdown = renderLedgerMarkdown(ledger);
    expect(markdown).toContain("| 1 | freeform/compose | Send tutorial brief |");
    expect(markdown).toContain("| 2 | tutorial/run | Run |");
    expect(markdown).toContain("2 provider calls · 1 planner calls in 1 planner run(s)");
  });
});
