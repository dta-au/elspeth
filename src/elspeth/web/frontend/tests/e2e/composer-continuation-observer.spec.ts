// W-1: the Composer Continuation observer in a real browser (design §4, §5.4).
// The browser and the authenticated account are real; every /api response is
// mocked, including a lifecycle-2 operation and its SSE stream, so no LLM is
// called. The send goes through the tutorial's Build step, which uses the same
// submission path as freeform.
import { expect, test, type Page, type Route } from "@playwright/test";

const sid = "11111111-1111-4111-8111-111111111111";
const stateId = "00000000-0000-4000-8000-000000000001";
const stamp = "2026-10-25T00:00:00Z";
const session = { id: sid, title: "First-run tutorial (in progress)", created_at: stamp, updated_at: stamp };
const state = {
  id: stateId, session_id: sid, version: 1,
  sources: { source: { plugin: "csv", options: { path: "briefs.csv" }, on_success: "result", on_validation_failure: "discard" } },
  nodes: [], edges: [],
  outputs: [{ name: "result", plugin: "json", options: { path: "out.json" }, on_write_failure: "discard" }],
  metadata: { name: null, description: null }, is_valid: true, validation_errors: [],
  validation_warnings: [], validation_suggestions: [], derived_from_state_id: null,
  created_at: stamp, composer_meta: null, plugin_policy_findings: [],
};

function message(role: "user" | "assistant", content: string): Record<string, unknown> {
  return {
    id: role === "user" ? "22222222-2222-4222-8222-222222222222" : "33333333-3333-4333-8333-333333333333", session_id: sid, role, content,
    segments: [{ kind: "text", content }], raw_content: null, rejection: null, operation_id: null,
    tool_calls: null, created_at: stamp, composition_state_id: role === "assistant" ? stateId : null,
    tool_call_id: null, parent_assistant_id: null, sequence_no: role === "assistant" ? 1 : 0,
  };
}

