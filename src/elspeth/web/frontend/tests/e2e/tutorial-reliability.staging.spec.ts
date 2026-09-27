// Tutorial reliability battery (non-mocked staging spec).
//
// Drives the REAL first-run composer tutorial against elspeth.example.gov.au,
// resets between every run, grades each run on four dimensions (a/b/c/d), and
// writes one JSON RunRecord per run under
// tests/e2e/.harness-results/<batch_id>/run-NN.json (gitignored).
//
// Integrates plan Tasks 4 (skeleton flow + selectors), 5 (id/cache capture),
// 6 (grade a/b/c + dim-d raw material + write record), 7 (real-system re-run),
// and 8 (parameterized N-run battery with reset between).
//
// Scope discipline (spec §10): this harness only OBSERVES the normalization gap
// and cache-as-fakery; it does not fix them.
//
// Per-transition ledger (elspeth-f191ba494a / elspeth-515096e18c): every
// freeform compose and tutorial run transition is recorded with
// its gestures, provider calls, planner runs and wall clock, attributed from
// the backend's durable audit rows. The record's `transitions` field is the
// "before" column for the 2026-09-02 remediation plan (Phase 0 → Phase 5).

import { mkdirSync, writeFileSync } from "node:fs";

import { test, expect, type Locator, type Page } from "@playwright/test";

import {
  ASSUMPTION_RUBRIC,
  JUDGE_RUBRIC,
} from "./harness/prompt-and-rubric";
import { classifyOutcome, type StepSignal } from "./harness/classify";
import { renderLedgerMarkdown, type TransitionLedger } from "./harness/transition-ledger";
import type { RunRecord } from "./harness/types";
import { TransitionLedgerRecorder } from "./helpers/transition-ledger-recorder";
import {
  classifyPlannerEfficiency,
  fetchComposition,
  fetchDiagnostics,
  fetchInterpretationEvents,
  fetchPlannerAuditEvidence,
  harnessCtx,
  plannerEfficiencyAssertionFailure,
  reachableSourceCount,
  resetToFirstRun,
  cleanSessions,
  scrapeNodeId,
  unavailablePlannerEfficiency,
  type PlannerEfficiency,
} from "./helpers/tutorial-harness";

const BATCH_ID = process.env.HARNESS_BATCH_ID ?? "skeleton";
const BATCH_SIZE = Number(process.env.HARNESS_BATCH_SIZE ?? "1");
const ACKNOWLEDGEMENT_PRIMARY_ACTION_NAMES = [
  /^View prompt$/,
  /^Approve the LLM prompt template$/,
  /^Acknowledge/i,
];

type EfficiencyRunRecord = RunRecord & {
  efficiency: PlannerEfficiency;
  transitions: TransitionLedger | null;
};

// Acknowledge every pending interpretation card currently rendered, then
// return how many were acknowledged this pass.
//
// Post-redesign (acknowledge card-stack): the cards render in the pinned
// AcknowledgementStack. Most cards resolve through an "Acknowledge …" primary.
// Prompt-template cards are two-stage: the same primary first says
// "View prompt", then flips to "Approve the LLM prompt template". Drive those
// primary actions as first-class unblockers; otherwise the prompt review stays
// pending and "Confirm wiring" never enables.
//
// Every successful click is logged as a ledger gesture (the review's ledger
// counted View prompt / Approve / Acknowledge as learner gestures).
//
// Log the gesture BEFORE the click and retract it on failure: the request a
// click fires is intercepted before the click promise resolves, so logging
// afterwards attributes every gesture to the NEXT transition (the first
// baseline run recorded exactly that shift).
async function clickGesture(
  locator: Locator,
  label: string,
  ledger: TransitionLedgerRecorder | null,
): Promise<boolean> {
  const gesture = ledger?.gesture(label) ?? null;
  const clicked = await locator.click().then(() => true, () => false);
  if (!clicked && gesture !== null) ledger?.retract(gesture);
  return clicked;
}

