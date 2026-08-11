import { join } from "node:path";

import type { Page, Route } from "@playwright/test";

import {
  authedContext,
  createSession,
  deleteSession,
  seedCompositionState,
  tokenFromStorageState,
  uploadBlob,
} from "./api";

export const DESKTOP_VIEWPORTS = [
  { width: 1920, height: 900 },
  { width: 1536, height: 760 },
  { width: 1280, height: 720 },
  { width: 2048, height: 1050 },
  { width: 2560, height: 1280 },
] as const;

export const WORKSPACE_SCENARIOS = [
  "empty-freeform",
  "populated-long-transcript",
  "active-guided-decision",
  "validation-audit-issues",
  "pending-acknowledgement",
  "active-completed-run",
  "multiple-notices",
  "tall-confirmation-dialog",
] as const;

export type WorkspaceScenario = (typeof WORKSPACE_SCENARIOS)[number];

export interface WorkspaceScenarioTelemetry {
  runHistoryRequests: number;
}

interface DeferredSignal {
  promise: Promise<void>;
  release: () => void;
}

interface InstalledScenario {
  sessionId: string;
  pendingAcknowledgement: DeferredSignal | null;
  telemetry: WorkspaceScenarioTelemetry;
}

const INSTALLED_SCENARIOS = new WeakMap<Page, InstalledScenario>();
const SOURCE_FILENAME = "workspace-geometry.csv";
const FIXED_TIME = "2026-08-11T08:00:00.000Z";

function deferredSignal(): DeferredSignal {
  let release: (() => void) | undefined;
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  if (release === undefined) throw new Error("failed to initialize fixture signal");
  return { promise, release };
}

function sourcePath(sessionId: string, blobId: string): string {
  const dataDir = process.env.PLAYWRIGHT_E2E_DATA_DIR;
  if (!dataDir) {
    throw new Error("PLAYWRIGHT_E2E_DATA_DIR is required for workspace geometry");
  }
  return join(dataDir, "blobs", sessionId, `${blobId}_${SOURCE_FILENAME}`);
}

async function seedCanonicalComposition(
  page: Page,
  sessionId: string,
  scenario: WorkspaceScenario,
): Promise<Record<string, unknown>> {
  const token = tokenFromStorageState(await page.context().storageState());
  const ctx = await authedContext(token);
  try {
    const blob = await uploadBlob(
      ctx,
      sessionId,
      SOURCE_FILENAME,
      "id,category\n1,alpha\n2,beta\n",
    );
    const tallDialogNodes = scenario === "tall-confirmation-dialog"
      ? Array.from({ length: 48 }, (_, index) => {
          const stage = String(index + 1).padStart(3, "0");
          const id = `llm_stage_${stage}_${"deterministic_review_".repeat(4)}fixture`;
          return {
            id,
            node_type: "transform",
            plugin: "llm",
            input: index === 0 ? "source" : `stage_${String(index).padStart(3, "0")}`,
            on_success: index === 47 ? "results" : `stage_${stage}`,
            on_error: "discard",
            options: { model: "fixture/deterministic-model" },
          };
        })
      : [];
    return await seedCompositionState(ctx, sessionId, {
      version: 1,
      metadata: {
        name: "Deterministic workspace pipeline",
        description: "Fixed geometry acceptance composition.",
      },
      sources: {
        source: {
          plugin: "csv",
          on_success:
            tallDialogNodes.length > 0 ? tallDialogNodes[0]!.id : "results",
          options: {
            path: sourcePath(sessionId, blob.id),
            blob_ref: blob.id,
            schema: { mode: "observed" },
          },
          on_validation_failure: "discard",
        },
      },
      nodes: tallDialogNodes,
      edges: [],
      outputs: [
        {
          name: "results",
          plugin: "csv",
          options: {
            path: "outputs/deterministic-workspace.csv",
            schema: { mode: "observed" },
          },
          on_write_failure: "discard",
        },
      ],
    });
  } finally {
    await ctx.dispose();
  }
}

