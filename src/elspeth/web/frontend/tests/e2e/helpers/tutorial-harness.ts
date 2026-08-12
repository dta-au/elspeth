import { type APIRequestContext } from "@playwright/test";
import { authedContext, tokenFromStorageState } from "./api";
import { readFileSync } from "node:fs";

const STORAGE = "tests/e2e/.auth/staging-user.json";

export async function harnessCtx(): Promise<APIRequestContext> {
  const token = tokenFromStorageState(JSON.parse(readFileSync(STORAGE, "utf-8")));
  return authedContext(token);
}

export async function resetToFirstRun(ctx: APIRequestContext): Promise<void> {
  const r = await ctx.patch("/api/composer-preferences", {
    data: { tutorial_completed_at: null, default_mode: "guided" },
  });
  if (!r.ok()) throw new Error(`reset prefs failed ${r.status()}: ${await r.text()}`);
}

export async function cleanSessions(ctx: APIRequestContext): Promise<void> {
  await ctx.post("/api/tutorial/abandon"); // best-effort; 204 or no-op
  await ctx.delete("/api/tutorial/orphans"); // best-effort
}

export interface InterpEvent { kind: string | null; user_term: string | null; composer_skill_hash: string | null; model_identifier: string | null; }
export async function fetchInterpretationEvents(ctx: APIRequestContext, sid: string): Promise<InterpEvent[]> {
  // Real route is GET /api/sessions/{sid}/interpretations (envelope {events:[...]}),
  // NOT the plan's /interpretation-events. Confirmed in
  // src/elspeth/web/sessions/routes/interpretation.py:195 (list_interpretations).
  const r = await ctx.get(`/api/sessions/${sid}/interpretations`);
  if (!r.ok()) throw new Error(`interp events failed ${r.status()}`);
  return ((await r.json()).events ?? []) as InterpEvent[];
}

const MESSAGE_AUDIT_PAGE_SIZE = 500;
const MESSAGE_ROLES = new Set(["user", "assistant", "system", "tool", "audit"]);
const PLANNER_CALL_STATUSES = new Set([
  "success",
  "timeout",
  "api_error",
  "auth_error",
  "bad_request_error",
  "malformed_response",
  "cancelled",
]);
const PLANNER_ATTEMPT_PHASES = new Set([
  "response",
  "discovery",
  "candidate",
  "repair",
  "hatch",
  "prose",
]);
const PLANNER_ATTEMPT_OUTCOMES = new Set([
  "discovery_executed",
  "candidate_rejected",
  "arg_error",
  "deferred_claim",
  "truncated",
  "prose_nudged",
  "prose_reply",
  "guard_fired",
  "budget_exhausted",
  "declined",
  "accepted",
  "malformed_response",
  "cancelled",
  "internal_error",
]);
const PLANNER_CODES = new Set([
  "CANDIDATE_CONSTRUCTION_ERROR",
  "COMPLETION_TOKENS_EXCEEDED",
  "COMPOSITION_EXHAUSTED",
  "COST_CAP_EXCEEDED",
  "COST_UNAVAILABLE",
  "DECLINED",
  "DISCOVERY_CYCLE",
  "DISCOVERY_EXHAUSTED",
  "DISCOVERY_NO_GAIN",
  "DISCOVERY_ONLY",
  "MALFORMED_RESPONSE",
  "PROSE_REPLY",
  "PROVIDER_CALLS_EXHAUSTED",
  "PROVIDER_ERROR",
  "REPAIR_BLIND_REPEAT",
  "REPAIR_EXHAUSTED",
  "REQUEST_BYTES_EXHAUSTED",
  "RESPONSE_TRUNCATED",
  "TIMEOUT",
  "TOOL_CALLS_EXHAUSTED",
  "VALIDATION_FAILED",
]);
const PLANNER_INFORMATION_CLASSES = new Set([
  "pipeline.current",
  "pipeline.full",
  "pipeline.source",
  "pipeline.component",
  "catalog.selection",
  "catalog.details.source",
  "catalog.details.transform",
  "catalog.details.sink",
  "recipe.index",
  "plugin.schema",
  "plugin.assistance",
  "model.catalog",
  "blob.index.session",
  "blob.index.composer",
  "blob.metadata",
  "blob.inspection",
  "blob.content",
  "secret.index",
  "secret.reference",
  "audit.info",
  "expression.grammar",
  "pipeline.preview",
  "pipeline.diff",
  "validation.code",
]);
const PLANNER_ATTEMPT_DESTINATIONS = new Set([
  "continue",
  "repair",
  "hatch",
  "terminal",
  "done",
]);
const REDUNDANT_STATE_LIST_TOOLS = new Set([
  "get_pipeline_state",
  "list_sources",
  "list_transforms",
  "list_sinks",
]);