async function resolveVisibleReviews(
  page: Page,
  ledger: TransitionLedgerRecorder | null = null,
): Promise<number> {
  const primaryButtons = ACKNOWLEDGEMENT_PRIMARY_ACTION_NAMES.map((name) =>
    page.getByRole("button", { name }),
  );
  const promptRegions = page.getByRole("region", {
    name: "Prompt template review",
  });
  let actions = 0;
  // Bounded inner loop: each acknowledged card unmounts, shrinking the list.
  // Prompt-template cards take two iterations (View prompt -> Approve).
  for (let guard = 0; guard < 12; guard++) {
    const regionCount = await promptRegions.count().catch(() => 0);
    for (let i = 0; i < regionCount; i++) {
      await promptRegions
        .nth(i)
        .evaluate((el) => {
          el.scrollTop = el.scrollHeight;
          el.dispatchEvent(new Event("scroll"));
        })
        .catch(() => {});
    }
    let clicked = false;
    for (const buttons of primaryButtons) {
      const total = await buttons.count().catch(() => 0);
      for (let i = 0; i < total; i++) {
        const btn = buttons.nth(i);
        if (await btn.isEnabled().catch(() => false)) {
          const label = ((await btn.textContent().catch(() => null)) ?? "").trim() || "Acknowledge";
          await clickGesture(btn, label, ledger);
          actions += 1;
          clicked = true;
          break;
        }
      }
      if (clicked) break;
    }
    if (!clicked) {
      await page.waitForTimeout(300);
    }
  }
  return actions;
}

/** Drive the fixed freeform brief through normal Composer review to Run. */
async function driveFreeformWalk(page: Page, ledger: TransitionLedgerRecorder): Promise<void> {
  await expect(page.getByRole("heading", { name: "Build with the Composer." })).toBeVisible({ timeout: 60_000 });
  ledger.notePhase("Build");
  const sendBrief = page.getByRole("button", { name: "Send tutorial brief" });
  await expect(sendBrief).toBeEnabled({ timeout: 30_000 });
  await clickGesture(sendBrief, "Send tutorial brief", ledger);

  const deadline = Date.now() + 900_000;
  while (Date.now() < deadline) {
    const ready = page.getByRole("button", { name: "Continue to Run" });
    if (await ready.isEnabled().catch(() => false)) {
      await clickGesture(ready, "Continue to Run", ledger);
      await expect(page.getByRole("heading", { name: /Ready to run/i })).toBeVisible({ timeout: 60_000 });
      return;
    }
    const accept = page.getByRole("region", { name: /Awaiting your decision/ })
      .getByRole("button", { name: /Accept proposal:/ }).first();
    if (await accept.isEnabled().catch(() => false)) {
      await clickGesture(accept, "Accept proposal", ledger);
      continue;
    }
    if (await resolveVisibleReviews(page, ledger) > 0) continue;
    const error = page.locator(".tutorial-error").first();
    if (await error.isVisible().catch(() => false)) {
      throw new Error(`freeform tutorial Build did not become runnable: ${(await error.textContent()) ?? "unknown"}`);
    }
    await page.waitForTimeout(1_000);
  }
  throw new Error("freeform tutorial Build never reached the Run turn before the deadline");
}

