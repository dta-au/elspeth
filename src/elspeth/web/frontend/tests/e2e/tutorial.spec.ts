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
}

function chatMessage(role: "user" | "assistant", content: string): Record<string, unknown> {
  return {
    id: role === "user" ? "user-1" : "assistant-1", session_id: sid, role, content,
    tool_calls: null, created_at: stamp, composition_state_id: role === "assistant" ? stateId : null,
    tool_call_id: null, parent_assistant_id: null, sequence_no: role === "assistant" ? 1 : 0,
  };
}

async function installRoutes(page: Page, fixture: Fixture): Promise<void> {
  const prefs: Record<string, unknown> = {
    default_mode: "freeform", freeform_intro_dismissed_at: null,
    tutorial_completed_at: null, tutorial_stage: null, tutorial_session_id: null,
    tutorial_run_id: null, tutorial_source_data_hash: null, show_advanced: false, updated_at: null,
  };
  await page.route("**/api/**", async (route: Route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const method = req.method();
    const fulfill = async (json: unknown): Promise<void> => { await route.fulfill({ json }); };
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
    if (path === `/api/sessions/${sid}/guided` && method === "GET") {
      await fulfill(null);
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
        const body = req.postDataJSON() as { content: string };
        expect(body.content).toContain("project-3.html");
        fixture.requests.push("freeform-compose");
        fixture.messages = [
          chatMessage("user", body.content),
          chatMessage("assistant", "I built the pipeline for review."),
        ];
        fixture.composed = true;
        await fulfill({ message: fixture.messages[1], state, proposals: [] });
      } else {
        await fulfill(fixture.messages);
      }
      return;
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
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("bold")).toBeVisible();
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
  expect(fixture.requests.filter((item) => item.startsWith("guided-"))).toHaveLength(0);
});