interface PlannerCallAudit {
  planner_call_ordinal: number;
  status: string;
  prompt_tokens: number | null;
  provider_cost: number | null;
  latency_ms: number;
}

interface PlannerAttemptAudit {
  ordinal: number;
  planner_call_ordinal: number;
  phase: string;
  outcome: string;
  planner_code: string | null;
  selected_tools: string[];
  requested_information: string[];
  new_information: string[];
  rejection_codes: string[];
  candidate_shape_hash: string | null;
  repeated_fingerprint: boolean;
  led_to: string;
}

export interface PlannerAuditCohort {
  ordinal: number;
  calls: PlannerCallAudit[];
  attempts: PlannerAttemptAudit[];
}

export interface PlannerAuditEvidence {
  calls: PlannerCallAudit[];
  attempts: PlannerAttemptAudit[];
  cohorts: PlannerAuditCohort[];
}

export interface PlannerEfficiency {
  evidence_status: "complete" | "unavailable";
  passed: boolean;
  route: "generic" | "invalid" | "unknown";
  provider_calls: number | null;
  useful_discovery_turns: number | null;
  no_gain_turns: number | null;
  repair_turns: number | null;
  prompt_tokens: number | null;
  prompt_tokens_complete: boolean;
  provider_cost: number | null;
  provider_cost_complete: boolean;
  model_latency_ms: number | null;
  selected_tools: string[];
  violations: string[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireRecord(value: unknown, label: string): Record<string, unknown> {
  if (!isRecord(value)) throw new Error(`${label} must be an object`);
  return value;
}

function requirePositiveInteger(value: unknown, label: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value <= 0) {
    throw new Error(`${label} must be a positive integer`);
  }
  return value;
}

function requireNonNegativeInteger(value: unknown, label: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
    throw new Error(`${label} must be a non-negative integer`);
  }
  return value;
}

function requireNullableNonNegativeInteger(value: unknown, label: string): number | null {
  return value === null ? null : requireNonNegativeInteger(value, label);
}

function requireNullableNonNegativeNumber(value: unknown, label: string): number | null {
  if (value === null) return null;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new Error(`${label} must be a finite non-negative number or null`);
  }
  return value;
}

function requireNonEmptyString(value: unknown, label: string): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new Error(`${label} must be a non-empty string`);
  }
  return value;
}

function requireNullableNonEmptyString(value: unknown, label: string): string | null {
  return value === null ? null : requireNonEmptyString(value, label);
}

function requireStringArray(value: unknown, label: string): string[] {
  if (!Array.isArray(value)) throw new Error(`${label} must be an array`);
  return value.map((item, index) => requireNonEmptyString(item, `${label}[${index}]`));
}

function requirePlannerTokens(
  value: unknown,
  label: string,
  options: { unique: boolean },
): string[] {
  const tokens = requireStringArray(value, label);
  for (const [index, token] of tokens.entries()) {
    if (token.length > 128 || !/^[a-z][a-z0-9_.]*$/.test(token)) {
      throw new Error(`${label}[${index}] must be a bounded planner token`);
    }
  }
  if (options.unique && new Set(tokens).size !== tokens.length) {
    throw new Error(`${label} entries must be unique`);
  }
  return tokens;
}

function requireInformationClasses(value: unknown, label: string): string[] {
  const classes = requireStringArray(value, label);
  for (const [index, informationClass] of classes.entries()) {
    if (!PLANNER_INFORMATION_CLASSES.has(informationClass)) {
      throw new Error(`${label}[${index}] has unknown information class ${informationClass}`);
    }
  }
  return classes;
}

