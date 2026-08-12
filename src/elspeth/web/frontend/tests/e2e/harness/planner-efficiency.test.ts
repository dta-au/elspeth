import type { APIRequestContext } from "@playwright/test";
import { describe, expect, it } from "vitest";

import {
  classifyPlannerEfficiency,
  fetchPlannerAuditEvidence,
  parsePlannerAuditMessages,
  plannerEfficiencyAssertionFailure,
  unavailablePlannerEfficiency,
} from "../helpers/tutorial-harness";

function ordinaryMessage(id: string): Record<string, unknown> {
  return { id, role: "user", tool_calls: null };
}

function plannerCall(
  ordinal: number | null,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    id: `call-${String(ordinal)}`,
    role: "audit",
    tool_calls: [
      {
        _kind: "llm_call_audit",
        call: {
          planner_call_ordinal: ordinal,
          status: "success",
          prompt_tokens: 10,
          provider_cost: 0.25,
          latency_ms: 120,
          ...overrides,
        },
      },
    ],
  };
}

function plannerAttempt(
  ordinal: number,
  phase: "discovery" | "candidate" | "repair" = "candidate",
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    id: `attempt-${ordinal}`,
    role: "audit",
    tool_calls: [
      {
        _kind: "planner_attempt_audit",
        attempt: {
          ordinal,
          planner_call_ordinal: ordinal,
          phase,
          outcome: phase === "candidate" ? "accepted" : "discovery_executed",
          planner_code: null,
          selected_tools: phase === "discovery" ? ["get_plugin_schema"] : ["emit_pipeline_proposal"],
          requested_information: phase === "discovery" ? ["plugin.schema"] : [],
          new_information: phase === "discovery" ? ["plugin.schema"] : [],
          rejection_codes: [],
          candidate_shape_hash: phase === "candidate" ? "a".repeat(64) : null,
          repeated_fingerprint: false,
          led_to: phase === "candidate" ? "done" : "continue",
          future_safe_field: "ignored",
          ...overrides,
        },
      },
    ],
  };
}