type Phase = "queued" | "executing" | "continuation_queued";
function statusV2(operationId: string, revision: number, phase: Phase, sequence: number, running: boolean): Record<string, unknown> {
  const now = new Date().toISOString();
  return {
    protocol_version: 2, operation_id: operationId, kind: "compose_message", status: phase === "queued" ? "queued" : "running", phase,
    status_revision: revision, created_at: stamp, observed_at: now, poll_after_ms: 1000, cancel_requested: false,
    segment: { sequence, generation: sequence, state: running ? "running" : "queued", deadline_at: running ? new Date(Date.now() + 600_000).toISOString() : null },
  };
}
function frame(operationId: string, event: string, sequence: number, payload?: Record<string, unknown>): string {
  const data = { schema_version: "composer-operation-stream.v2", session_id: sid, operation_id: operationId, sequence, event, ...(payload === undefined ? {} : { payload }) };
  const id = payload !== undefined && (event === "status" || event === "terminal") ? `id: ${String(payload.status_revision)}\n` : "";
  return `${id}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

interface Fixture { operationId: string | null; versionHeader: string | null; streams: number; messages: Array<Record<string, unknown>> }

async function installRoutes(page: Page, fixture: Fixture, secondStream: Promise<void>, thirdStream: Promise<void>): Promise<void> {
  const prefs = { freeform_intro_dismissed_at: null, tutorial_completed_at: null, tutorial_stage: null, tutorial_session_id: null, tutorial_run_id: null, tutorial_source_data_hash: null, show_advanced: false, updated_at: null };
  let created = false;
  await page.route((url) => url.pathname.startsWith("/api/"), async (route: Route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const method = req.method();
    const json = async (body: unknown, status = 200, headers: Record<string, string> = {}): Promise<void> => { await route.fulfill({ status, json: body, headers }); };
    const op = fixture.operationId;
    if (path === "/api/system/status") {
      await json({ composer_available: true, composer_model: "test", composer_advisor_model: "test", composer_provider: "test", composer_reason: null, composer_missing_keys: [], composer_timeout_seconds: 180, tutorial_ready: true, tutorial_reason: null, plugin_policy_readiness: { tutorial_ready: true, rows: [] }, composer_operation_admission_version: 2 });
      return;
    }
    if (path === "/api/composer-preferences") { await json(prefs); return; }
    if (path === "/api/sessions" && method === "GET") { await json(created ? [session] : []); return; }
    if (path === "/api/sessions" && method === "POST") { created = true; await json(session); return; }
    if (path === `/api/sessions/${sid}` && method === "PATCH") { await json(session); return; }
    if (path === `/api/sessions/${sid}/operations` && method === "GET") { await json({ session_id: sid, items: [], next_cursor: null }); return; }
    if (path === `/api/sessions/${sid}/operations/active`) { await route.fulfill({ status: 204, body: "" }); return; }
    if (path === `/api/tutorial/${sid}/sample`) { await json({ sample_urls: [1, 2, 3].map((n) => `https://example.gov.au/project-${n}.html`) }); return; }
    if (path === `/api/tutorial/${sid}/readiness`) { await json({ state_id: stateId }); return; }
    if (path === `/api/sessions/${sid}/messages` && method === "POST") {
      const body = req.postDataJSON() as { content: string; operation_id: string };
      fixture.operationId = body.operation_id;
      fixture.versionHeader = req.headers()["composer-operation-version"] ?? null;
      fixture.messages = [message("user", body.content)];
      await json({ ...statusV2(body.operation_id, 0, "queued", 1, false), result: null, error: null }, 202, { "Composer-Operation-Version": "2" });
      return;
    }
    if (path === `/api/sessions/${sid}/messages`) { await json(fixture.messages); return; }
    if (op !== null && path === `/api/sessions/${sid}/operations/${op}/stream`) {
      fixture.streams += 1;
      const headers = { "Content-Type": "text/event-stream", "Composer-Operation-Version": "2" };
      if (fixture.streams === 1) {
        await route.fulfill({ status: 200, headers, body:
          frame(op, "status", 0, statusV2(op, 1, "executing", 1, true))
          + frame(op, "status", 1, statusV2(op, 2, "continuation_queued", 2, false))
          + frame(op, "reconnect", 2) });
        return;
      }
      if (fixture.streams === 2) {
        await secondStream;
        await route.fulfill({ status: 200, headers, body:
          frame(op, "status", 0, statusV2(op, 3, "executing", 2, true)) + frame(op, "reconnect", 1) });
        return;
      }
      await thirdStream;
      await route.fulfill({ status: 200, headers, body: frame(op, "terminal", 0, { status: "completed", phase: "settled", status_revision: 4 }) });
      return;
    }
    if (op !== null && path === `/api/sessions/${sid}/operations/${op}`) {
      fixture.messages = [fixture.messages[0], message("assistant", "The pipeline is ready for review.")];
      await json({ ...statusV2(op, 4, "executing", 2, true), status: "completed", phase: "settled", segment: null, result: { message: fixture.messages[1], state, proposals: [] }, error: null }, 200, { "Composer-Operation-Version": "2" });
      return;
    }
    if (path === `/api/sessions/${sid}/state`) { await json(op !== null && fixture.messages.length > 1 ? state : null); return; }
    if (path === `/api/sessions/${sid}/state/versions`) { await json([]); return; }
    if (path === `/api/sessions/${sid}/proposals`) { await json([]); return; }
    if (path === `/api/sessions/${sid}/interpretations`) { await json({ events: [] }); return; }
    if (path === `/api/sessions/${sid}/composer/preferences`) { await json({ session_id: sid, trust_mode: "explicit_approve", density_default: "medium", interpretation_review_disabled: false, updated_at: stamp }); return; }
    if (path === `/api/sessions/${sid}/composer-progress`) { await json({ session_id: sid, request_id: null, phase: "idle", headline: "Idle.", evidence: [], likely_next: null, reason: "composer_idle", updated_at: stamp }); return; }
    if ((path === `/api/sessions/${sid}/runs` || path === `/api/sessions/${sid}/blobs`) && method === "GET") { await json([]); return; }
    if (path === "/api/tutorial/orphans") { await json({ deleted_count: 0 }); return; }
    await route.continue();
  });
}

test("a continuing request shows Continuing, announces its next step once, and completes", async ({ page }) => {
  let releaseSecond!: () => void;
  let releaseThird!: () => void;
  const secondStream = new Promise<void>((resolve) => { releaseSecond = resolve; });
  const thirdStream = new Promise<void>((resolve) => { releaseThird = resolve; });
  const fixture: Fixture = { operationId: null, versionHeader: null, streams: 0, messages: [] };
  await installRoutes(page, fixture, secondStream, thirdStream);
  await page.goto("/");
  await page.getByRole("button", { name: "Let's go" }).click();
  await page.getByRole("button", { name: "Send tutorial brief" }).click();

  const status = page.locator(".composing-status-summary");
  await expect(status).toContainText("Continuing");
  await expect(status).toContainText("The composer reached a checkpoint. The next step of this request is queued.");
  await expect(status).not.toContainText(/saved/i);
  expect(fixture.versionHeader).toBe("2");

  releaseSecond();
  await expect(status).toContainText("Still working on this request.");
  releaseThird();
  await expect(page.getByText("The pipeline is ready for review.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Continue to Run" })).toBeEnabled();
  expect(fixture.streams).toBe(3);
});