function longTranscript(sessionId: string): Record<string, unknown>[] {
  return Array.from({ length: 56 }, (_, index) => {
    const role = index % 2 === 0 ? "user" : "assistant";
    const sequence = index + 1;
    const content = `${role === "user" ? "Operator" : "Composer"} turn ${sequence}: This fixed transcript line is deliberately long enough to exercise the authoring pane scroll owner without relying on timing or generated prose.`;
    return {
      id: `workspace-message-${String(sequence).padStart(2, "0")}`,
      session_id: sessionId,
      role,
      content,
      segments: role === "assistant" ? [{ kind: "text", content }] : undefined,
      tool_calls: null,
      created_at: `2026-08-11T08:00:${String(index).padStart(2, "0")}.000Z`,
      composition_state_id: null,
      tool_call_id: null,
      parent_assistant_id: null,
      sequence_no: sequence,
    };
  });
}

function guidedFixture(
  sessionId: string,
  compositionState: Record<string, unknown>,
): Record<string, unknown> {
  return {
    guided_session: {
      step: "step_1_source",
      history: [],
      terminal: null,
      chat_history: [
        {
          role: "assistant",
          content: "Choose the authoritative input for this pipeline.",
          seq: 0,
          step: "step_1_source",
          ts_iso: FIXED_TIME,
          assistant_message_kind: "assistant",
          synthetic_failure_reason: null,
          turn_token: null,
        },
      ],
      chat_turn_seq: 1,
      profile: { coaching: true, bookends: true },
    },
    next_turn: {
      type: "single_select",
      step_index: 0,
      turn_token: "a".repeat(64),
      payload: {
        question: "Which source should the pipeline use?",
        options: [
          { id: "csv", label: "CSV", hint: "Use the uploaded CSV fixture." },
          { id: "inline_blob", label: "Inline rows", hint: null },
        ],
        allow_custom: false,
      },
    },
    terminal: null,
    composition_state: { ...compositionState, session_id: sessionId },
  };
}

function validationIssues(): Record<string, unknown> {
  const errors = Array.from({ length: 24 }, (_, index) => ({
    component_id: "source",
    component_type: "source",
    message: `Deterministic validation issue ${String(index + 1).padStart(2, "0")}: review the fixed source contract before running.`,
    suggestion: "Choose a supported field contract.",
  }));
  return {
    is_valid: false,
    summary: "24 deterministic validation issues require attention.",
    checks: [],
    errors,
    warnings: [],
    semantic_contracts: [],
    readiness: {
      authoring_valid: false,
      execution_ready: false,
      completion_ready: false,
      blockers: errors.map((_, index) => ({
        code: "validation_error",
        component_id: "source",
        component_type: "source",
        detail: `Deterministic validation issue ${index + 1}`,
      })),
    },
  };
}

function validValidation(): Record<string, unknown> {
  return {
    is_valid: true,
    summary: "The deterministic tall-dialog composition is runnable.",
    checks: [],
    errors: [],
    warnings: [],
    semantic_contracts: [],
    readiness: {
      authoring_valid: true,
      execution_ready: true,
      completion_ready: true,
      blockers: [],
    },
  };
}

function auditIssues(
  sessionId: string,
  validationResult: Record<string, unknown>,
): Record<string, unknown> {
  return {
    session_id: sessionId,
    composition_version: 1,
    checked_at: FIXED_TIME,
    rows: [
      { id: "validation", label: "Validation", status: "error", summary: "24 validation issues require attention.", detail: "The deterministic fixture keeps the inspector tall.", component_ids: ["source"] },
      { id: "plugin_trust", label: "Plugin trust", status: "warning", summary: "Review the fixed plugin trust warning.", detail: null, component_ids: ["source"] },
      { id: "provenance", label: "Provenance", status: "ok", summary: "Source provenance is recorded.", detail: null, component_ids: [] },
      { id: "retention", label: "Retention", status: "not_applicable", summary: "No completed run retention yet.", detail: null, component_ids: [] },
      { id: "llm_interpretations", label: "LLM interpretations", status: "not_applicable", summary: "No interpretation events.", detail: null, component_ids: [] },
      { id: "secrets", label: "Secrets", status: "ok", summary: "No secret bindings are required.", detail: null, component_ids: [] },
    ],
    validation_result: validationResult,
  };
}