describe("parsePlannerAuditMessages", () => {
  it("treats a session with only non-planner LLM audit as zero planner calls", () => {
    const evidence = parsePlannerAuditMessages([
      ordinaryMessage("user-1"),
      plannerCall(null),
      ordinaryMessage("assistant-1"),
    ]);

    expect(evidence).toEqual({ calls: [], attempts: [], cohorts: [] });
    expect(classifyPlannerEfficiency(evidence, true)).toMatchObject({
      passed: true,
      route: "zero_call_server",
      provider_calls: 0,
      violations: [],
    });
  });

  it("parses the two-call generic route and preserves complete usage metrics", () => {
    const evidence = parsePlannerAuditMessages([
      plannerCall(1),
      plannerAttempt(1, "discovery"),
      plannerCall(2, { prompt_tokens: 20, provider_cost: 0.5, latency_ms: 80 }),
      plannerAttempt(2, "candidate"),
    ]);

    expect(classifyPlannerEfficiency(evidence, true)).toEqual({
      evidence_status: "complete",
      passed: true,
      route: "generic",
      provider_calls: 2,
      useful_discovery_turns: 1,
      no_gain_turns: 0,
      repair_turns: 0,
      prompt_tokens: 30,
      prompt_tokens_complete: true,
      provider_cost: 0.75,
      provider_cost_complete: true,
      model_latency_ms: 200,
      selected_tools: ["get_plugin_schema", "emit_pipeline_proposal"],
      violations: [],
    });
  });

  it("accepts a successful one-call terminal candidate", () => {
    const evidence = parsePlannerAuditMessages([
      plannerCall(1),
      plannerAttempt(1, "candidate"),
    ]);

    expect(classifyPlannerEfficiency(evidence, true)).toMatchObject({
      passed: true,
      route: "generic",
      provider_calls: 1,
      useful_discovery_turns: 0,
      no_gain_turns: 0,
      repair_turns: 0,
    });
  });

  it("admits the response-phase failure vocabulary as evidence, not acceptance", () => {
    const evidence = parsePlannerAuditMessages([
      plannerCall(1, { status: "malformed_response" }),
      plannerAttempt(1, "candidate", {
        phase: "response",
        outcome: "malformed_response",
        planner_code: "MALFORMED_RESPONSE",
        selected_tools: [],
        candidate_shape_hash: null,
        led_to: "terminal",
      }),
    ]);

    expect(evidence.attempts[0]).toMatchObject({
      phase: "response",
      outcome: "malformed_response",
      planner_code: "MALFORMED_RESPONSE",
    });
    expect(classifyPlannerEfficiency(evidence, true).passed).toBe(false);
  });

  it("keeps token and cost totals unknown when any provider call omits them", () => {
    const evidence = parsePlannerAuditMessages([
      plannerCall(1, { prompt_tokens: null }),
      plannerAttempt(1, "discovery"),
      plannerCall(2, { provider_cost: null }),
      plannerAttempt(2, "candidate"),
    ]);

    expect(classifyPlannerEfficiency(evidence, true)).toMatchObject({
      passed: true,
      prompt_tokens: null,
      prompt_tokens_complete: false,
      provider_cost: null,
      provider_cost_complete: false,
    });
  });

  it("accepts a transport retry as structurally valid without inventing an attempt", () => {
    const evidence = parsePlannerAuditMessages([
      plannerCall(1, { status: "api_error" }),
      plannerCall(2),
      plannerAttempt(1, "candidate", { planner_call_ordinal: 2 }),
    ]);

    expect(evidence.cohorts).toHaveLength(1);
    expect(evidence.calls.map((call) => call.planner_call_ordinal)).toEqual([1, 2]);
    expect(evidence.attempts.map((attempt) => attempt.planner_call_ordinal)).toEqual([2]);
    expect(classifyPlannerEfficiency(evidence, true)).toMatchObject({
      passed: false,
      provider_calls: 2,
      violations: expect.arrayContaining(["all planner provider calls must succeed"]),
    });
  });

  it("parses reset ordinals as distinct completed planner cohorts", () => {
    const evidence = parsePlannerAuditMessages([
      plannerCall(1),
      plannerAttempt(1, "candidate"),
      ordinaryMessage("between-plans"),
      plannerCall(1),
      plannerAttempt(1, "candidate"),
    ]);

    expect(evidence.cohorts).toHaveLength(2);
    expect(evidence.cohorts.map((cohort) => cohort.calls.length)).toEqual([1, 1]);
    expect(classifyPlannerEfficiency(evidence, true)).toMatchObject({
      passed: false,
      provider_calls: 2,
      violations: expect.arrayContaining(["generic route must use exactly one semantic planning cohort"]),
    });
  });

  it("starts a new cohort after a transport-only terminal cohort", () => {
    const evidence = parsePlannerAuditMessages([
      plannerCall(1, { status: "timeout" }),
      plannerCall(1),
      plannerAttempt(1, "candidate"),
    ]);

    expect(evidence.cohorts).toHaveLength(2);
    expect(evidence.cohorts.map((cohort) => cohort.attempts.length)).toEqual([0, 1]);
    expect(classifyPlannerEfficiency(evidence, true)).toMatchObject({
      passed: false,
      violations: expect.arrayContaining([
        "generic route must use exactly one semantic planning cohort",
        "all planner provider calls must succeed",
      ]),
    });
  });

  it.each([
    {
      name: "continue attempt and between-call budget failure",
      ledTo: "continue",
      phase: "discovery",
      outcome: "discovery_executed",
      finalTransportStatus: null,
      expectedCalls: [1, 1],
    },
    {
      name: "repair attempt and final provider failure",
      ledTo: "repair",
      phase: "candidate",
      outcome: "candidate_rejected",
      finalTransportStatus: "api_error",
      expectedCalls: [2, 1],
    },
  ] as const)("starts a new cohort after $name", (scenario) => {
    const firstCohortFinalCall = scenario.finalTransportStatus === null
      ? []
      : [plannerCall(2, { status: scenario.finalTransportStatus })];
    const evidence = parsePlannerAuditMessages([
      plannerCall(1),
      plannerAttempt(1, scenario.phase, {
        led_to: scenario.ledTo,
        outcome: scenario.outcome,
      }),
      ...firstCohortFinalCall,
      plannerCall(1),
      plannerAttempt(1, "candidate"),
    ]);

    expect(evidence.cohorts).toHaveLength(2);
    expect(evidence.cohorts.map((cohort) => cohort.calls.length)).toEqual(scenario.expectedCalls);
    expect(evidence.cohorts.map((cohort) => cohort.attempts.length)).toEqual([1, 1]);
    const efficiency = classifyPlannerEfficiency(evidence, true);
    expect(efficiency.passed).toBe(false);
    expect(efficiency.violations).toContain(
      "generic route must use exactly one semantic planning cohort",
    );
    expect(efficiency.violations.includes("all planner provider calls must succeed")).toBe(
      scenario.finalTransportStatus !== null,
    );
  });

  it.each([
    {
      name: "ordinal reset while a response attempt is pending",
      rows: [plannerCall(1), plannerCall(1, { status: "api_error" })],
      message: "must be immediately followed by its attempt",
    },
    {
      name: "gapped planner call ordinal",
      rows: [plannerCall(1, { status: "api_error" }), plannerCall(3, { status: "api_error" })],
      message: "planner call ordinals must be contiguous",
    },
    {
      name: "gapped attempt ordinal",
      rows: [plannerCall(1), plannerAttempt(2, "candidate", { planner_call_ordinal: 1 })],
      message: "planner attempt ordinal 2 must equal 1",
    },
    {
      name: "attempt separated from its call",
      rows: [plannerCall(1), ordinaryMessage("interleaved"), plannerAttempt(1)],
      message: "must be immediately followed by its attempt",
    },
  ])("rejects $name", ({ rows, message }) => {
    expect(() => parsePlannerAuditMessages(rows)).toThrow(message);
  });

  it("rejects malformed typed attempt fields while ignoring unknown properties", () => {
    expect(() =>
      parsePlannerAuditMessages([
        plannerCall(1),
        plannerAttempt(1, "candidate", { repeated_fingerprint: "false" }),
      ]),
    ).toThrow("repeated_fingerprint must be a boolean");
  });

  it.each([
    ["missing message role", { id: "bad", tool_calls: null }, "message[0].role must be a non-empty string"],
    [
      "unknown message role",
      { id: "bad", role: "invented", tool_calls: null },
      "message[0].role has unknown value invented",
    ],
    [
      "unknown audit envelope kind",
      { id: "bad", role: "audit", tool_calls: [{ _kind: "invented" }] },
      "message[0].tool_calls[0]._kind has unknown value invented",
    ],
  ])("rejects %s", (_name, row, message) => {
    expect(() => parsePlannerAuditMessages([row])).toThrow(message as string);
  });

  it.each([
    ["unknown phase", { phase: "invented" }, "planner attempt phase has unknown value invented"],
    ["unknown outcome", { outcome: "invented" }, "planner attempt outcome has unknown value invented"],
    ["unknown planner code", { planner_code: "INVENTED" }, "planner attempt planner_code has unknown value INVENTED"],
    [
      "instance information key",
      { requested_information: ["plugin.schema:transform/example"] },
      "planner attempt requested_information[0] has unknown information class",
    ],
  ])("rejects %s", (_name, override, message) => {
    expect(() =>
      parsePlannerAuditMessages([
        plannerCall(1),
        plannerAttempt(1, "candidate", override as Record<string, unknown>),
      ]),
    ).toThrow(message as string);
  });

  it.each([
    ["overlong selected tool", { selected_tools: [`a${"b".repeat(128)}`] }, "selected_tools[0] must be a bounded planner token"],
    ["invalid selected tool", { selected_tools: ["Get-plugin"] }, "selected_tools[0] must be a bounded planner token"],
    ["duplicate rejection code", { rejection_codes: ["bad_shape", "bad_shape"] }, "rejection_codes entries must be unique"],
    ["invalid rejection code", { rejection_codes: ["BAD_SHAPE"] }, "rejection_codes[0] must be a bounded planner token"],
  ])("enforces Task 7 token bounds for %s", (_name, override, message) => {
    expect(() =>
      parsePlannerAuditMessages([
        plannerCall(1),
        plannerAttempt(1, "candidate", override as Record<string, unknown>),
      ]),
    ).toThrow(message as string);
  });
});

