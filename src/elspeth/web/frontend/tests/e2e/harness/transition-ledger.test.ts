// Unit coverage for the pure per-transition ledger (transition-ledger.ts).
//
// The recorder (helpers/transition-ledger-recorder.ts) is Playwright glue and
// is exercised only by the staging spec; everything it feeds through these
// functions — request classification, audit-row reduction, row-identity
// attribution, the per-transition invariant, totals and rendering — is pinned
// here without a browser.

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

function llmCall(
  id: string,
  call: Partial<{ planner_call_ordinal: number | null; status: string; latency_ms: number | null }> = {},
): Record<string, unknown> {
  return auditMessage(id, {
    _kind: "llm_call_audit",
    call: { planner_call_ordinal: null, status: "success", latency_ms: 1_000, ...call },
  });
}

function plannerAttempt(id: string, ordinal: number, phase: string): Record<string, unknown> {
  return auditMessage(id, {
    _kind: "planner_attempt_audit",
    attempt: { planner_call_ordinal: ordinal, phase, ordinal: 1 },
  });
}

function row(id: string, overrides: Partial<LlmAuditRow> = {}): LlmAuditRow {
  return {
    id,
    kind: "llm_call",
    planner_call_ordinal: null,
    status: "success",
    latency_ms: 1_000,
    phase: null,
    ...overrides,
  };
}

function completeEvidence(overrides: Partial<TransitionEvidence> = {}): TransitionEvidence {
  return {
    status: "complete",
    reason: null,
    provider_calls: 0,
    planner_calls: 0,
    planner_runs: 0,
    failed_calls: 0,
    model_latency_ms: 0,
    attempt_phases: [],
    row_ids: [],
    includes_rows_from_unavailable_transition: false,
    ...overrides,
  };
}

type EntryOverrides = Partial<Omit<TransitionLedgerEntry, "response" | "evidence">> & {
  response?: Partial<TransitionLedgerEntry["response"]>;
  evidence?: TransitionEvidence;
};

function entry(ordinal: number, endpoint: TransitionLedgerEntry["endpoint"], overrides: EntryOverrides = {}): TransitionLedgerEntry {
  const gestures = overrides.gestures ?? [{ label: "Continue", at_ms: ordinal * 10_000 }];
  const { response, evidence, ...rest } = overrides;
  const base: Omit<TransitionLedgerEntry, "violations"> = {
    ordinal,
    endpoint,
    gesture: gestures.at(-1)?.label ?? null,
    gestures,
    gesture_count: gestures.length,
    phase_before: null,
    request: { start_profile: null, respond_shape: null, control_signal: null, chat_message_chars: null },
    response: {
      status: 200,
      step_after: null,
      next_turn_type: null,
      new_turn_occurrence: false,
      terminal: null,
      assistant_message_kind: null,
      run_id: null,
      ...response,
    },
    requested_at_ms: ordinal * 10_000 + 100,
    responded_at_ms: ordinal * 10_000 + 2_100,
    wall_clock_ms: 2_000,
    since_previous_ms: null,
    error: null,
    evidence: evidence ?? completeEvidence(),
    ...rest,
  };
  return { ...base, violations: transitionViolations(base) };
}

describe("classifyTransitionRequest", () => {
  it("recognises the four transition boundaries, POST only", () => {
    expect(classifyTransitionRequest(`${BASE}/guided/start`, "POST")).toBe("guided/start");
    expect(classifyTransitionRequest(`${BASE}/guided/respond`, "POST")).toBe("guided/respond");
    expect(classifyTransitionRequest(`${BASE}/guided/chat?x=1`, "POST")).toBe("guided/chat");
    expect(classifyTransitionRequest("https://elspeth.example.test/api/tutorial/run", "POST")).toBe("tutorial/run");
    expect(classifyTransitionRequest(`${BASE}/guided/respond`, "GET")).toBeNull();
  });

  it("does not count reconcile probes, reads, or the tutorial sample as transitions", () => {
    expect(classifyTransitionRequest(`${BASE}/guided/start/abc/reconcile`, "POST")).toBeNull();
    expect(classifyTransitionRequest(`${BASE}/guided`, "GET")).toBeNull();
    expect(classifyTransitionRequest(`${BASE}/guided/tutorial-sample`, "POST")).toBeNull();
    expect(classifyTransitionRequest(`${BASE}/messages`, "POST")).toBeNull();
  });

  it("extracts the session id from guided urls only", () => {
    expect(sessionIdFromTransitionUrl(`${BASE}/guided/respond`)).toBe(SID);
    expect(sessionIdFromTransitionUrl("https://elspeth.example.test/api/tutorial/run")).toBeNull();
  });
});

