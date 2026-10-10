// Fixed-input freeform tutorial browser contract. The browser and authenticated
// account are real; provider and run responses are mocked to avoid LLM spend.
import { expect, test, type Page, type Route } from "@playwright/test";

const sid = "11111111-1111-4111-8111-111111111111";
const stateId = "00000000-0000-4000-8000-000000000001";
const stamp = "2026-05-19T12:00:00Z";
const session = { id: sid, title: "First-run tutorial (in progress)", created_at: stamp, updated_at: stamp };
const state = {
  id: stateId, session_id: sid, version: 1,
  sources: { source: { plugin: "csv", options: { path: "project_briefs.csv" }, on_success: "scrape", on_validation_failure: "discard" } },
  nodes: [
    { id: "scrape", node_type: "transform", plugin: "web_scrape", input: "source", on_success: "summarize", on_error: null, options: {} },
    { id: "summarize", node_type: "transform", plugin: "llm", input: "scrape", on_success: "clean", on_error: null, options: { profile: "e2e-bedrock" } },
    { id: "clean", node_type: "transform", plugin: "field_mapper", input: "summarize", on_success: "result", on_error: null, options: {} },
  ],
  edges: [],
  outputs: [{ name: "result", plugin: "json", options: { path: "project_brief_summaries.json" }, on_write_failure: "discard" }],
  metadata: { name: null, description: null }, is_valid: true, validation_errors: [],
  validation_warnings: [], validation_suggestions: [], derived_from_state_id: null,
  created_at: stamp, composer_meta: null, plugin_policy_findings: [],
};

interface Fixture {
  created: boolean;
  composed: boolean;
  completed: boolean;
  title: string;
  runCount: number;
  messages: Array<Record<string, unknown>>;
  requests: string[];
  /** Leave the compose POST unanswered, so Build stays mid-compose. */
  holdCompose?: boolean;
}

function chatMessage(role: "user" | "assistant", content: string): Record<string, unknown> {
  return {
    id: role === "user" ? "22222222-2222-4222-8222-222222222222" : "33333333-3333-4333-8333-333333333333", session_id: sid, role, content,
    segments: [{ kind: "text", content }], raw_content: null, rejection: null, operation_id: null,
    tool_calls: null, created_at: stamp, composition_state_id: role === "assistant" ? stateId : null,
    tool_call_id: null, parent_assistant_id: null, sequence_no: role === "assistant" ? 1 : 0,
  };
}