// Dimension (d) output-substance (spec §6): count rows that carry a meaningful,
// non-degenerate value in the LLM-EXTRACTED attribute — NOT in an input column.
//
// We do NOT hard-code the colour field key (it varies per composed pipeline).
// Instead we derive the input columns from the composition source (the keys of
// the first seeded source row, e.g. {url}) and treat every OTHER key in an
// output row as a candidate extraction field. We also exclude an obvious `html`
// key because the prompt strips HTML before the sink. A row is substantive iff
// at least one such extraction field holds a non-empty, non-degenerate string.
//
// The earlier version scanned EVERY value in the row, so any non-degenerate
// string anywhere (the URL, the agency name) made the row count — it measured
// "did we get a row" not "did the row carry a real extracted value", and the
// minSubstantiveRows check never bit. This targets the extraction output.
const DEGENERATE_VALUE = /cannot|unknown|n\/a|none|no clear|not (?:found|available|determined)/i;
const KNOWN_INPUT_KEYS = /^(?:url|source|html|html_content|raw_html|content|content_fingerprint)$/i;
function substantiveRowCount(
  rows: Array<Record<string, unknown>>,
  sourceInputKeys: string[],
): number {
  const inputKeys = new Set(sourceInputKeys.map((k) => k.toLowerCase()));
  return rows.filter((row) =>
    Object.entries(row).some(([key, v]) => {
      const k = key.toLowerCase();
      if (inputKeys.has(k) || KNOWN_INPUT_KEYS.test(k)) return false; // not an extraction field
      return typeof v === "string" && v.trim().length > 0 && !DEGENERATE_VALUE.test(v);
    }),
  ).length;
}