describe("summarizeLlmAuditRows", () => {
  it("reduces audit envelopes and skips conversation rows", () => {
    const rows = summarizeLlmAuditRows([
      { id: "m1", role: "user", content: "hi", tool_calls: null },
      llmCall("a1", { planner_call_ordinal: 1, latency_ms: 4_000 }),
      plannerAttempt("a2", 1, "candidate"),
      llmCall("a3", { status: "error", latency_ms: null }),
      { id: "m2", role: "assistant", content: "ok", tool_calls: null },
    ]);
    expect(rows).toEqual([
      { id: "a1", kind: "llm_call", planner_call_ordinal: 1, status: "success", latency_ms: 4_000, phase: null },
      { id: "a2", kind: "planner_attempt", planner_call_ordinal: 1, status: null, latency_ms: null, phase: "candidate" },
      { id: "a3", kind: "llm_call", planner_call_ordinal: null, status: "error", latency_ms: null, phase: null },
    ]);
  });

  it("fails closed on an unknown envelope kind or a malformed page", () => {
    expect(() => summarizeLlmAuditRows([auditMessage("x", { _kind: "mystery" })])).toThrow(/unknown value mystery/);
    expect(() => summarizeLlmAuditRows({ not: "an array" })).toThrow(/must be an array/);
    expect(() => summarizeLlmAuditRows([{ role: "audit", tool_calls: [{ _kind: "llm_call_audit", call: {} }] }])).toThrow(
      /non-empty id/,
    );
  });
});

describe("attributeFreshRows", () => {
  it("attributes only rows not yet claimed and derives the counts from them", () => {
    const known = new Set(["old-1", "old-2"]);
    const rows = [
      row("old-1", { planner_call_ordinal: 1 }),
      row("old-2", { kind: "planner_attempt", phase: "discovery", latency_ms: null }),
      row("new-1", { planner_call_ordinal: 1, latency_ms: 30_000 }),
      row("new-2", { kind: "planner_attempt", phase: "candidate", planner_call_ordinal: 1, latency_ms: null }),
      row("new-3", { planner_call_ordinal: 2, latency_ms: 20_000, status: "error" }),
      row("new-4", { latency_ms: 500 }),
    ];
    const evidence = attributeFreshRows(known, rows, { afterUnavailable: false });
    expect(evidence).toEqual(
      completeEvidence({
        provider_calls: 3,
        planner_calls: 2,
        planner_runs: 1,
        failed_calls: 1,
        model_latency_ms: 50_500,
        attempt_phases: ["candidate"],
        row_ids: ["new-1", "new-2", "new-3", "new-4"],
      }),
    );
  });

  it("flags rows first seen after an unavailable read as possibly belonging to it", () => {
    const withRows = attributeFreshRows(new Set(), [row("r")], { afterUnavailable: true });
    expect(withRows.includes_rows_from_unavailable_transition).toBe(true);
    const noRows = attributeFreshRows(new Set(), [], { afterUnavailable: true });
    expect(noRows.includes_rows_from_unavailable_transition).toBe(false);
    expect(noRows.provider_calls).toBe(0);
  });
});