function parsePlannerCall(value: unknown): PlannerCallAudit | null {
  const call = requireRecord(value, "llm_call_audit.call");
  const rawOrdinal = call.planner_call_ordinal;
  if (rawOrdinal === null || rawOrdinal === undefined) return null;
  const status = requireNonEmptyString(call.status, "planner call status");
  if (!PLANNER_CALL_STATUSES.has(status)) {
    throw new Error(`planner call status has unknown value ${status}`);
  }
  return {
    planner_call_ordinal: requirePositiveInteger(rawOrdinal, "planner_call_ordinal"),
    status,
    prompt_tokens: requireNullableNonNegativeInteger(call.prompt_tokens, "planner call prompt_tokens"),
    provider_cost: requireNullableNonNegativeNumber(call.provider_cost, "planner call provider_cost"),
    latency_ms: requireNonNegativeInteger(call.latency_ms, "planner call latency_ms"),
  };
}

function parsePlannerAttempt(value: unknown): PlannerAttemptAudit {
  const attempt = requireRecord(value, "planner_attempt_audit.attempt");
  const phase = requireNonEmptyString(attempt.phase, "planner attempt phase");
  if (!PLANNER_ATTEMPT_PHASES.has(phase)) {
    throw new Error(`planner attempt phase has unknown value ${phase}`);
  }
  const ledTo = requireNonEmptyString(attempt.led_to, "planner attempt led_to");
  if (!PLANNER_ATTEMPT_DESTINATIONS.has(ledTo)) {
    throw new Error(`planner attempt led_to has unknown value ${ledTo}`);
  }
  const candidateShapeHash = requireNullableNonEmptyString(
    attempt.candidate_shape_hash,
    "planner attempt candidate_shape_hash",
  );
  if (candidateShapeHash !== null && !/^[0-9a-f]{64}$/.test(candidateShapeHash)) {
    throw new Error("planner attempt candidate_shape_hash must be 64 lowercase hexadecimal characters or null");
  }
  if (typeof attempt.repeated_fingerprint !== "boolean") {
    throw new Error("planner attempt repeated_fingerprint must be a boolean");
  }
  const outcome = requireNonEmptyString(attempt.outcome, "planner attempt outcome");
  if (!PLANNER_ATTEMPT_OUTCOMES.has(outcome)) {
    throw new Error(`planner attempt outcome has unknown value ${outcome}`);
  }
  const plannerCode = requireNullableNonEmptyString(
    attempt.planner_code,
    "planner attempt planner_code",
  );
  if (plannerCode !== null && !PLANNER_CODES.has(plannerCode)) {
    throw new Error(`planner attempt planner_code has unknown value ${plannerCode}`);
  }
  return {
    ordinal: requirePositiveInteger(attempt.ordinal, "planner attempt ordinal"),
    planner_call_ordinal: requirePositiveInteger(
      attempt.planner_call_ordinal,
      "planner attempt planner_call_ordinal",
    ),
    phase,
    outcome,
    planner_code: plannerCode,
    selected_tools: requirePlannerTokens(
      attempt.selected_tools,
      "planner attempt selected_tools",
      { unique: false },
    ),
    requested_information: requireInformationClasses(
      attempt.requested_information,
      "planner attempt requested_information",
    ),
    new_information: requireInformationClasses(
      attempt.new_information,
      "planner attempt new_information",
    ),
    rejection_codes: requirePlannerTokens(
      attempt.rejection_codes,
      "planner attempt rejection_codes",
      { unique: true },
    ),
    candidate_shape_hash: candidateShapeHash,
    repeated_fingerprint: attempt.repeated_fingerprint,
    led_to: ledTo,
  };
}