function pendingInterpretation(
  sessionId: string,
  compositionStateId: string,
): Record<string, unknown> {
  return {
    id: "workspace-interpretation-01",
    session_id: sessionId,
    composition_state_id: compositionStateId,
    affected_node_id: "source",
    tool_call_id: "workspace-tool-call-01",
    user_term: "authoritative category",
    kind: "vague_term",
    llm_draft: "Use the category field as the authoritative classification.",
    accepted_value: null,
    choice: "pending",
    created_at: FIXED_TIME,
    resolved_at: null,
    actor: "system:composer",
    interpretation_source: "user_approved",
    model_identifier: "deterministic-e2e-model",
    model_version: "2026-08-11",
    provider: "playwright-route",
    composer_skill_hash: "b".repeat(64),
    arguments_hash: null,
    hash_domain_version: null,
    runtime_model_identifier_at_resolve: null,
    runtime_model_version_at_resolve: null,
    resolved_prompt_template_hash: null,
  };
}

function runFixtures(sessionId: string): Record<string, unknown>[] {
  const accounting = {
    source: { rows_processed: 2, rows_rejected: 0, rows_read: 2 },
    sources: { source: { rows_processed: 2, rows_rejected: 0, rows_read: 2 } },
    tokens: { emitted: 2, terminal: 2, succeeded: 2, failed: 0, structural: 0, pending: 0, abandoned: 0 },
    routing: { routed_success: 2, routed_failure: 0, quarantined: 0, discarded: 0 },
    integrity: { closure: "closed", missing_terminal_outcomes: 0, duplicate_terminal_outcomes: 0 },
  };
  return [
    { id: "workspace-run-completed", session_id: sessionId, status: "completed", cancel_requested: false, accounting, accounting_corruption: null, error: null, started_at: "2026-08-11T08:00:00.000Z", finished_at: "2026-08-11T08:00:02.000Z", composition_version: 1, discard_summary: null },
    { id: "workspace-run-active", session_id: sessionId, status: "running", cancel_requested: false, accounting: null, accounting_corruption: null, error: null, started_at: "2026-08-11T08:01:00.000Z", finished_at: null, composition_version: 1, discard_summary: null },
  ];
}

