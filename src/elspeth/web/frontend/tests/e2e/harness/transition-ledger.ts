// Per-TRANSITION tutorial ledger (elspeth-f191ba494a; closes the granularity
// gap in elspeth-515096e18c).
//
// The per-walk planner-efficiency gate (helpers/tutorial-harness.ts) counts
// provider calls over the whole guided walk. That granularity hid the
// b073d248e server-authored sketch for 26 days: the bypass removed the
// provider from ONE transition (rootless step-2 -> 3) while the walk still
// carried one call (the frozen-prompt revision), so "zero provider calls" never
// fired. This module attributes provider calls to the individual guided
// transition that produced them, so a transition that emits pipeline
// structure with no provider call is visible on its own line.
//
// Evidence discipline: provider calls come from the backend's durable audit
// rows (role="audit" llm_call_audit / planner_attempt_audit envelopes on the
// session's chat_messages, exposed by GET /messages?include_llm_audit=true),
// never from client-side timing. Attribution is by ROW IDENTITY: the recorder
// (helpers/transition-ledger-recorder.ts) holds each guided HTTP response back
// from the browser until it has re-read the durable rows, so every new row id
// belongs unambiguously to the transition whose settlement just committed it
// (guided settlements write their audit cohort inside the request; the
// response is not sent until the cohort is durable).
//
// Pure module: no Playwright, no I/O. Unit-tested in transition-ledger.test.ts.

export const TRANSITION_LEDGER_SCHEMA = "transition-ledger/1";

/** The HTTP boundaries that constitute a tutorial transition. */
export type TransitionEndpoint =
  | "guided/start"
  | "guided/respond"
  | "guided/chat"
  | "tutorial/run";