describe("classifyPlannerEfficiency", () => {
  it("requires functional completion for the zero-call route", () => {
    expect(classifyPlannerEfficiency({ calls: [], attempts: [], cohorts: [] }, false)).toMatchObject({
      passed: false,
      route: "invalid",
      violations: ["functional completion is required for efficiency acceptance"],
    });
  });

  it("rejects no-gain discovery, repairs, failed calls, and state/list rediscovery", () => {
    const evidence = parsePlannerAuditMessages([
      plannerCall(1),
      plannerAttempt(1, "discovery", {
        selected_tools: ["get_pipeline_state", "list_transforms"],
        new_information: [],
      }),
      plannerCall(2, { status: "api_error" }),
      plannerCall(3),
      plannerAttempt(2, "repair", {
        planner_call_ordinal: 3,
        outcome: "accepted",
        selected_tools: ["emit_pipeline_proposal"],
        led_to: "done",
      }),
    ]);

    const result = classifyPlannerEfficiency(evidence, true);
    expect(result.passed).toBe(false);
    expect(result.route).toBe("invalid");
    expect(result.no_gain_turns).toBe(1);
    expect(result.repair_turns).toBe(1);
    expect(result.violations).toEqual(
      expect.arrayContaining([
        "all planner provider calls must succeed",
        "generic route must be candidate or discovery,candidate",
        "generic route must have zero no-gain discovery turns",
        "generic route must have zero repair turns",
        "planner selected redundant state/list tools: get_pipeline_state, list_transforms",
      ]),
    );
  });

  it("does not call an empty-information discovery no-gain when it requested nothing", () => {
    const evidence = parsePlannerAuditMessages([
      plannerCall(1),
      plannerAttempt(1, "discovery", {
        requested_information: [],
        new_information: [],
      }),
      plannerCall(2),
      plannerAttempt(2, "candidate"),
    ]);

    expect(classifyPlannerEfficiency(evidence, true).no_gain_turns).toBe(0);
  });

  it.each([
    ["wrong outcome", { outcome: "guard_fired" }],
    ["wrong destination", { led_to: "repair" }],
    ["no information gain", { new_information: [] }],
  ])("rejects generic discovery with %s", (_name, override) => {
    const evidence = parsePlannerAuditMessages([
      plannerCall(1),
      plannerAttempt(1, "discovery", override as Record<string, unknown>),
      plannerCall(2),
      plannerAttempt(2, "candidate"),
    ]);

    expect(classifyPlannerEfficiency(evidence, true).violations).toContain(
      "generic discovery must execute once, continue, and add information",
    );
  });
});