async function fulfillWorkspaceRoute(
  route: Route,
  installed: InstalledScenario,
  scenario: WorkspaceScenario,
  compositionState: Record<string, unknown> | null,
): Promise<boolean> {
  const request = route.request();
  const { pathname } = new URL(request.url());
  const method = request.method();
  const { sessionId } = installed;

  if (pathname === `/api/sessions/${sessionId}/messages` && method === "GET") {
    await route.fulfill({ json: scenario === "populated-long-transcript" ? longTranscript(sessionId) : [] });
    return true;
  }
  if (pathname === `/api/sessions/${sessionId}/guided` && method === "GET") {
    await route.fulfill({
      json: scenario === "active-guided-decision" && compositionState !== null
        ? guidedFixture(sessionId, compositionState)
        : { guided_session: null, next_turn: null, terminal: null, composition_state: null },
    });
    return true;
  }
  if (pathname === `/api/sessions/${sessionId}/interpretations` && method === "GET") {
    if (scenario !== "pending-acknowledgement") {
      await route.fulfill({ json: { events: [] } });
      return true;
    }
    const signal = installed.pendingAcknowledgement;
    if (signal === null) throw new Error("pending acknowledgement signal missing");
    await signal.promise;
    const stateId = compositionState?.id;
    if (typeof stateId !== "string") throw new Error("seeded state id missing");
    await route.fulfill({ json: { events: [pendingInterpretation(sessionId, stateId)] } });
    return true;
  }
  if (pathname === `/api/sessions/${sessionId}/runs` && method === "GET") {
    installed.telemetry.runHistoryRequests += 1;
    await route.fulfill({ json: scenario === "active-completed-run" ? runFixtures(sessionId) : [] });
    return true;
  }
  if (pathname === "/api/runs/workspace-run-completed/outputs" && method === "GET") {
    await route.fulfill({ json: { run_id: "workspace-run-completed", landscape_run_id: "workspace-landscape-completed", artifacts: [] } });
    return true;
  }
  if (
    scenario === "validation-audit-issues" ||
    scenario === "tall-confirmation-dialog"
  ) {
    const result = scenario === "validation-audit-issues"
      ? validationIssues()
      : validValidation();
    if (pathname === `/api/sessions/${sessionId}/validate` && method === "POST") {
      await route.fulfill({ json: result });
      return true;
    }
    if (pathname === `/api/sessions/${sessionId}/audit-readiness` && method === "GET") {
      await route.fulfill({ json: auditIssues(sessionId, result) });
      return true;
    }
  }
  if (scenario === "multiple-notices") {
    if (pathname === "/api/system/status" && method === "GET") {
      await route.fulfill({ json: { composer_available: false, composer_model: "deterministic-e2e-model", composer_provider: "playwright-route", composer_reason: "Composer provider is unavailable for the deterministic notice scenario.", composer_missing_keys: [], composer_timeout_seconds: 180 } });
      return true;
    }
    if (pathname === "/api/composer-preferences" && method === "GET") {
      await route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "Deterministic preferences failure." }) });
      return true;
    }
  }
  return false;
}

export async function installWorkspaceScenario(
  page: Page,
  scenario: WorkspaceScenario,
): Promise<string> {
  const token = tokenFromStorageState(await page.context().storageState());
  const ctx = await authedContext(token);
  let sessionId: string;
  try {
    sessionId = (await createSession(ctx, `workspace ${scenario}`)).id;
  } finally {
    await ctx.dispose();
  }
  const compositionState = scenario === "empty-freeform"
    ? null
    : await seedCanonicalComposition(page, sessionId, scenario);
  const installed: InstalledScenario = {
    sessionId,
    pendingAcknowledgement: scenario === "pending-acknowledgement" ? deferredSignal() : null,
    telemetry: { runHistoryRequests: 0 },
  };
  INSTALLED_SCENARIOS.set(page, installed);
  await page.route("**/api/**", async (route) => {
    if (!(await fulfillWorkspaceRoute(route, installed, scenario, compositionState))) {
      await route.continue();
    }
  });
  return sessionId;
}

export async function deleteWorkspaceScenario(page: Page, sessionId: string): Promise<void> {
  INSTALLED_SCENARIOS.get(page)?.pendingAcknowledgement?.release();
  const token = tokenFromStorageState(await page.context().storageState());
  const ctx = await authedContext(token);
  try {
    await deleteSession(ctx, sessionId);
  } finally {
    await ctx.dispose();
    INSTALLED_SCENARIOS.delete(page);
  }
}

export function releasePendingAcknowledgement(page: Page): void {
  const signal = INSTALLED_SCENARIOS.get(page)?.pendingAcknowledgement;
  if (signal === null || signal === undefined) throw new Error("no pending acknowledgement fixture");
  signal.release();
}

export function workspaceScenarioTelemetry(page: Page): WorkspaceScenarioTelemetry {
  const installed = INSTALLED_SCENARIOS.get(page);
  if (installed === undefined) throw new Error("no workspace scenario is installed");
  return installed.telemetry;
}

export function resetWorkspaceScenarioTelemetry(page: Page): void {
  const installed = INSTALLED_SCENARIOS.get(page);
  if (installed === undefined) throw new Error("no workspace scenario is installed");
  installed.telemetry.runHistoryRequests = 0;
}