const GUIDED_ENDPOINT = /\/api\/sessions\/([0-9a-f-]{36})\/guided\/(start|respond|chat)(?:[?#]|$)/i;
const TUTORIAL_RUN_ENDPOINT = /\/api\/tutorial\/run(?:[?#]|$)/i;

/** Classify a browser request as a transition boundary, or null. POST only;
 *  `guided/start/{op}/reconcile` and GET /guided probes are not transitions. */
export function classifyTransitionRequest(url: string, method: string): TransitionEndpoint | null {
  if (method !== "POST") return null;
  const guided = GUIDED_ENDPOINT.exec(url);
  if (guided !== null) {
    const verb = guided[2].toLowerCase();
    if (verb === "start") return "guided/start";
    if (verb === "respond") return "guided/respond";
    return "guided/chat";
  }
  if (TUTORIAL_RUN_ENDPOINT.test(url)) return "tutorial/run";
  return null;
}

export function sessionIdFromTransitionUrl(url: string): string | null {
  const guided = GUIDED_ENDPOINT.exec(url);
  return guided === null ? null : guided[1].toLowerCase();
}

// ── Durable audit rows ────────────────────────────────────────────────────────

export interface LlmAuditRow {
  id: string;
  kind: "llm_call" | "planner_attempt";
  /** Non-null only on plan_pipeline calls; ordinal 1 opens a planner run. */
  planner_call_ordinal: number | null;
  status: string | null;
  latency_ms: number | null;
  /** planner_attempt rows only. */
  phase: string | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireId(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) throw new Error(`${label} must carry a non-empty id`);
  return value;
}

function optionalNonNegativeInteger(value: unknown, label: string): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
    throw new Error(`${label} must be a non-negative integer or null`);
  }
  return value;
}

/**
 * Reduce the audit-grade message projection to the rows the ledger attributes.
 * Conversation rows are skipped; an audit row with an unknown envelope kind is
 * an error (fail closed — the view is a closed vocabulary).
 */
export function summarizeLlmAuditRows(messages: unknown): LlmAuditRow[] {
  if (!Array.isArray(messages)) throw new Error("audit messages response must be an array");
  const rows: LlmAuditRow[] = [];
  for (const [index, raw] of messages.entries()) {
    if (!isRecord(raw)) throw new Error(`message[${index}] must be an object`);
    if (raw.role !== "audit") continue;
    const toolCalls = raw.tool_calls;
    if (toolCalls === null || toolCalls === undefined) continue;
    if (!Array.isArray(toolCalls)) throw new Error(`message[${index}].tool_calls must be an array or null`);
    const id = requireId(raw.id, `message[${index}]`);
    for (const [envelopeIndex, rawEnvelope] of toolCalls.entries()) {
      const label = `message[${index}].tool_calls[${envelopeIndex}]`;
      if (!isRecord(rawEnvelope)) throw new Error(`${label} must be an object`);
      const kind = rawEnvelope._kind;
      if (kind === "llm_call_audit") {
        if (!isRecord(rawEnvelope.call)) throw new Error(`${label}.call must be an object`);
        const call = rawEnvelope.call;
        rows.push({
          id,
          kind: "llm_call",
          planner_call_ordinal: optionalNonNegativeInteger(call.planner_call_ordinal, `${label}.call.planner_call_ordinal`),
          status: typeof call.status === "string" ? call.status : null,
          latency_ms: optionalNonNegativeInteger(call.latency_ms, `${label}.call.latency_ms`),
          phase: null,
        });
      } else if (kind === "planner_attempt_audit") {
        if (!isRecord(rawEnvelope.attempt)) throw new Error(`${label}.attempt must be an object`);
        const attempt = rawEnvelope.attempt;
        rows.push({
          id,
          kind: "planner_attempt",
          planner_call_ordinal: optionalNonNegativeInteger(attempt.planner_call_ordinal, `${label}.attempt.planner_call_ordinal`),
          status: null,
          latency_ms: null,
          phase: typeof attempt.phase === "string" ? attempt.phase : null,
        });
      } else {
        throw new Error(`${label}._kind has unknown value ${String(kind)}`);
      }
    }
  }
  return rows;
}

// ── Per-transition evidence ───────────────────────────────────────────────────

export interface TransitionEvidence {
  status: "complete" | "unavailable";
  /** Why the durable rows could not be read for this transition. */
  reason: string | null;
  /** Every llm_call row attributed here: planner, chat drivers, advisors. */
  provider_calls: number;
  /** llm_call rows carrying a planner_call_ordinal (plan_pipeline calls). */
  planner_calls: number;
  /** Planner runs opened here: llm_call rows with planner_call_ordinal === 1. */
  planner_runs: number;
  /** llm_call rows whose status is not "success". */
  failed_calls: number;
  /** Sum of latency_ms over the attributed llm_call rows (the model time). */
  model_latency_ms: number;
  /** planner_attempt phases in row order (discovery / candidate / repair ...). */
  attempt_phases: string[];
  /** Ids of every attributed row (llm_call and planner_attempt). */
  row_ids: string[];
  /** True when a prior transition's read failed, so rows the server wrote for
   *  it were first seen — and are counted — here. */
  includes_rows_from_unavailable_transition: boolean;
}

export function unavailableTransitionEvidence(reason: string): TransitionEvidence {
  return {
    status: "unavailable",
    reason: reason.slice(0, 500),
    provider_calls: 0,
    planner_calls: 0,
    planner_runs: 0,
    failed_calls: 0,
    model_latency_ms: 0,
    attempt_phases: [],
    row_ids: [],
    includes_rows_from_unavailable_transition: false,
  };
}

/** Attribute the rows not yet seen to the transition that just settled. */
export function attributeFreshRows(
  knownIds: ReadonlySet<string>,
  rows: readonly LlmAuditRow[],
  options: { afterUnavailable: boolean },
): TransitionEvidence {
  const fresh = rows.filter((row) => !knownIds.has(row.id));
  const calls = fresh.filter((row) => row.kind === "llm_call");
  return {
    status: "complete",
    reason: null,
    provider_calls: calls.length,
    planner_calls: calls.filter((row) => row.planner_call_ordinal !== null).length,
    planner_runs: calls.filter((row) => row.planner_call_ordinal === 1).length,
    failed_calls: calls.filter((row) => row.status !== "success").length,
    model_latency_ms: calls.reduce((total, row) => total + (row.latency_ms ?? 0), 0),
    attempt_phases: fresh.filter((row) => row.kind === "planner_attempt").map((row) => row.phase ?? "unknown"),
    row_ids: fresh.map((row) => row.id),
    includes_rows_from_unavailable_transition: options.afterUnavailable && fresh.length > 0,
  };
}

// ── Ledger entries ────────────────────────────────────────────────────────────

export interface LedgerGesture {
  label: string;
  /** Milliseconds since the recorder started (client clock; gesture pacing only). */
  at_ms: number;
}

export interface TransitionResponseView {
  /** HTTP status, or null when the request never produced a response. */
  status: number | null;
  /** guided_session.step after settlement (server-authored). */
  step_after: string | null;
  /** next_turn.type after settlement (server-authored). */
  next_turn_type: string | null;
  /** True when next_turn.turn_token differs from the previous transition's —
   *  i.e. the server emitted a NEW turn occurrence, not a re-render. */
  new_turn_occurrence: boolean;
  /** terminal.kind after settlement. */
  terminal: string | null;
  /** guided/chat only. */
  assistant_message_kind: string | null;
  /** tutorial/run only. */
  run_id: string | null;
}

export interface TransitionRequestView {
  /** guided/start: the workflow profile requested ("tutorial" | "live"). */
  start_profile: string | null;
  /** guided/respond: which action arm the client sent. */
  respond_shape: string | null;
  /** guided/respond: the control signal, when one was sent. */
  control_signal: string | null;
  /** guided/chat: message length only; the text itself is not recorded. */
  chat_message_chars: number | null;
}

export interface TransitionLedgerEntry {
  ordinal: number;
  endpoint: TransitionEndpoint;
  /** The driver gesture that fired this request (the last one logged). */
  gesture: string | null;
  /** Every gesture since the previous transition's response, firing one last. */
  gestures: LedgerGesture[];
  gesture_count: number;
  /** Workflow-stepper label read before the firing gesture (client view). */
  phase_before: string | null;
  request: TransitionRequestView;
  response: TransitionResponseView;
  requested_at_ms: number;
  responded_at_ms: number | null;
  /** Request start -> server response received, measured by the recorder. */
  wall_clock_ms: number | null;
  /** Previous transition's response -> this request (the human/driver gap). */
  since_previous_ms: number | null;
  /** Transport failure text when the request never got a response. */
  error: string | null;
  evidence: TransitionEvidence;
  violations: string[];
}

/**
 * The per-transition invariant that the walk-level gate cannot see: a
 * transition that hands the learner a NEW pipeline proposal must have paid a
 * planner call for it in THAT transition. Zero planner calls behind a fresh
 * propose_pipeline turn is the b073d248e shape (server-authored structure).
 * Unavailable evidence is a violation too — the check cannot be vacuously
 * green (ZERO ROWS = FAIL).
 */
export function transitionViolations(entry: Omit<TransitionLedgerEntry, "violations">): string[] {
  const violations: string[] = [];
  const label = `transition ${entry.ordinal} (${entry.endpoint}${entry.gesture === null ? "" : `, ${entry.gesture}`})`;
  if (entry.endpoint !== "tutorial/run" && entry.evidence.status === "unavailable") {
    violations.push(`${label}: durable provider-call evidence unavailable: ${entry.evidence.reason ?? "unknown"}`);
  }
  const ok = entry.response.status !== null && entry.response.status >= 200 && entry.response.status < 300;
  if (
    ok &&
    entry.evidence.status === "complete" &&
    entry.response.next_turn_type === "propose_pipeline" &&
    entry.response.new_turn_occurrence &&
    entry.evidence.planner_calls === 0
  ) {
    violations.push(
      `${label}: emitted a new propose_pipeline turn with zero planner provider calls attributed to it (server-authored structure?)`,
    );
  }
  return violations;
}

// ── Whole-ledger totals ───────────────────────────────────────────────────────

export interface TransitionLedgerTotals {
  transitions: number;
  /** All gestures, including the welcome click and the post-run bookends. */
  gestures: number;
  /** Gestures after guided/start up to and including the one that started
   *  the run — the review's "gestures to a reviewable pipeline" plus Run. */
  gestures_to_run: number | null;
  provider_calls: number;
  planner_calls: number;
  planner_runs: number;
  failed_calls: number;
  model_latency_ms: number;
  /** Sum of guided transition durations (start/respond/chat), excluding the run. */
  guided_wall_clock_ms: number;
  /** First build gesture -> run request (the review's wall clock to "ready"). */
  wall_clock_to_run_ms: number | null;
  run_wall_clock_ms: number | null;
  /** Rows present at the end that no transition claimed. */
  unattributed_provider_calls: number;
  unattributed_planner_calls: number;
  /** Transitions whose durable read failed. */
  attribution_gaps: number;
}

export interface TransitionLedger {
  schema: typeof TRANSITION_LEDGER_SCHEMA;
  deployment: {
    /** The SPA bundle path the deployment served, for cross-run comparison. */
    bundle: string | null;
    /** True when the harness was told the deployment predates the explicit
     *  Run button (593cad72c) and the run auto-fires on the last acknowledge. */
    legacy_auto_run: boolean;
  };
  session_id: string | null;
  entries: TransitionLedgerEntry[];
  /** Gestures after the last transition (audit story, graduation). */
  post_gestures: LedgerGesture[];
  /** The end-of-walk durable read that the unattributed counts derive from. */
  final_read: { status: "complete" | "unavailable"; reason: string | null };
  /** Transitions whose response had not arrived when the ledger closed (the
   *  walk deadline tripped mid-request); they have no entry. */
  in_flight_at_finalize: number;
  totals: TransitionLedgerTotals;
  violations: string[];
}

export function ledgerTotals(
  entries: readonly TransitionLedgerEntry[],
  postGestures: readonly LedgerGesture[],
  finalRows: readonly LlmAuditRow[],
): TransitionLedgerTotals {
  const startIndex = entries.findIndex((entry) => entry.endpoint === "guided/start");
  const runIndex = entries.findIndex((entry) => entry.endpoint === "tutorial/run");
  const buildEntries = entries.filter(
    (_entry, index) => index > startIndex && (runIndex === -1 || index <= runIndex),
  );
  const firstBuildGesture = buildEntries.flatMap((entry) => entry.gestures)[0] ?? null;
  const run = runIndex === -1 ? null : entries[runIndex];
  const claimed = new Set(entries.flatMap((entry) => entry.evidence.row_ids));
  const unattributedCalls = finalRows.filter((row) => row.kind === "llm_call" && !claimed.has(row.id));
  const guidedEntries = entries.filter((entry) => entry.endpoint !== "tutorial/run");
  return {
    transitions: entries.length,
    gestures: entries.reduce((total, entry) => total + entry.gesture_count, 0) + postGestures.length,
    gestures_to_run:
      startIndex === -1 || runIndex === -1
        ? null
        : buildEntries.reduce((total, entry) => total + entry.gesture_count, 0),
    provider_calls: entries.reduce((total, entry) => total + entry.evidence.provider_calls, 0),
    planner_calls: entries.reduce((total, entry) => total + entry.evidence.planner_calls, 0),
    planner_runs: entries.reduce((total, entry) => total + entry.evidence.planner_runs, 0),
    failed_calls: entries.reduce((total, entry) => total + entry.evidence.failed_calls, 0),
    model_latency_ms: entries.reduce((total, entry) => total + entry.evidence.model_latency_ms, 0),
    guided_wall_clock_ms: guidedEntries.reduce((total, entry) => total + (entry.wall_clock_ms ?? 0), 0),
    wall_clock_to_run_ms:
      firstBuildGesture === null || run === null ? null : run.requested_at_ms - firstBuildGesture.at_ms,
    run_wall_clock_ms: run?.wall_clock_ms ?? null,
    unattributed_provider_calls: unattributedCalls.length,
    unattributed_planner_calls: unattributedCalls.filter((row) => row.planner_call_ordinal !== null).length,
    attribution_gaps: guidedEntries.filter((entry) => entry.evidence.status === "unavailable").length,
  };
}

export function ledgerViolations(
  entries: readonly TransitionLedgerEntry[],
  totals: TransitionLedgerTotals,
  finalRead: TransitionLedger["final_read"],
  inFlightAtFinalize: number,
): string[] {
  const violations = entries.flatMap((entry) => entry.violations);
  if (totals.unattributed_planner_calls > 0) {
    violations.push(
      `${totals.unattributed_planner_calls} planner provider call(s) were recorded outside every observed transition`,
    );
  }
  if (finalRead.status === "unavailable") {
    // Without the final read the unattributed counts are vacuous zeros.
    violations.push(`final durable read unavailable: ${finalRead.reason ?? "unknown"}`);
  }
  if (inFlightAtFinalize > 0) {
    violations.push(`${inFlightAtFinalize} transition(s) still in flight when the ledger was finalized`);
  }
  return violations;
}

// ── Rendering ─────────────────────────────────────────────────────────────────

function ms(value: number | null): string {
  return value === null ? "-" : `${(value / 1000).toFixed(1)}s`;
}

/** Markdown table for the console, the report, and the tracker comment. */
export function renderLedgerMarkdown(ledger: TransitionLedger): string {
  const lines = [
    "| # | endpoint | gesture | gestures | phase before → step after | next turn | provider calls | planner calls (runs) | model time | wall clock | since prev | notes |",
    "|---|---|---|---|---|---|---|---|---|---|---|---|",
  ];
  for (const entry of ledger.entries) {
    const notes: string[] = [];
    if (entry.response.terminal !== null) notes.push(`terminal=${entry.response.terminal}`);
    if (entry.response.assistant_message_kind !== null && entry.response.assistant_message_kind !== "assistant") {
      notes.push(entry.response.assistant_message_kind);
    }
    if (entry.evidence.status === "unavailable") notes.push("evidence unavailable");
    if (entry.evidence.includes_rows_from_unavailable_transition) notes.push("includes prior rows");
    if (entry.evidence.failed_calls > 0) notes.push(`${entry.evidence.failed_calls} failed call(s)`);
    if (entry.error !== null) notes.push(`error: ${entry.error}`);
    for (const violation of entry.violations) notes.push(`VIOLATION: ${violation}`);
    lines.push(
      `| ${entry.ordinal} | ${entry.endpoint} | ${entry.gesture ?? "-"} | ${entry.gesture_count} | ${entry.phase_before ?? "-"} → ${entry.response.step_after ?? "-"} | ${entry.response.next_turn_type ?? "-"} | ${entry.evidence.provider_calls} | ${entry.evidence.planner_calls} (${entry.evidence.planner_runs}) | ${ms(entry.evidence.model_latency_ms)} | ${ms(entry.wall_clock_ms)} | ${ms(entry.since_previous_ms)} | ${notes.join("; ")} |`,
    );
  }
  const t = ledger.totals;
  lines.push("");
  lines.push(
    `Totals: ${t.transitions} transitions · ${t.gestures} gestures (${t.gestures_to_run ?? "-"} to run) · ${t.provider_calls} provider calls · ${t.planner_calls} planner calls in ${t.planner_runs} planner run(s) · model time ${ms(t.model_latency_ms)} · guided wall clock ${ms(t.guided_wall_clock_ms)} · first build gesture → run ${ms(t.wall_clock_to_run_ms)} · run ${ms(t.run_wall_clock_ms)} · unattributed ${t.unattributed_provider_calls} (${t.unattributed_planner_calls} planner) · gaps ${t.attribution_gaps}`,
  );
  if (ledger.post_gestures.length > 0) {
    lines.push(`Post-run gestures: ${ledger.post_gestures.map((gesture) => gesture.label).join(", ")}`);
  }
  if (ledger.in_flight_at_finalize > 0) {
    lines.push(`In flight at finalize: ${ledger.in_flight_at_finalize}`);
  }
  if (ledger.violations.length > 0) {
    lines.push(`Violations: ${ledger.violations.join("; ")}`);
  }
  return lines.join("\n");
}