async function installRoutes(page: Page, fixture: Fixture): Promise<void> {
  let operationId: string | null = null;
  const prefs: Record<string, unknown> = {
    freeform_intro_dismissed_at: null,
    tutorial_completed_at: null, tutorial_stage: null, tutorial_session_id: null,
    tutorial_run_id: null, tutorial_source_data_hash: null, show_advanced: false, updated_at: null,
  };
  await page.route("**/api/**", async (route: Route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const method = req.method();
    const fulfill = async (json: unknown): Promise<void> => { await route.fulfill({ json }); };
    if (path.includes("/guided")) {
      fixture.requests.push("forbidden-guided-request");
      await route.fulfill({ status: 404, json: { detail: "Not found" } });
      return;
    }
    if (path === "/api/system/status" && method === "GET") {
      await fulfill({
        composer_available: true, composer_model: "test", composer_advisor_model: "test",
        composer_provider: "test", composer_reason: null, composer_missing_keys: [],
        composer_timeout_seconds: 180, tutorial_ready: true, tutorial_reason: null,
        plugin_policy_readiness: { tutorial_ready: true, rows: [] },
      });
      return;
    }
    if (path === "/api/composer-preferences") {
      if (method === "PATCH") {
        const body = req.postDataJSON() as Record<string, unknown>;
        fixture.requests.push(`preferences:${String(body.tutorial_stage ?? body.tutorial_completed_via ?? "other")}`);
        for (const key of Object.keys(prefs)) {
          if (key in body) prefs[key] = body[key];
        }
        if (body.tutorial_completed_at != null) {
          fixture.completed = true;
          Object.assign(prefs, {
            tutorial_stage: null, tutorial_session_id: null,
            tutorial_run_id: null, tutorial_source_data_hash: null,
          });
        }
        prefs.updated_at = stamp;
      }
      await fulfill(prefs);
      return;
    }
    if (path === "/api/sessions" && method === "GET") {
      await fulfill(fixture.created ? [{ ...session, title: fixture.title }] : []);
      return;
    }
    if (path === "/api/sessions" && method === "POST") {
      fixture.created = true;
      fixture.requests.push("create-session");
      await fulfill(session);
      return;
    }
    if (path === `/api/sessions/${sid}` && method === "PATCH") {
      fixture.title = (req.postDataJSON() as { title: string }).title;
      fixture.requests.push(`rename:${fixture.title}`);
      await fulfill({ ...session, title: fixture.title });
      return;
    }
    if (path === `/api/tutorial/${sid}/sample` && method === "GET") {
      fixture.requests.push("sample");
      await fulfill({ sample_urls: [1, 2, 3].map((n) =>
        `https://dta-au.github.io/elspeth/tutorial-site/project-${n}.html`) });
      return;
    }
    if (path === `/api/tutorial/${sid}/readiness` && method === "GET") {
      fixture.requests.push("readiness");
      await fulfill({ state_id: stateId });
      return;
    }
    if (path === `/api/sessions/${sid}/messages`) {
      if (method === "POST") {
        const body = req.postDataJSON() as { content: string; operation_id: string };
        operationId = body.operation_id;
        expect(body.content).toContain("project-3.html");
        fixture.requests.push("freeform-compose");
        if (fixture.holdCompose === true) {
          fixture.messages = [chatMessage("user", body.content)];
          return;
        }
        fixture.messages = [
          chatMessage("user", body.content),
          chatMessage("assistant", "I built the pipeline for review."),
        ];
        fixture.composed = true;
        await route.fulfill({ status: 202, json: { operation_id: operationId, kind: "compose_message", status: "queued", poll_after_ms: 1000 } });
      } else {
        await fulfill(fixture.messages);
      }
      return;
    }
    if (operationId !== null && path === `/api/sessions/${sid}/operations/${operationId}/stream`) {
      await route.fulfill({ status: 503, json: { detail: "stream unavailable" } }); return;
    }
    if (operationId !== null && path === `/api/sessions/${sid}/operations/${operationId}`) {
      await fulfill({ operation_id: operationId, kind: "compose_message", status: "completed", poll_after_ms: 1000, cancel_requested: false, deadline_at: stamp, deadline_remaining_ms: 0, result: { message: fixture.messages[1], state, proposals: [] }, error: null }); return;
    }
    if (path === `/api/sessions/${sid}/state` && method === "GET") {
      await fulfill(fixture.composed ? state : null);
      return;
    }
    if (path === `/api/sessions/${sid}/state/versions` && method === "GET") {
      await fulfill(fixture.composed ? [{ id: stateId, version: 1, created_at: stamp, node_count: 3 }] : []);
      return;
    }
    if (path === `/api/sessions/${sid}/proposals` && method === "GET") {
      await fulfill([]);
      return;
    }
    if (path === `/api/sessions/${sid}/interpretations` && method === "GET") {
      await fulfill({ events: [] });
      return;
    }
    if (path === `/api/sessions/${sid}/composer/preferences` && method === "GET") {
      await fulfill({ session_id: sid, trust_mode: "explicit_approve", density_default: "medium", interpretation_review_disabled: false, updated_at: stamp });
      return;
    }
    if (path === `/api/sessions/${sid}/composer-progress` && method === "GET") {
      await fulfill({ session_id: sid, request_id: null, phase: "idle", headline: "Idle.", evidence: [], likely_next: null, reason: "composer_idle", updated_at: stamp });
      return;
    }
    if (path === `/api/sessions/${sid}/validate` && method === "POST") {
      await fulfill({ is_valid: true, readiness: { authoring_valid: true, execution_ready: true, completion_ready: true, blockers: [] }, summary: "Valid.", checks: [], errors: [], warnings: [], semantic_contracts: [] });
      return;
    }
    if (path === `/api/sessions/${sid}/audit-readiness` && method === "GET") {
      await fulfill({ session_id: sid, composition_version: 1, checked_at: stamp, rows: [], validation_result: {
        is_valid: true, readiness: { authoring_valid: true, execution_ready: true, completion_ready: true, blockers: [] },
        summary: "Valid.", checks: [], errors: [], warnings: [], semantic_contracts: [],
      } });
      return;
    }
    if ((path === `/api/sessions/${sid}/runs` || path === `/api/sessions/${sid}/blobs`) && method === "GET") {
      await fulfill([]);
      return;
    }
    if (path === "/api/tutorial/orphans" && method === "DELETE") {
      await fulfill({ deleted_count: 0 });
      return;
    }
    if (path === "/api/tutorial/run" && method === "POST") {
      expect(req.postDataJSON()).toEqual({ session_id: sid });
      fixture.requests.push("run");
      fixture.runCount += 1;
      await fulfill({ run_id: "run-1", output: {
        source_data_hash: "a7f3e2fullhash",
        rows: [{ url: "project-1.html", summary: "bold" }],
        discarded_row_count: 0,
      }, seeded_from_cache: false, cache_key: null });
      return;
    }
    if (path === `/api/sessions/${sid}/runs/run-1/audit-story` && method === "GET") {
      fixture.requests.push("audit-story");
      await fulfill({
        run_id: "run-1", session_id: sid, llm_call_count: 5,
        source_data_hash: "a7f3e2fullhash", started_at: stamp,
        plugin_versions: { web_scrape: "1.0.0", llm: "1.0.0" },
        seeded_from_cache: false, cache_key: null,
      });
      return;
    }
    await route.continue();
  });
}