describe("transitionViolations", () => {
  it("flags a NEW propose_pipeline turn that paid no planner call (the b073d248e shape)", () => {
    const bypass = entry(3, "guided/respond", {
      response: { next_turn_type: "propose_pipeline", new_turn_occurrence: true },
      evidence: completeEvidence({ provider_calls: 1, planner_calls: 0 }),
    });
    expect(bypass.violations).toEqual([
      expect.stringMatching(/transition 3 \(guided\/respond, Continue\): emitted a new propose_pipeline turn with zero planner provider calls/),
    ]);
  });

  it("accepts a proposal turn that paid for itself, and a re-rendered proposal turn", () => {
    const paid = entry(3, "guided/respond", {
      response: { next_turn_type: "propose_pipeline", new_turn_occurrence: true },
      evidence: completeEvidence({ provider_calls: 2, planner_calls: 2, planner_runs: 1 }),
    });
    expect(paid.violations).toEqual([]);
    const rerender = entry(4, "guided/respond", {
      response: { next_turn_type: "propose_pipeline", new_turn_occurrence: false },
      evidence: completeEvidence(),
    });
    expect(rerender.violations).toEqual([]);
  });

  it("treats unavailable evidence on a guided transition as a violation, not a pass", () => {
    const gap = entry(2, "guided/respond", { evidence: unavailableTransitionEvidence("audit read failed") });
    expect(gap.violations).toEqual([
      "transition 2 (guided/respond, Continue): durable provider-call evidence unavailable: audit read failed",
    ]);
    // The run is not a guided transition; its provider work is the pipeline's
    // own, recorded in Landscape, so an unavailable read there is not a gap.
    const run = entry(9, "tutorial/run", { evidence: unavailableTransitionEvidence("n/a") });
    expect(run.violations).toEqual([]);
  });

  it("does not raise the proposal invariant on a failed response", () => {
    const failed = entry(3, "guided/respond", {
      response: { status: 500, next_turn_type: "propose_pipeline", new_turn_occurrence: true },
    });
    expect(failed.violations).toEqual([]);
  });
});

function walk(): TransitionLedgerEntry[] {
  return [
    entry(1, "guided/start", { gestures: [{ label: "Let's go", at_ms: 1_000 }], evidence: completeEvidence({ row_ids: [] }) }),
    entry(2, "guided/chat", {
      gestures: [{ label: "Send", at_ms: 11_000 }],
      evidence: completeEvidence({ provider_calls: 1, model_latency_ms: 3_000, row_ids: ["c1"] }),
    }),
    entry(3, "guided/respond", {
      gestures: [
        { label: "Looks right", at_ms: 20_000 },
        { label: "Finish sources", at_ms: 21_000 },
      ],
      evidence: completeEvidence({
        provider_calls: 2,
        planner_calls: 2,
        planner_runs: 1,
        model_latency_ms: 60_000,
        attempt_phases: ["discovery", "candidate"],
        row_ids: ["p1", "p2", "p3", "p4"],
      }),
      response: { next_turn_type: "propose_pipeline", new_turn_occurrence: true },
    }),
    entry(4, "guided/respond", {
      gestures: [{ label: "Confirm wiring", at_ms: 40_000 }],
      response: { terminal: "completed" },
    }),
    entry(5, "tutorial/run", {
      gestures: [{ label: "Acknowledge", at_ms: 50_000 }],
      requested_at_ms: 50_100,
      responded_at_ms: 80_100,
      wall_clock_ms: 30_000,
      evidence: completeEvidence(),
    }),
  ];
}

