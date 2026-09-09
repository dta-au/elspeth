// Playwright glue for the per-transition tutorial ledger (harness/transition-ledger.ts).
//
// Intercepts every transition request (POST guided/start|respond|chat and
// POST /api/tutorial/run) with page.route, forwards it to the deployment, and
// HOLDS the response back from the browser until the backend's durable audit
// rows have been re-read. The browser cannot fire the next gesture before the
// held response arrives, so every audit row first seen after a request is the
// settlement of THAT request — attribution by row identity, not by clock.
//
// Gestures are logged by the driver through `gesture(label)`; the ones logged
// since the previous transition's response belong to the next transition (the
// last of them is the one that fired it).

import type { APIRequestContext, APIResponse, Page, Route } from "@playwright/test";

import {
  TRANSITION_LEDGER_SCHEMA,
  attributeFreshRows,
  classifyTransitionRequest,
  ledgerTotals,
  ledgerViolations,
  sessionIdFromTransitionUrl,
  summarizeLlmAuditRows,
  transitionViolations,
  unavailableTransitionEvidence,
  type LedgerGesture,
  type LlmAuditRow,
  type TransitionEndpoint,
  type TransitionEvidence,
  type TransitionLedger,
  type TransitionLedgerEntry,
  type TransitionRequestView,
  type TransitionResponseView,
} from "../harness/transition-ledger";
import { fetchLlmAuditMessages } from "./tutorial-harness";

// A guided transition can legitimately take minutes (two multi-minute planner
// runs on the pre-remediation tutorial); route.fetch's 30 s default would
// abort the walk. Matches the driver's own 900 s walk deadline.
const TRANSITION_FETCH_TIMEOUT_MS = 900_000;
// How long finalize() waits for an in-flight transition before reporting it
// as a gap instead of blocking the run record on the fetch timeout above.
const FINALIZE_GRACE_MS = 5_000;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function summarizeRequest(endpoint: TransitionEndpoint, body: unknown): TransitionRequestView {
  const view: TransitionRequestView = {
    start_profile: null,
    respond_shape: null,
    control_signal: null,
    chat_message_chars: null,
  };
  if (!isRecord(body)) return view;
  if (endpoint === "guided/start") {
    view.start_profile = typeof body.profile === "string" ? body.profile : null;
  } else if (endpoint === "guided/respond") {
    const arms = (["chosen", "custom_inputs", "edited_values"] as const).filter(
      (arm) => body[arm] !== null && body[arm] !== undefined,
    );
    view.control_signal = typeof body.control_signal === "string" ? body.control_signal : null;
    view.respond_shape = arms.length > 0 ? arms.join("+") : view.control_signal === null ? "empty" : "control_signal";
  } else if (endpoint === "guided/chat") {
    view.chat_message_chars = typeof body.message === "string" ? body.message.length : null;
  }
  return view;
}

function summarizeResponse(
  endpoint: TransitionEndpoint,
  status: number | null,
  body: unknown,
  previousTurnToken: string | null,
): { view: TransitionResponseView; turnToken: string | null | undefined } {
  const view: TransitionResponseView = {
    status,
    step_after: null,
    next_turn_type: null,
    new_turn_occurrence: false,
    terminal: null,
    assistant_message_kind: null,
    run_id: null,
  };
  if (!isRecord(body)) return { view, turnToken: undefined };
  if (endpoint === "tutorial/run") {
    view.run_id = typeof body.run_id === "string" ? body.run_id : null;
    return { view, turnToken: undefined };
  }
  const session = body.guided_session;
  if (isRecord(session) && typeof session.step === "string") view.step_after = session.step;
  const terminal = body.terminal;
  if (isRecord(terminal) && typeof terminal.kind === "string") view.terminal = terminal.kind;
  if (typeof body.assistant_message_kind === "string") view.assistant_message_kind = body.assistant_message_kind;
  const nextTurn = body.next_turn;
  let turnToken: string | null = null;
  if (isRecord(nextTurn)) {
    if (typeof nextTurn.type === "string") view.next_turn_type = nextTurn.type;
    if (typeof nextTurn.turn_token === "string") turnToken = nextTurn.turn_token;
  }
  view.new_turn_occurrence = turnToken !== null && turnToken !== previousTurnToken;
  return { view, turnToken };
}