test("Welcome → freeform Build → explicit Run → Audit → Graduation keeps one session", async ({ page }) => {
  const fixture: Fixture = {
    created: false, composed: false, completed: false, title: session.title,
    runCount: 0, messages: [], requests: [],
  };
  await installRoutes(page, fixture);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /Welcome to ELSPETH/i })).toBeVisible();
  await page.getByRole("button", { name: "Let's go" }).click();
  await expect(page.getByRole("heading", { name: "Build with the Composer." })).toBeVisible();
  await page.getByRole("button", { name: "Send tutorial brief" }).click();
  await expect(page.getByRole("button", { name: "Continue to Run" })).toBeEnabled();
  await page.getByRole("button", { name: "Continue to Run" }).click();
  await expect(page.getByRole("heading", { name: "Ready to run." })).toBeVisible();
  expect(fixture.runCount).toBe(0);
  // Run's actions live in the step header; its results in the authoring pane.
  await page.locator(".tutorial-step-header").getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.locator(".workspace-authoring-pane").getByRole("table")).toContainText("bold");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(page.getByText(/This is the audit story/i)).toBeVisible();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(page.getByRole("heading", { name: "You're ready to use the composer." })).toBeVisible();
  await page.getByRole("button", { name: "Take me to the composer" }).click();
  await expect(page.getByLabel("Chat panel", { exact: true })).toBeVisible();

  expect(fixture.runCount).toBe(1);
  expect(fixture.completed).toBe(true);
  expect(fixture.requests).toContain("freeform-compose");
  expect(fixture.requests).toContain("readiness");
  expect(fixture.requests).toContain("audit-story");
  expect(fixture.requests).toContain("rename:First-run tutorial");
  expect(fixture.requests.indexOf("readiness")).toBeLessThan(fixture.requests.indexOf("run"));
  expect(fixture.requests).not.toContain("forbidden-guided-request");
});

// Build's layout as rendered geometry (2026-10-08 review). The tutorial used to
// stack its own chrome inside the authoring pane: 0px gutter beside the
// transcript's 16px, the chat header 97px below the artifact toolbar it shares
// a row with, and the conversation squeezed to 143px (49px at 1280x560).
test("Build keeps its chrome out of the authoring pane and its Continue reachable at every width", async ({ page }) => {
  const fixture: Fixture = {
    created: false, composed: false, completed: false, title: session.title,
    runCount: 0, messages: [], requests: [], holdCompose: true,
  };
  await page.setViewportSize({ width: 1280, height: 800 });
  await installRoutes(page, fixture);
  await page.goto("/");
  await page.getByRole("button", { name: "Let's go" }).click();
  await page.getByRole("button", { name: "Send tutorial brief" }).click();
  const continueButton = page.getByRole("button", { name: "Continue to Run" });
  await expect(continueButton).toBeDisabled();
  await expect(continueButton).toHaveAccessibleDescription(/Composer is working on your pipeline/);
  await expect(page.locator(".chat-panel-messages")).toBeVisible();

  const geometry = await page.evaluate(() => {
    const rect = (selector: string): DOMRect => {
      const element = document.querySelector(selector);
      if (element === null) throw new Error(`missing ${selector}`);
      return element.getBoundingClientRect();
    };
    const pane = rect(".workspace-authoring-pane");
    const insets = Array.from(
      document.querySelectorAll(".workspace-authoring-pane :is(h2, p, pre, button, textarea)"),
    )
      .map((element) => element.getBoundingClientRect())
      .filter((box) => box.width > 1 && box.height > 1)
      .map((box) => Math.round(box.left - pane.left));
    return {
      minInset: Math.min(...insets),
      paneTop: Math.round(pane.top),
      transcriptTop: Math.round(rect(".chat-panel-messages").top),
      toolbarTop: Math.round(rect(".artifact-workspace-toolbar").top),
      transcript: Math.round(rect(".chat-panel-messages").height),
    };
  });
  expect(geometry.minInset, "every authoring control and line keeps the pane's 16px gutter").toBeGreaterThanOrEqual(16);
  expect(geometry.paneTop, "both panes start on one row across the seam").toBe(geometry.toolbarTop);
  expect(geometry.transcriptTop, "no chrome sits above the conversation in its pane").toBe(geometry.paneTop);
  expect(geometry.transcript, "the conversation keeps its 160px floor").toBeGreaterThanOrEqual(160);

  // Narrow Compose view hides the workspace bar's artifact cell, which is why
  // Continue lives in the step header: it stays beside the composer here.
  await page.setViewportSize({ width: 390, height: 700 });
  await expect(page.getByRole("tab", { name: "Compose" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByLabel("Message input")).toBeVisible();
  await expect(continueButton).toBeVisible();
});