/** Parse the audit-grade message projection without trusting its JSON shape. */
export function parsePlannerAuditMessages(value: unknown): PlannerAuditEvidence {
  if (!Array.isArray(value)) throw new Error("planner audit messages response must be an array");
  const cohorts: PlannerAuditCohort[] = [];
  let currentCohort: PlannerAuditCohort | null = null;
  let currentCohortComplete = false;
  let pendingResponseCall: { call: PlannerCallAudit; row: number } | null = null;

  for (const [messageIndex, rawMessage] of value.entries()) {
    const message = requireRecord(rawMessage, `message[${messageIndex}]`);
    const role = requireNonEmptyString(message.role, `message[${messageIndex}].role`);
    if (!MESSAGE_ROLES.has(role)) {
      throw new Error(`message[${messageIndex}].role has unknown value ${role}`);
    }
    if (role !== "audit") continue;
    if (message.tool_calls === null) continue;
    if (!Array.isArray(message.tool_calls)) {
      throw new Error(`message[${messageIndex}].tool_calls must be an array or null`);
    }
    const rowCalls: PlannerCallAudit[] = [];
    const rowAttempts: PlannerAttemptAudit[] = [];
    for (const [envelopeIndex, rawEnvelope] of message.tool_calls.entries()) {
      const envelope = requireRecord(
        rawEnvelope,
        `message[${messageIndex}].tool_calls[${envelopeIndex}]`,
      );
      const kind = requireNonEmptyString(
        envelope._kind,
        `message[${messageIndex}].tool_calls[${envelopeIndex}]._kind`,
      );
      if (kind === "llm_call_audit") {
        const call = parsePlannerCall(envelope.call);
        if (call !== null) {
          rowCalls.push(call);
        }
      } else if (kind === "planner_attempt_audit") {
        rowAttempts.push(parsePlannerAttempt(envelope.attempt));
      } else {
        throw new Error(
          `message[${messageIndex}].tool_calls[${envelopeIndex}]._kind has unknown value ${kind}`,
        );
      }
    }
    if (rowCalls.length > 1 || rowAttempts.length > 1 || (rowCalls.length > 0 && rowAttempts.length > 0)) {
      throw new Error(`message[${messageIndex}] must contain at most one planner audit envelope`);
    }

    const rowAttempt = rowAttempts[0] ?? null;
    if (pendingResponseCall !== null) {
      if (
        rowAttempt === null ||
        messageIndex !== pendingResponseCall.row + 1 ||
        rowAttempt.planner_call_ordinal !== pendingResponseCall.call.planner_call_ordinal
      ) {
        throw new Error(
          `planner response-bearing call ${pendingResponseCall.call.planner_call_ordinal} must be immediately followed by its attempt`,
        );
      }
      if (currentCohort === null) throw new Error("planner audit cohort state is missing");
      const expectedAttemptOrdinal = currentCohort.attempts.length + 1;
      if (rowAttempt.ordinal !== expectedAttemptOrdinal) {
        throw new Error(
          `planner attempt ordinal ${rowAttempt.ordinal} must equal ${expectedAttemptOrdinal} within its cohort`,
        );
      }
      currentCohort.attempts.push(rowAttempt);
      currentCohortComplete = rowAttempt.led_to === "done" || rowAttempt.led_to === "terminal";
      pendingResponseCall = null;
    } else if (rowAttempt !== null) {
      throw new Error(
        `planner attempt ${rowAttempt.ordinal} must immediately follow planner call ${rowAttempt.planner_call_ordinal}`,
      );
    }

    const rowCall = rowCalls[0] ?? null;
    if (rowCall === null) continue;
    if (rowCall.planner_call_ordinal === 1) {
      // plan_pipeline ordinals restart at 1 for every invocation. A preceding
      // cohort need not have emitted a terminal semantic attempt: transport or
      // between-call budget failures can end it first. Pending response-bearing
      // calls were rejected above, before an ambiguous reset can reach here.
      currentCohort = { ordinal: cohorts.length + 1, calls: [], attempts: [] };
      cohorts.push(currentCohort);
      currentCohortComplete = false;
    } else {
      if (currentCohort === null || currentCohortComplete) {
        throw new Error("planner call ordinals must start at 1 for each cohort");
      }
      const priorOrdinal = currentCohort.calls.at(-1)?.planner_call_ordinal ?? 0;
      if (rowCall.planner_call_ordinal !== priorOrdinal + 1) {
        throw new Error("planner call ordinals must be contiguous within a cohort");
      }
    }
    currentCohort.calls.push(rowCall);
    if (rowCall.status === "success" || rowCall.status === "malformed_response") {
      pendingResponseCall = { call: rowCall, row: messageIndex };
    }
  }

  if (pendingResponseCall !== null) {
    throw new Error(
      `planner response-bearing call ${pendingResponseCall.call.planner_call_ordinal} must be immediately followed by its attempt`,
    );
  }
  return {
    calls: cohorts.flatMap((cohort) => cohort.calls),
    attempts: cohorts.flatMap((cohort) => cohort.attempts),
    cohorts,
  };
}