describe("fetchPlannerAuditEvidence", () => {
  it("paginates the audit-grade messages view in 500-row pages", async () => {
    const urls: string[] = [];
    const pages: unknown[] = [
      Array.from({ length: 500 }, (_, index) => ordinaryMessage(`ordinary-${index}`)),
      [plannerCall(1), plannerAttempt(1)],
    ];
    const ctx = {
      get: async (url: string) => {
        urls.push(url);
        const body = pages.shift();
        return {
          ok: () => true,
          status: () => 200,
          json: async () => body,
          text: async () => "",
        };
      },
    } as unknown as APIRequestContext;

    const evidence = await fetchPlannerAuditEvidence(ctx, "session-id");

    expect(urls).toEqual([
      "/api/sessions/session-id/messages?include_llm_audit=true&limit=500&offset=0",
      "/api/sessions/session-id/messages?include_llm_audit=true&limit=500&offset=500",
    ]);
    expect(evidence.calls).toHaveLength(1);
    expect(evidence.attempts).toHaveLength(1);
  });

  it("preserves adjacency when a planner call and attempt straddle a page boundary", async () => {
    const urls: string[] = [];
    const firstPage = [
      ...Array.from({ length: 499 }, (_, index) => ordinaryMessage(`ordinary-${index}`)),
      plannerCall(1),
    ];
    const pages: unknown[] = [firstPage, [plannerAttempt(1)]];
    const ctx = {
      get: async (url: string) => {
        urls.push(url);
        const body = pages.shift();
        return {
          ok: () => true,
          status: () => 200,
          json: async () => body,
          text: async () => "",
        };
      },
    } as unknown as APIRequestContext;

    const evidence = await fetchPlannerAuditEvidence(ctx, "session-id");

    expect(urls).toHaveLength(2);
    expect(evidence.cohorts).toHaveLength(1);
    expect(evidence.attempts).toHaveLength(1);
  });

  it("fails rather than manufacturing zero-call evidence when a page is unavailable", async () => {
    const ctx = {
      get: async () => ({
        ok: () => false,
        status: () => 503,
        json: async () => null,
        text: async () => "temporarily unavailable",
      }),
    } as unknown as APIRequestContext;

    await expect(fetchPlannerAuditEvidence(ctx, "session-id")).rejects.toThrow(
      "planner audit messages failed 503: temporarily unavailable",
    );
  });
});

describe("plannerEfficiencyAssertionFailure", () => {
  it("does not mask an existing tutorial failure", () => {
    const unavailable = unavailablePlannerEfficiency("audit API unavailable");

    expect(plannerEfficiencyAssertionFailure(unavailable, "original tutorial failure")).toBeNull();
  });

  it("fails a no-hard-error run when efficiency evidence is unavailable", () => {
    const unavailable = unavailablePlannerEfficiency("audit API unavailable");

    expect(plannerEfficiencyAssertionFailure(unavailable, null)).toContain(
      "planner efficiency evidence unavailable",
    );
  });

  it("fails a no-hard-error run when functional completion was not achieved", () => {
    const efficiency = classifyPlannerEfficiency({ calls: [], attempts: [], cohorts: [] }, false);

    expect(plannerEfficiencyAssertionFailure(efficiency, null)).toContain(
      "functional completion is required for efficiency acceptance",
    );
  });
});