export interface TransitionLedgerRecorderOptions {
  legacyAutoRun: boolean;
}

export class TransitionLedgerRecorder {
  private readonly t0 = Date.now();
  private readonly entries: TransitionLedgerEntry[] = [];
  private pendingGestures: LedgerGesture[] = [];
  private readonly knownIds = new Set<string>();
  private afterUnavailable = false;
  private lastRespondedAt: number | null = null;
  private lastTurnToken: string | null = null;
  private phase: string | null = null;
  private bundle: string | null = null;
  // Route handlers run concurrently per request; chain them so the pending
  // gesture list and the known-row set are touched by one transition at a time.
  private chain: Promise<void> = Promise.resolve();
  private inFlight = 0;

  sessionId: string | null = null;

  constructor(
    private readonly page: Page,
    private readonly ctx: APIRequestContext,
    private readonly options: TransitionLedgerRecorderOptions,
  ) {}

  async install(): Promise<void> {
    await this.page.route(
      (url) => classifyTransitionRequest(url.href, "POST") !== null,
      (route) => {
        const run = this.chain.then(() => this.handle(route));
        this.chain = run.catch(() => undefined);
        return run;
      },
    );
  }

  /** Record a driver gesture (a click the learner would have made). Log it
   *  BEFORE issuing the click: a click's request is intercepted before the
   *  click promise resolves, so a gesture logged afterwards lands on the NEXT
   *  transition (observed on the first baseline run — every gesture was one
   *  transition late). Retract it if the click then fails. */
  gesture(label: string): LedgerGesture {
    const gesture = { label, at_ms: Date.now() - this.t0 };
    this.pendingGestures.push(gesture);
    return gesture;
  }

  /** Withdraw a gesture whose click did not happen, if no transition has
   *  claimed it yet. */
  retract(gesture: LedgerGesture): void {
    this.pendingGestures = this.pendingGestures.filter((pending) => pending !== gesture);
  }

  /** The workflow-stepper label the driver last observed. */
  notePhase(label: string | null): void {
    this.phase = label;
  }

  /** The SPA bundle the deployment served (for cross-run comparison). */
  noteBundle(bundle: string | null): void {
    this.bundle = bundle;
  }

  private async handle(route: Route): Promise<void> {
    const request = route.request();
    const endpoint = classifyTransitionRequest(request.url(), request.method());
    if (endpoint === null) {
      await route.continue();
      return;
    }
    this.inFlight += 1;
    try {
      await this.record(route, endpoint);
    } finally {
      this.inFlight -= 1;
    }
  }