/** Fetch every filtered message page; hidden audit rows are sliced after filtering. */
export async function fetchPlannerAuditEvidence(
  ctx: APIRequestContext,
  sid: string,
): Promise<PlannerAuditEvidence> {
  const messages: unknown[] = [];
  for (let offset = 0; ; offset += MESSAGE_AUDIT_PAGE_SIZE) {
    const r = await ctx.get(
      `/api/sessions/${sid}/messages?include_llm_audit=true&limit=${MESSAGE_AUDIT_PAGE_SIZE}&offset=${offset}`,
    );
    if (!r.ok()) {
      throw new Error(`planner audit messages failed ${r.status()}: ${(await r.text()).slice(0, 500)}`);
    }
    const page = await r.json();
    if (!Array.isArray(page)) throw new Error("planner audit messages response must be an array");
    messages.push(...page);
    if (page.length < MESSAGE_AUDIT_PAGE_SIZE) break;
  }
  return parsePlannerAuditMessages(messages);
}

function completeSum(
  values: Array<number | null>,
): { value: number | null; complete: boolean } {
  const complete = values.every((value) => value !== null);
  return {
    value: complete ? values.reduce<number>((total, value) => total + (value ?? 0), 0) : null,
    complete,
  };
}

/** Grade structural provider work independently from elapsed wall-clock time. */
export function classifyPlannerEfficiency(
  evidence: PlannerAuditEvidence,
  functionalCompleted: boolean,
): PlannerEfficiency {
  const { calls, attempts } = evidence;
  const usefulDiscoveryTurns = attempts.filter(
    (attempt) => attempt.phase === "discovery" && attempt.new_information.length > 0,
  ).length;
  const noGainTurns = attempts.filter(
    (attempt) =>
      attempt.phase === "discovery" &&
      attempt.requested_information.length > 0 &&
      attempt.new_information.length === 0,
  ).length;
  const repairTurns = attempts.filter((attempt) => attempt.phase === "repair").length;
  const promptTokens = completeSum(calls.map((call) => call.prompt_tokens));
  const providerCost = completeSum(calls.map((call) => call.provider_cost));
  const selectedTools = attempts.flatMap((attempt) => attempt.selected_tools);
  const redundantSelections = [...new Set(selectedTools.filter((name) => REDUNDANT_STATE_LIST_TOOLS.has(name)))].sort();
  const violations: string[] = [];

  if (!functionalCompleted) {
    violations.push("functional completion is required for efficiency acceptance");
  }
  if (calls.length === 0) {
    // The tutorial must exercise the composer LLM planner: a zero-call walk
    // means the audit view is empty or a deterministic path bypassed the
    // model, and neither is acceptable evidence of planner efficiency.
    violations.push("the tutorial must converge through the LLM planner; zero provider calls is not a valid route");
  } else {
    if (evidence.cohorts.length !== 1) {
      violations.push("generic route must use exactly one semantic planning cohort");
    }
    if (calls.length > 2) violations.push("generic route must use one or two planner provider calls");
    if (calls.some((call) => call.status !== "success")) {
      violations.push("all planner provider calls must succeed");
    }
    const phases = attempts.map((attempt) => attempt.phase).join(",");
    if (phases !== "candidate" && phases !== "discovery,candidate") {
      violations.push("generic route must be candidate or discovery,candidate");
    }
    const discovery = attempts.length === 2 ? attempts[0] : null;
    if (
      discovery !== null &&
      (discovery.phase !== "discovery" ||
        discovery.outcome !== "discovery_executed" ||
        discovery.led_to !== "continue" ||
        discovery.new_information.length === 0)
    ) {
      violations.push("generic discovery must execute once, continue, and add information");
    }
    const candidate = attempts.at(-1);
    if (candidate?.phase !== "candidate" || candidate.outcome !== "accepted" || candidate.led_to !== "done") {
      violations.push("generic route must finish with an accepted candidate");
    }
    if (noGainTurns > 0) violations.push("generic route must have zero no-gain discovery turns");
    if (repairTurns > 0) violations.push("generic route must have zero repair turns");
    if (redundantSelections.length > 0) {
      violations.push(`planner selected redundant state/list tools: ${redundantSelections.join(", ")}`);
    }
  }

  return {
    evidence_status: "complete",
    passed: violations.length === 0,
    route: violations.length > 0 ? "invalid" : "generic",
    provider_calls: calls.length,
    useful_discovery_turns: usefulDiscoveryTurns,
    no_gain_turns: noGainTurns,
    repair_turns: repairTurns,
    prompt_tokens: promptTokens.value,
    prompt_tokens_complete: promptTokens.complete,
    provider_cost: providerCost.value,
    provider_cost_complete: providerCost.complete,
    model_latency_ms: calls.reduce((total, call) => total + call.latency_ms, 0),
    selected_tools: selectedTools,
    violations,
  };
}