async function runOnce(page: Page, runIndex: number): Promise<void> {
  test.setTimeout(1_800_000); // walk (≤900s, one planner run) + draft-wait (≤420s) + run-wait (≤360s) + grading

  // --- per-run state (Task 5 capture targets; all consumed in the record) ---
  let sessionId: string | null = null;
  let tutorialRunId: string | null = null;
  let seededFromCache = false;
  let outputRows: Array<Record<string, unknown>> = [];
  let discardedRowCount = 0;

  // No separate real-system re-run: when the tutorial applies NO normalization it
  // already executed through the normal ExecutionService→Orchestrator path (re-
  // running the same session collides on output artifacts → FileExistsError), so
  // dim (b) derives from the tutorial run's own success + the normalization flag.
  // realsystemRunId stays null by construction (kept for the record schema).
  const realsystemRunId: string | null = null;

  let turnReached = 0; // increment after each turn's success
  let graduated = false;
  let hardError: string | null = null;

  // --- backend-grounded step signals (de-conflation; see harness/classify.ts) ---
  // Capture the compose (POST /api/sessions/{id}/messages) and run
  // (POST /api/tutorial/run) POSTs REGARDLESS of resp.ok(), plus whether each
  // request even responded before the deadline. Classification keys on these,
  // not on the Playwright timeout string (which always says "Timeout").
  const mkStep = (): StepSignal => ({
    fired: false,
    responded: false,
    status: null,
    bodyText: null,
    elapsedMs: null,
  });
  const step = { compose: mkStep(), run: mkStep() };
  const startMs: { compose: number | null; run: number | null } = { compose: null, run: null };
  // Build now submits one fixed brief through the ordinary freeform endpoint.
  const isCompose = (url: string, method: string) =>
    method === "POST" && /\/api\/sessions\/[0-9a-f-]{36}\/messages\b/i.test(url);
  const isRun = (url: string, method: string) =>
    method === "POST" && url.includes("/api/tutorial/run");

  page.on("request", (req) => {
    const url = req.url();
    const method = req.method();
    if (isCompose(url, method)) {
      // The first authoring POST is the fixed brief; later freeform amendments
      // are recorded by the ledger as separate transitions.
      if (!step.compose.fired) {
        step.compose.fired = true;
        startMs.compose = Date.now();
      }
    } else if (isRun(url, method)) {
      step.run.fired = true;
      startMs.run = Date.now();
    }
  });
  page.on("requestfailed", (req) => {
    // Transport failure (connection reset / abort) — record the failure text so
    // INFRA_BODY in the classifier can see it. responded stays false.
    const url = req.url();
    const method = req.method();
    const errText = req.failure()?.errorText ?? "connection failed";
    if (isCompose(url, method)) step.compose.bodyText ??= errText;
    else if (isRun(url, method)) step.run.bodyText ??= errText;
  });

  // Capture session id, tutorial run id, cache flag, output rows, AND the
  // compose/run step status+timing+error-body (Task 5 + Task 6 Step 2).
  page.on("response", async (resp) => {
    const url = resp.url();
    const method = resp.request().method();
    const compose = isCompose(url, method);
    const run = isRun(url, method);
    if (compose || run) {
      const tgt = compose ? step.compose : step.run;
      const start = compose ? startMs.compose : startMs.run;
      tgt.responded = true;
      tgt.status = resp.status();
      tgt.elapsedMs = start !== null ? Date.now() - start : null;
      if (!resp.ok()) tgt.bodyText = await resp.text().catch(() => null);
    }
    if (run && resp.ok()) {
      const body = await resp.json().catch(() => null);
      if (body) {
        tutorialRunId = body.run_id ?? null;
        seededFromCache = body.seeded_from_cache ?? false;
        const output = body.output ?? {};
        outputRows = Array.isArray(output.rows) ? output.rows : [];
        discardedRowCount = output.discarded_row_count ?? 0;
      }
    }
    const m = url.match(/\/api\/sessions\/([0-9a-f-]{36})\//i);
    if (m && !sessionId) sessionId = m[1];
  });

  // Per-transition ledger (elspeth-f191ba494a). Installed before navigation
  // so the freeform brief transition is the first authoring entry. It holds each
  // authoring response back from the browser until the backend's durable audit rows have
  // been re-read, which is what makes per-transition attribution exact.
  const ledgerCtx = await harnessCtx();
  const ledger = new TransitionLedgerRecorder(page, ledgerCtx);
  await ledger.install();
  let transitions: TransitionLedger | null = null;
  let ledgerError: string | null = null;

  try {
    await page.goto("/");
    await expect(
      page.getByRole("main", { name: /first-run tutorial/i }),
    ).toBeVisible();
    ledger.noteBundle(
      await page
        .evaluate(
          () =>
            document.querySelector<HTMLScriptElement>('script[src*="/assets/index-"]')?.getAttribute("src") ??
            null,
        )
        .catch(() => null),
    );

    // Welcome bookend → Start mounts the freeform Composer surface.
    // Bookend clicks log their gesture first (see clickGesture) and let a
    // click failure throw as before.
    ledger.gesture("Let's go");
    await page.getByRole("button", { name: "Let's go" }).click();
    turnReached = 1;

    // Build sends the fixed brief through ordinary freeform Composer and
    // reviews its proposal before the Run card appears.
    turnReached = 2;
    await driveFreeformWalk(page, ledger);
    expect(step.run.fired, "the tutorial run must not fire before Run is clicked").toBe(false);
    ledger.gesture("Run");
    await page.getByRole("button", { name: "Run", exact: true }).click();

    // Wait for completion, continue to the audit story. Headroom for
    // LLM-provider latency over the heavy 5-source canonical scenario plus
    // the wire-stage advisor sign-off.
    await expect(page.getByRole("button", { name: "Continue" })).toBeVisible({
      timeout: 420_000,
    });
    turnReached = 3;
    ledger.gesture("Continue (run complete)");
    await page.getByRole("button", { name: "Continue" }).click();
    turnReached = 4;

    // Audit story, continue.
    await expect(page.getByText(/This is the audit story/i)).toBeVisible();
    ledger.gesture("Continue (audit story)");
    await page.getByRole("button", { name: "Continue" }).click();
    turnReached = 5;

    // Graduation persists completion and opens the same freeform session.
    await expect(page.getByRole("heading", { name: "You're ready to use the composer." })).toBeVisible();
    ledger.gesture("Take me to the composer");
    await page
      .getByRole("button", { name: "Take me to the composer" })
      .click();
    turnReached = 6;

    await expect(page.getByLabel("Chat panel", { exact: true })).toBeVisible();
    turnReached = 7;
    graduated = true;

    // Id-capture assertions (Task 5 Step 2). The cache-bypass assertion is kept
    // advisory: the staged tutorial runs the canonical scenario, so a cache hit
    // is no longer a fault the way a stale FIXED_PROMPT compose would have been —
    // the run still executes through the normal path. We record seededFromCache
    // in the RunRecord for the judge.
    expect(sessionId, "session id captured").not.toBeNull();
    expect(tutorialRunId, "tutorial run id captured").not.toBeNull();

    // Dimension (b): NO separate re-run (it would collide on output artifacts).
    // When normalization did not fire, the tutorial run IS the real-system path;
    // dim (b) is derived in the finally block from graduated + rows + the
    // normalization flag (parity principle: a fired normalization = fault).
  } catch (e) {
    hardError = e instanceof Error ? e.message : String(e);
    throw e; // rethrow so Playwright captures trace/video for this failed run
  } finally {
    // Close the per-transition ledger first (its final durable read is what
    // the unattributed-call counts derive from), then release its context.
    try {
      transitions = await ledger.finalize();
    } catch (error) {
      ledgerError = error instanceof Error ? error.message : String(error);
    }
    await ledgerCtx.dispose().catch(() => undefined);

    // --- build + write the per-run RunRecord (Task 6 + Task 7) ---
    const ctx = await harnessCtx();
    const events = sessionId
      ? await fetchInterpretationEvents(ctx, sessionId).catch(() => [])
      : [];
    const comp = sessionId
      ? await fetchComposition(ctx, sessionId).catch(() => ({
          composer_meta: null,
          nodes: [],
          sourceInputKeys: [],
          raw: null,
        }))
      : { composer_meta: null, nodes: [], sourceInputKeys: [], raw: null };
    const scrapeNode = scrapeNodeId(comp.nodes);
    const diag = tutorialRunId
      ? await fetchDiagnostics(ctx, tutorialRunId).catch(() => ({
          operations: [],
          tokens: [],
          failureDetail: null,
        }))
      : { operations: [], tokens: [], failureDetail: null };
    // expectVerify entries may be an InterpretationKind (e.g. "pipeline_decision")
    // OR a user_term (e.g. "prompt_injection_shield_recommendation", the shield
    // review's discriminating signal whose kind is pipeline_decision). Match on
    // either, so the shield review is recognised by its user_term. Kinds are enum
    // values and user_terms are field-path strings, so the OR cannot false-match.
    const underFlagged = ASSUMPTION_RUBRIC.expectVerify.filter(
      (k) => !events.some((e) => e.kind === k || e.user_term === k),
    );
    // overFlagTerms mixes InterpretationKind-valued entries (e.g.
    // "invented_source", whose pattern /invent|fabricat/i matches the KIND) with
    // user_term-valued entries (e.g. "project_name"/"total_cost", matched on the
    // review's user_term). Test each term's pattern against EITHER the event kind
    // OR its user_term so the kind-valued entry is recognised. Kinds are enum
    // values and user_terms are field-path strings, so the OR cannot false-match
    // across the two namespaces.
    const overFlagged = ASSUMPTION_RUBRIC.overFlagTerms.filter((_label, i) => {
      const pattern = ASSUMPTION_RUBRIC.overFlagTermPatterns[i];
      return events.some(
        (e) =>
          (typeof e.kind === "string" && pattern.test(e.kind)) ||
          (typeof e.user_term === "string" && pattern.test(e.user_term)),
      );
    });
    const normalized =
      (comp.composer_meta as Record<string, unknown> | null)
        ?.tutorial_runtime_normalized === true;
    const reachable = reachableSourceCount(diag.tokens, scrapeNode);
    const substantive = substantiveRowCount(outputRows, comp.sourceInputKeys);

    // Dimension (b): tutorial-backend PARITY. When normalization did NOT fire, the
    // tutorial run already executed through the normal ExecutionService→Orchestrator
    // path, so a graduated run with output rows IS a passing real-system run. A
    // fired normalization means the tutorial was treated differently from a regular
    // run (it repaired the composed pipeline) → dim (b) FAILS. No separate re-run is
    // issued (it would collide on the first run's output artifacts → FileExistsError).
    const dimBPassed = !normalized && graduated && outputRows.length > 0;
    const efficiency = sessionId
      ? await fetchPlannerAuditEvidence(ctx, sessionId)
          .then((evidence) => classifyPlannerEfficiency(evidence, dimBPassed))
          .catch((error: unknown) =>
            unavailablePlannerEfficiency(error instanceof Error ? error.message : String(error)),
          )
      : unavailablePlannerEfficiency("session id was not captured");

    // Classify (spec §7) via the pure, unit-tested classifier (harness/classify.ts).
    // It keys on the BACKEND outcome of the blocking step (compose/run POST status,
    // whether it responded, whether a pipeline was composed) — NOT on the Playwright
    // "Timeout" string, which the old classifier matched and which laundered compose/
    // run validation failures and provider latency into one `infra_fault` bucket
    // (notes/tutorial-harness-infra-timeout-rootcause-2026-06-07.md). The two headline
    // numbers (tutorial-pass-rate vs infra-noise rate) are only meaningful once these
    // are separated.
    const { outcome, sub, fix } = classifyOutcome({
      graduated,
      turnReached,
      compose: step.compose,
      run: step.run,
      composedNodeCount: comp.nodes.length,
      normalized,
      underFlaggedCount: underFlagged.length,
      overFlaggedCount: overFlagged.length,
      reachable,
      minReachable: JUDGE_RUBRIC.minReachableSources,
      discardedRowCount,
      maxDiscarded: JUDGE_RUBRIC.maxDiscardedRows,
      substantive,
      minSubstantive: JUDGE_RUBRIC.minSubstantiveRows,
      outputRowCount: outputRows.length,
      hardError,
    });

    const record: EfficiencyRunRecord = {
      batch_id: BATCH_ID,
      run_index: runIndex,
      outcome,
      fault_subclass: sub,
      fix_target: fix,
      turn_reached: turnReached,
      tutorial_run_id: tutorialRunId,
      realsystem_run_id: realsystemRunId,
      seeded_from_cache: seededFromCache,
      dim_a_tutorial_completed: graduated,
      dim_b_realsystem_passed: dimBPassed,
      dim_c_assumptions_ok:
        underFlagged.length === 0 && overFlagged.length === 0,
      dim_d_solution_quality: {
        status: "pending_judge",
        judge_score: null,
        source_reachable: `${reachable}/${JUDGE_RUBRIC.minReachableSources}`,
        discarded_row_count: discardedRowCount,
        substantive_rows: `${substantive}/${outputRows.length}`,
      },
      assumptions: {
        raised: events.map((e) => ({ kind: e.kind, term: e.user_term })),
        under_flagged: [...underFlagged],
        over_flagged: [...overFlagged],
      },
      output_rows: outputRows,
      landscape: {
        tutorial_failure: hardError,
        realsystem_failure: normalized
          ? "tutorial normalization repaired the pipeline; not run as a regular run"
          : null,
        normalization_fired: normalized,
      },
      stamp: {
        composer_skill_hash: events[0]?.composer_skill_hash ?? null,
        model_identifier: events[0]?.model_identifier ?? null,
      },
      timing_s: {
        ...(step.compose.elapsedMs !== null
          ? { compose_s: Math.round(step.compose.elapsedMs / 100) / 10 }
          : {}),
        ...(step.run.elapsedMs !== null
          ? { run_s: Math.round(step.run.elapsedMs / 100) / 10 }
          : {}),
      },
      efficiency,
      // Per-transition ledger: gestures, provider calls, planner runs and wall
      // clock for every authoring transition, from the durable audit rows.
      transitions,
      // Backend step evidence (the de-conflation inputs) — kept in the record so
      // a future batch is diagnosable without re-running: did each POST fire,
      // respond, with what status, in how long.
      steps: {
        compose: {
          fired: step.compose.fired,
          responded: step.compose.responded,
          status: step.compose.status,
          elapsed_s: step.compose.elapsedMs !== null ? Math.round(step.compose.elapsedMs / 100) / 10 : null,
          body: step.compose.bodyText?.slice(0, 500) ?? null,
        },
        run: {
          fired: step.run.fired,
          responded: step.run.responded,
          status: step.run.status,
          elapsed_s: step.run.elapsedMs !== null ? Math.round(step.run.elapsedMs / 100) / 10 : null,
          body: step.run.bodyText?.slice(0, 500) ?? null,
        },
      },
      error: hardError,
    };

    const dir = `tests/e2e/.harness-results/${BATCH_ID}`;
    mkdirSync(dir, { recursive: true });
    writeFileSync(
      `${dir}/run-${String(runIndex).padStart(2, "0")}.json`,
      JSON.stringify(record, null, 2),
    );
    await ctx.dispose();
    if (transitions !== null) {
      // Human-readable copy of the ledger beside the JSON record, so a batch
      // log reads as the review's turn-by-turn table.
      const table = renderLedgerMarkdown(transitions);
      writeFileSync(`${dir}/run-${String(runIndex).padStart(2, "0")}.ledger.md`, `${table}\n`);
      console.log(`[transition-ledger] run ${runIndex}\n${table}`);
      await test.info().attach(`transition-ledger-run-${runIndex}`, {
        body: JSON.stringify(transitions, null, 2),
        contentType: "application/json",
      });
    } else {
      console.log(`[transition-ledger] run ${runIndex}: ledger unavailable: ${ledgerError ?? "unknown"}`);
    }
    const efficiencyFailure = plannerEfficiencyAssertionFailure(efficiency, hardError);
    if (efficiencyFailure !== null) {
      expect(
        efficiencyFailure,
        `planner efficiency failed: ${efficiencyFailure}`,
      ).toBeNull();
    }
    // Per-transition invariant (elspeth-515096e18c): a transition that hands
    // the learner a new proposal must have paid a planner call for it in THAT
    // transition, and every transition's evidence must have been readable.
    // Like the efficiency gate, it is the primary failure only when the walk
    // itself did not already fail.
    if (hardError === null) {
      const ledgerViolations = transitions === null
        ? [`per-transition ledger unavailable: ${ledgerError ?? "unknown"}`]
        : transitions.violations;
      expect(
        ledgerViolations,
        `per-transition ledger violations: ${ledgerViolations.join("; ")}`,
      ).toEqual([]);
      // One freeform compose request owns the planner work that authored the
      // tutorial pipeline. The ledger's per-transition invariant above refuses
      // a published state or proposal with zero attributed planner calls.
      const freeformCompose = transitions?.entries.filter((entry) => entry.endpoint === "freeform/compose") ?? [];
      expect(freeformCompose.length, "the fixed brief must go through freeform Composer").toBeGreaterThan(0);
      expect(freeformCompose.some((entry) => entry.evidence.planner_calls > 0)).toBe(true);
    }
  }
}

test.describe("tutorial reliability battery", () => {
  // Inter-run cooldown: spacing the runs reduces the LLM-provider contention that
  // inflated composition/run latency under back-to-back load (batch-2026-06-06:
  // isolated runs ~72s, but 3/10 timed out under rapid succession). Skipped before
  // the first run. Tune via HARNESS_COOLDOWN_MS.
  const COOLDOWN_MS = Number(process.env.HARNESS_COOLDOWN_MS ?? "15000");
  let cooldownNeeded = false;
  test.beforeEach(async () => {
    if (cooldownNeeded) await new Promise((r) => setTimeout(r, COOLDOWN_MS));
    cooldownNeeded = true;
    const ctx = await harnessCtx();
    await cleanSessions(ctx);
    await resetToFirstRun(ctx);
    await ctx.dispose();
  });

  for (let i = 1; i <= BATCH_SIZE; i++) {
    // Independent tests so one failure does not block the rest; reset runs in
    // beforeEach between every one (config is workers:1, retries:0, sequential).
    test(`tutorial run ${i}/${BATCH_SIZE}`, async ({ page }) => {
      await runOnce(page, i);
    });
  }
});