  private async record(route: Route, endpoint: TransitionEndpoint): Promise<void> {
    const request = route.request();
    const sid = sessionIdFromTransitionUrl(request.url());
    if (sid !== null && this.sessionId === null) this.sessionId = sid;
    const ordinal = this.entries.length + 1;
    const gestures = this.pendingGestures;
    this.pendingGestures = [];
    const requestedAt = Date.now() - this.t0;
    let requestBody: unknown = null;
    try {
      requestBody = request.postDataJSON();
    } catch {
      requestBody = null;
    }
    const base = {
      ordinal,
      endpoint,
      gesture: gestures.at(-1)?.label ?? null,
      gestures,
      gesture_count: gestures.length,
      phase_before: this.phase,
      request: summarizeRequest(endpoint, requestBody),
      requested_at_ms: requestedAt,
      since_previous_ms: this.lastRespondedAt === null ? null : requestedAt - this.lastRespondedAt,
    };

    let response: APIResponse;
    try {
      response = await route.fetch({ timeout: TRANSITION_FETCH_TIMEOUT_MS });
    } catch (error) {
      const failed = {
        ...base,
        response: summarizeResponse(endpoint, null, null, this.lastTurnToken).view,
        responded_at_ms: null,
        wall_clock_ms: null,
        error: (error instanceof Error ? error.message : String(error)).slice(0, 500),
        evidence: unavailableTransitionEvidence("request failed before a response"),
      };
      this.entries.push({ ...failed, violations: transitionViolations(failed) });
      this.afterUnavailable = true;
      await route.abort("failed").catch(() => undefined);
      return;
    }
    const respondedAt = Date.now() - this.t0;
    let responseBody: unknown = null;
    try {
      responseBody = await response.json();
    } catch {
      responseBody = null;
    }
    const { view, turnToken } = summarizeResponse(endpoint, response.status(), responseBody, this.lastTurnToken);
    if (turnToken !== undefined && response.ok()) this.lastTurnToken = turnToken;

    // The durable read happens BEFORE the browser sees this response.
    const evidence = await this.readEvidence();
    const partial = {
      ...base,
      response: view,
      responded_at_ms: respondedAt,
      wall_clock_ms: respondedAt - requestedAt,
      error: null,
      evidence,
    };
    this.entries.push({ ...partial, violations: transitionViolations(partial) });
    this.lastRespondedAt = respondedAt;
    // The page may already be closed when a late response lands (walk deadline
    // tripped mid-request); the entry above is still the record of it.
    await route.fulfill({ response }).catch(() => undefined);
  }

  private async readEvidence(): Promise<TransitionEvidence> {
    if (this.sessionId === null) {
      this.afterUnavailable = true;
      return unavailableTransitionEvidence("session id not yet observed on a guided request");
    }
    try {
      const rows = summarizeLlmAuditRows(await fetchLlmAuditMessages(this.ctx, this.sessionId));
      const evidence = attributeFreshRows(this.knownIds, rows, { afterUnavailable: this.afterUnavailable });
      for (const id of evidence.row_ids) this.knownIds.add(id);
      this.afterUnavailable = false;
      return evidence;
    } catch (error) {
      this.afterUnavailable = true;
      return unavailableTransitionEvidence(error instanceof Error ? error.message : String(error));
    }
  }

  /** Close the ledger: one last durable read for the unattributed counts.
   *  A transition still in flight (the walk deadline tripped mid-request) is
   *  given a short grace, then reported as a gap rather than awaited for up to
   *  the fetch timeout. */
  async finalize(): Promise<TransitionLedger> {
    await Promise.race([
      this.chain,
      new Promise<void>((resolve) => setTimeout(resolve, FINALIZE_GRACE_MS)),
    ]);
    const inFlight = this.inFlight;
    let finalRows: LlmAuditRow[] = [];
    let finalRead: TransitionLedger["final_read"] = { status: "complete", reason: null };
    if (this.sessionId === null) {
      finalRead = { status: "unavailable", reason: "session id never observed" };
    } else {
      try {
        finalRows = summarizeLlmAuditRows(await fetchLlmAuditMessages(this.ctx, this.sessionId));
      } catch (error) {
        finalRead = {
          status: "unavailable",
          reason: (error instanceof Error ? error.message : String(error)).slice(0, 500),
        };
      }
    }
    // Snapshot: a late in-flight transition may still append after this returns.
    const entries = [...this.entries];
    const postGestures = [...this.pendingGestures];
    const totals = ledgerTotals(entries, postGestures, finalRows);
    return {
      schema: TRANSITION_LEDGER_SCHEMA,
      deployment: { bundle: this.bundle, legacy_auto_run: this.options.legacyAutoRun },
      session_id: this.sessionId,
      entries,
      post_gestures: postGestures,
      final_read: finalRead,
      in_flight_at_finalize: inFlight,
      totals,
      violations: ledgerViolations(entries, totals, finalRead, inFlight),
    };
  }
}