export function unavailablePlannerEfficiency(reason: string): PlannerEfficiency {
  return {
    evidence_status: "unavailable",
    passed: false,
    route: "unknown",
    provider_calls: null,
    useful_discovery_turns: null,
    no_gain_turns: null,
    repair_turns: null,
    prompt_tokens: null,
    prompt_tokens_complete: false,
    provider_cost: null,
    provider_cost_complete: false,
    model_latency_ms: null,
    selected_tools: [],
    violations: [`planner audit evidence unavailable: ${reason.slice(0, 500)}`],
  };
}

/** Return a failure only when efficiency is the primary failure for this run. */
export function plannerEfficiencyAssertionFailure(
  efficiency: PlannerEfficiency,
  hardError: string | null,
): string | null {
  if (hardError !== null || efficiency.passed) return null;
  if (efficiency.evidence_status === "unavailable") {
    return `planner efficiency evidence unavailable: ${efficiency.violations.join("; ")}`;
  }
  return efficiency.violations.join("; ") || "planner efficiency acceptance failed";
}

// A composition node as returned in CompositionStateResponse.nodes (top-level
// list; each carries id/node_type/plugin). Confirmed shape:
// src/elspeth/web/sessions/schemas.py:224 (nodes: CompositionObjectList) and the
// mocked tutorial.spec.ts compositionState ({id, node_type, plugin, ...}).
export interface CompositionNode { id: string; node_type?: string | null; plugin?: string | null; }
export async function fetchComposition(ctx: APIRequestContext, sid: string): Promise<{
  composer_meta: Record<string, unknown> | null;
  nodes: CompositionNode[];
  sourceInputKeys: string[];
  raw: unknown;
}> {
  // Real route is GET /api/sessions/{sid}/state (CompositionStateResponse, top-level
  // composer_meta + nodes + source), NOT the plan's /composition. Confirmed in
  // src/elspeth/web/sessions/routes/composer.py:1101 (get_current_state). The route
  // returns CompositionStateResponse | None, so body itself may be null.
  const r = await ctx.get(`/api/sessions/${sid}/state`);
  if (!r.ok()) throw new Error(`composition failed ${r.status()}`);
  const body = await r.json();
  const nodes = (Array.isArray(body?.nodes) ? body.nodes : []) as CompositionNode[];
  // Input columns = the keys of the source's first seeded row. These are the
  // user/source-supplied fields (e.g. {url}); the LLM extraction writes NEW keys.
  // Used by the dim-(d) substance check to target the extracted attribute rather
  // than scanning input columns. Grounded in compositionState.source.options.rows.
  const srcRows = (body?.source as { options?: { rows?: unknown } } | null)?.options?.rows;
  const firstRow = Array.isArray(srcRows) && srcRows.length > 0 ? srcRows[0] : null;
  const sourceInputKeys =
    firstRow && typeof firstRow === "object" ? Object.keys(firstRow as Record<string, unknown>) : [];
  return {
    composer_meta: (body?.composer_meta as Record<string, unknown> | null) ?? null,
    nodes,
    sourceInputKeys,
    raw: body,
  };
}

// The id of the web-scrape transform node in the composed DAG. The scrape is a
// TRANSFORM-node call, not an operation (operations are only source_load /
// sink_write / runtime_preflight — core/landscape/schema.py:356). We match on the
// plugin name; fall back to an id/plugin substring for older composer output.
export function scrapeNodeId(nodes: CompositionNode[]): string | null {
  const byPlugin = nodes.find((n) => (n.plugin ?? "").toLowerCase() === "web_scrape");
  if (byPlugin) return byPlugin.id;
  const bySubstr = nodes.find(
    (n) => /scrape|fetch/i.test(n.plugin ?? "") || /scrape|fetch/i.test(n.id ?? ""),
  );
  return bySubstr?.id ?? null;
}