describe("ledgerTotals", () => {
  it("sums the walk and measures gestures / wall clock from the first build gesture to the run", () => {
    const totals = ledgerTotals(walk(), [{ label: "Continue (audit story)", at_ms: 90_000 }], [
      row("c1"),
      row("p1", { planner_call_ordinal: 1 }),
      row("p2", { kind: "planner_attempt", latency_ms: null }),
      row("p3", { planner_call_ordinal: 2 }),
      row("p4", { kind: "planner_attempt", latency_ms: null }),
      row("stray-planner", { planner_call_ordinal: 1 }),
      row("stray-advisor"),
    ]);
    expect(totals).toEqual({
      transitions: 5,
      gestures: 7,
      gestures_to_run: 5,
      provider_calls: 3,
      planner_calls: 2,
      planner_runs: 1,
      failed_calls: 0,
      model_latency_ms: 63_000,
      guided_wall_clock_ms: 8_000,
      wall_clock_to_run_ms: 50_100 - 11_000,
      run_wall_clock_ms: 30_000,
      unattributed_provider_calls: 2,
      unattributed_planner_calls: 1,
      attribution_gaps: 0,
    });
  });

  it("reports null run-relative measures when the walk never reached the run", () => {
    const totals = ledgerTotals(walk().slice(0, 3), [], []);
    expect(totals.gestures_to_run).toBeNull();
    expect(totals.wall_clock_to_run_ms).toBeNull();
    expect(totals.run_wall_clock_ms).toBeNull();
  });

  it("counts guided transitions with unavailable evidence as attribution gaps", () => {
    const entries = walk();
    entries[1] = entry(2, "guided/chat", { evidence: unavailableTransitionEvidence("boom") });
    expect(ledgerTotals(entries, [], []).attribution_gaps).toBe(1);
  });
});

describe("ledgerViolations", () => {
  it("collects per-transition violations and rejects unattributed planner calls", () => {
    const entries = walk();
    entries[2] = entry(3, "guided/respond", {
      response: { next_turn_type: "propose_pipeline", new_turn_occurrence: true },
      evidence: completeEvidence({ provider_calls: 1 }),
    });
    const totals = ledgerTotals(entries, [], [row("stray", { planner_call_ordinal: 1 })]);
    expect(ledgerViolations(entries, totals, { status: "complete", reason: null }, 0)).toEqual([
      expect.stringMatching(/transition 3 .* zero planner provider calls/),
      "1 planner provider call(s) were recorded outside every observed transition",
    ]);
  });

  it("is never vacuously green: a missing final read or an in-flight transition is a violation", () => {
    const entries = walk();
    const totals = ledgerTotals(entries, [], []);
    expect(ledgerViolations(entries, totals, { status: "unavailable", reason: "timeout" }, 1)).toEqual([
      "final durable read unavailable: timeout",
      "1 transition(s) still in flight when the ledger was finalized",
    ]);
    expect(ledgerViolations(entries, totals, { status: "complete", reason: null }, 0)).toEqual([]);
  });
});

describe("renderLedgerMarkdown", () => {
  it("renders one row per transition plus a totals line", () => {
    const entries = walk();
    const postGestures = [{ label: "Take me to the composer", at_ms: 95_000 }];
    const totals = ledgerTotals(entries, postGestures, []);
    const ledger: TransitionLedger = {
      schema: TRANSITION_LEDGER_SCHEMA,
      deployment: { bundle: "/assets/index-test.js", legacy_auto_run: true },
      session_id: SID,
      entries,
      post_gestures: postGestures,
      final_read: { status: "complete", reason: null },
      in_flight_at_finalize: 0,
      totals,
      violations: ledgerViolations(entries, totals, { status: "complete", reason: null }, 0),
    };
    const markdown = renderLedgerMarkdown(ledger);
    const lines = markdown.split("\n");
    expect(lines[0]).toMatch(/^\| # \| endpoint \| gesture \|/);
    expect(lines.filter((line) => /^\| \d+ \|/.test(line))).toHaveLength(5);
    expect(markdown).toContain("| 3 | guided/respond | Finish sources | 2 |");
    expect(markdown).toContain("| 2 (1) | 60.0s |");
    expect(markdown).toContain("terminal=completed");
    expect(markdown).toContain("Totals: 5 transitions · 7 gestures (5 to run) · 3 provider calls · 2 planner calls in 1 planner run(s)");
    expect(markdown).toContain("Post-run gestures: Take me to the composer");
    expect(markdown).not.toContain("Violations:");
  });
});