export async function startRealRun(ctx: APIRequestContext, sid: string): Promise<string> {
  const r = await ctx.post(`/api/sessions/${sid}/execute`);
  if (!r.ok()) throw new Error(`execute failed ${r.status()}: ${await r.text()}`);
  return (await r.json()).run_id as string;
}

const TERMINAL = new Set(["completed", "completed_with_failures", "failed", "empty", "cancelled"]);
export async function pollRunTerminal(ctx: APIRequestContext, rid: string, timeoutMs = 240_000): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const r = await ctx.get(`/api/runs/${rid}`);
    if (!r.ok()) throw new Error(`status failed ${r.status()}`);
    const status = (await r.json()).status as string;
    if (TERMINAL.has(status)) return status;
    if (Date.now() > deadline) throw new Error(`run ${rid} did not reach terminal in ${timeoutMs}ms`);
    await new Promise((res) => setTimeout(res, 3000));
  }
}

// One per-token node-state in the diagnostics projection. Confirmed shape
// (src/elspeth/web/execution/schemas.py:573 RunDiagnosticNodeState and a real
// Landscape run): {state_id, token_id, node_id, status, error, attempt, ...}.
// status vocabulary is open|pending|completed|failed (Landscape node_states).
export interface DiagNodeState { token_id: string; node_id: string; status: string; error: unknown | null; attempt?: number; }
export interface DiagToken { token_id: string; row_id: string; states: DiagNodeState[]; }
export interface DiagOperation { node_id: string; operation_type: string; status: string; error_message: string | null; }
export async function fetchDiagnostics(ctx: APIRequestContext, rid: string): Promise<{
  operations: DiagOperation[];
  tokens: DiagToken[];
  failureDetail: { operation_type: string; error_message: string } | null;
}> {
  const r = await ctx.get(`/api/runs/${rid}/diagnostics`);
  if (!r.ok()) throw new Error(`diagnostics failed ${r.status()}`);
  const body = await r.json();
  return {
    operations: body.operations ?? [],
    tokens: body.tokens ?? [],
    failureDetail: body.failure_detail
      ? {
          operation_type: body.failure_detail.operation_type ?? "",
          error_message: body.failure_detail.error_message ?? "",
        }
      : null,
  };
}

// Reachability (spec §6, mechanical). The web scrape is a transform-node CALL,
// NOT an operation, so it is recorded in tokens[].states[] under the scrape
// node id — NOT in the operations table (which only ever holds source_load /
// sink_write / runtime_preflight). We therefore count DISTINCT rows whose scrape
// node-state succeeded, scoped to the composed DAG's scrape node id.
//
// Negative success test: a state counts as a successful scrape unless its status
// is "failed" or it carries an error. This is robust to an unexpected-but-
// successful status string (a positive status === "completed" test would read
// any such state as a failure and recreate the always-0 defect). Dedupe by
// token (a row can have multiple scrape states across retry attempts).
export function reachableSourceCount(tokens: DiagToken[], scrapeNodeId: string | null): number {
  // Match scrape states by exact composition node id when we have it. INSURANCE:
  // the composition node `.id` (from /state) and the runtime `node_id` (in
  // node_states) are expected to be the same string (real ids are descriptive,
  // hash-suffixed, e.g. "transform_scrape_pages_<hash>" — confirmed on a live
  // run), but if the exact match finds NO scrape state at all we fall back to a
  // /scrape|fetch/i substring on node_id. Without this fallback an id-format
  // mismatch would silently return 0 for every run and recreate the always-0
  // defect that compile/--list cannot catch.
  const matchesExact = (nid: string) => scrapeNodeId !== null && nid === scrapeNodeId;
  const matchesSubstr = (nid: string) => /scrape|fetch/i.test(nid);
  const anyExact = tokens.some((t) => (t.states ?? []).some((s) => matchesExact(s.node_id)));
  const match = anyExact ? matchesExact : matchesSubstr;

  const reachableTokens = new Set<string>();
  for (const tok of tokens) {
    for (const st of tok.states ?? []) {
      if (!match(st.node_id)) continue;
      const failed = st.status === "failed" || (st.error !== null && st.error !== undefined);
      if (!failed) reachableTokens.add(tok.token_id);
    }
  }
  return reachableTokens.size;
}
