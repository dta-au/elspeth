// E2E spec: collector-authoring WIRE CONTRACT over the guided surface
// (route-mocked, deterministic — the ADR-031 2026-08-25 amendment's mocked
// canary, sibling of tutorial.spec.ts).
//
// The frozen tutorial script is UNTOUCHED (Q8-1 ruling): this spec drives the
// same guided frontend surface with a MOCKED ordinary collector-authoring turn
// sequence, so the wire contract the WS6 guard lift added is pinned in CI with
// near-zero flake:
//   - guided turn sequencing: propose_pipeline (collector-bearing proposal) →
//     accept → confirm_wiring → completed;
//   - collector node shape in responses: the strict guided decoder must admit
//     the closed collector behavior ({kind, opener_stable_id, policy}) — a
//     decoder regression turns the turn into "received but could not be read"
//     and fails this walk;
//   - topology/edge rendering: the proposal graph renders the collector node
//     and the wire overlay renders its from/to edge rows;
//   - advisories: wire-stage warnings render for the collector shape.
//
// Everything is served from route mocks (no live backend, no LLM). The first-
// run shell is only the mount for the guided surface; the turns below are an
// ordinary collector pipeline, not the tutorial scenario.

import { expect, test, type Page, type Route } from "@playwright/test";

const session = {
  id: "11111111-1111-4111-8111-222222222222",
  title: "New session",
  created_at: "2026-08-25T12:00:00Z",
  updated_at: "2026-08-25T12:00:00Z",
};

const SOURCE_ID = "00000000-0000-4000-8000-000000000601";
const EXPLODE_ID = "00000000-0000-4000-8000-000000000602";
const COLLECTOR_ID = "00000000-0000-4000-8000-000000000603";
const OUTPUT_ID = "00000000-0000-4000-8000-000000000604";
const EDGE_IDS = Array.from(
  { length: 6 },
  (_, index) => `00000000-0000-4000-8000-${String(610 + index).padStart(12, "0")}`,
);

// Composition state with the collector node carrying its scope binding — the
// strict composition decoder (exactRecord) is a second consumer of the
// collector shape, scope_on_group_failure deliberately absent (deleted field).
const compositionState = {
  id: "00000000-0000-4000-8000-000000000600",
  session_id: session.id,
  version: 1,
  sources: {
    source: {
      plugin: "inline_blob",
      options: { rows: [{ doc: "a" }] },
      on_success: "rows",
      on_validation_failure: "discard",
    },
  },
  nodes: [
    {
      id: "explode",
      node_type: "transform",
      plugin: "json_explode",
      input: "rows",
      on_success: "sections",
      on_error: "discard",
      options: {},
    },
    {
      id: "stitcher",
      node_type: "collector",
      plugin: "batch_stats",
      input: "sections",
      on_success: "output",
      on_error: null,
      options: {},
      scope_name: "document_sections",
      scope_opener: "explode",
      scope_policy: "require_all",
    },
  ],
  edges: [],
  outputs: [
    {
      name: "output",
      plugin: "json",
      options: {},
      on_write_failure: "discard",
    },
  ],
  metadata: { name: null, description: null },
  is_valid: true,
  validation_errors: [],
  validation_warnings: [],
  validation_suggestions: [],
  derived_from_state_id: null,
  created_at: "2026-08-25T12:00:00Z",
  composer_meta: null,
  plugin_policy_findings: [],
};

function guidedSession(step: string): Record<string, unknown> {
  return {
    step,
    history: [],
    terminal: null,
    chat_history: [],
    chat_turn_seq: 0,
    profile: { coaching: true, bookends: true },
  };
}

// The propose_pipeline turn: a collector-bearing proposal in the exact closed
// projection shape validate_payload emits. The collector behavior carries the
// opener's stable id and the closed policy; no collector node_error edge is
// present (on_error is OPTIONAL on a collector — group failure is structural).
const proposeTurn: Record<string, unknown> = {
  type: "propose_pipeline",
  step_index: 2,
  turn_token: "b".repeat(64),
  payload: {
    proposal_id: "00000000-0000-4000-8000-000000000600",
    draft_hash: "d".repeat(64),
    // Non-null: marks this as a revision re-plan so the review primary is
    // offered (the pre-Send auto-proposal withholding keys off null).
    supersedes_draft_hash: "e".repeat(64),
    summary: "guided.proposal.summary.full_graph.v1",
    rationale: "guided.proposal.rationale.review_required.v1",
    component_counts: { sources: 1, nodes: 2, edges: 6, outputs: 1 },
    blockers: [],
    graph: {
      sources: [
        { stable_id: SOURCE_ID, label: "source-1", plugin: { kind: "source", id: "inline_blob" } },
      ],
      edges: [
        {
          stable_id: EDGE_IDS[0],
          from_endpoint: { kind: "source", stable_id: SOURCE_ID },
          to_endpoint: { kind: "node", stable_id: EXPLODE_ID },
          flow: { kind: "source_success", branch: null },
        },
        {
          stable_id: EDGE_IDS[1],
          from_endpoint: { kind: "source", stable_id: SOURCE_ID },
          to_endpoint: { kind: "discard" },
          flow: { kind: "source_validation_failure" },
        },
        {
          stable_id: EDGE_IDS[2],
          from_endpoint: { kind: "node", stable_id: EXPLODE_ID },
          to_endpoint: { kind: "node", stable_id: COLLECTOR_ID },
          flow: { kind: "node_success", branch: null },
        },
        {
          stable_id: EDGE_IDS[3],
          from_endpoint: { kind: "node", stable_id: EXPLODE_ID },
          to_endpoint: { kind: "discard" },
          flow: { kind: "node_error" },
        },
        {
          stable_id: EDGE_IDS[4],
          from_endpoint: { kind: "node", stable_id: COLLECTOR_ID },
          to_endpoint: { kind: "output", stable_id: OUTPUT_ID },
          flow: { kind: "node_success", branch: null },
        },
        {
          stable_id: EDGE_IDS[5],
          from_endpoint: { kind: "output", stable_id: OUTPUT_ID },
          to_endpoint: { kind: "discard" },
          flow: { kind: "output_write_failure" },
        },
      ],
    },
    nodes: [
      {
        stable_id: EXPLODE_ID,
        label: "node-1",
        node_type: "transform",
        plugin: { kind: "transform", id: "json_explode" },
        behavior: { kind: "transform" },
        node_options_summary: [],
      },
      {
        stable_id: COLLECTOR_ID,
        label: "node-2",
        node_type: "collector",
        plugin: { kind: "transform", id: "batch_stats" },
        behavior: {
          kind: "collector",
          opener_stable_id: EXPLODE_ID,
          policy: "require_all",
        },
        node_options_summary: [],
      },
    ],
    outputs: [
      { stable_id: OUTPUT_ID, label: "output-1", plugin: { kind: "sink", id: "json" } },
    ],
    edit_targets: [
      { kind: "node", stable_id: EXPLODE_ID },
      { kind: "node", stable_id: COLLECTOR_ID },
    ],
  },
};

// The step_4_wire turn: the collector on the wire review surface — batch-in
// cardinality, behavior details naming the opener, edge rows, and a wire
// advisory for the collector shape.
const wireTurn: Record<string, unknown> = {
  type: "confirm_wiring",
  step_index: 3,
  turn_token: "c".repeat(64),
  payload: {
    proposal_id: "00000000-0000-4000-8000-000000000600",
    draft_hash: "d".repeat(64),
    sources: [
      {
        stable_id: SOURCE_ID,
        label: "Source",
        plugin: "inline_blob",
        on_validation_failure: "discard",
        guaranteed_fields: ["doc"],
        row_cardinality: { input: "none", output: "zero_or_many", expected_output_count: null },
      },
    ],
    nodes: [
      {
        stable_id: EXPLODE_ID,
        label: "Explode step",
        node_type: "transform",
        plugin: "json_explode",
        behavior: { kind: "transform" },
        node_options_summary: [],
        required_fields: ["doc"],
        guaranteed_fields: ["doc", "section"],
        row_cardinality: { input: "one", output: "zero_or_many", expected_output_count: null },
        structured_output_fields: [],
      },
      {
        stable_id: COLLECTOR_ID,
        label: "Collector step",
        node_type: "collector",
        plugin: "batch_stats",
        behavior: {
          kind: "collector",
          opener_stable_id: EXPLODE_ID,
          policy: "require_all",
        },
        node_options_summary: [],
        required_fields: ["section"],
        guaranteed_fields: ["doc", "section_count"],
        row_cardinality: { input: "batch", output: "zero_or_many", expected_output_count: null },
        structured_output_fields: [],
      },
    ],
    outputs: [
      {
        stable_id: OUTPUT_ID,
        label: "Summary output",
        plugin: "json",
        on_write_failure: "discard",
        required_fields: ["section_count"],
        business_schema: {
          mode: "observed",
          fields: [],
          guaranteed_fields: [],
          required_fields: ["section_count"],
        },
      },
    ],
    connections: [
      {
        stable_id: EDGE_IDS[0],
        from_endpoint: { kind: "source", stable_id: SOURCE_ID },
        to_endpoint: { kind: "node", stable_id: EXPLODE_ID },
        flow: { kind: "source_success", branch: null },
        schema_contract: {
          from: "source",
          to: "explode",
          producer_guarantees: ["doc"],
          consumer_requires: ["doc"],
          missing_fields: [],
          satisfied: true,
        },
      },
      {
        stable_id: EDGE_IDS[2],
        from_endpoint: { kind: "node", stable_id: EXPLODE_ID },
        to_endpoint: { kind: "node", stable_id: COLLECTOR_ID },
        flow: { kind: "node_success", branch: null },
        schema_contract: {
          from: "explode",
          to: "stitcher",
          producer_guarantees: ["doc", "section"],
          consumer_requires: ["section"],
          missing_fields: [],
          satisfied: true,
        },
      },
      {
        stable_id: EDGE_IDS[4],
        from_endpoint: { kind: "node", stable_id: COLLECTOR_ID },
        to_endpoint: { kind: "output", stable_id: OUTPUT_ID },
        flow: { kind: "node_success", branch: null },
        schema_contract: null,
      },
    ],
    semantic_contracts: [],
    warnings: [
      {
        component: "stitcher",
        severity: "medium",
        message:
          "Collector 'stitcher' closes the scope opened by 'explode' with require_all: one lost section row fails the whole document group. Continuing is allowed.",
      },
    ],
    blockers: [],
    can_confirm: true,
  },
};

function completedSession(): Record<string, unknown> {
  return {
    ...guidedSession("step_4_wire"),
    terminal: {
      kind: "completed",
      reason: null,
      pipeline_yaml: "sources:\n  source:\n    plugin: inline_blob\n",
    },
  };
}

interface FixtureState {
  guidedRespondCount: number;
  requestLog: string[];
}

async function installCollectorRoutes(page: Page, state: FixtureState): Promise<void> {
  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/api/system/status" && method === "GET") {
      await route.fulfill({
        json: {
          composer_available: true,
          composer_model: "gpt-5.5",
          composer_provider: "test",
          composer_reason: null,
          composer_missing_keys: [],
          composer_timeout_seconds: 180,
          tutorial_ready: true,
          tutorial_reason: null,
          plugin_policy_readiness: {
            tutorial_ready: true,
            rows: [
              "policy_compilation",
              "required_core",
              "local_capability_configuration",
              "live_health",
              "tutorial_profile",
              "tutorial_required_control_coverage",
            ].map((id) => ({
              id,
              label: id,
              status: "ok",
              summary: "Ready for the collector fixture.",
              detail: null,
            })),
          },
        },
      });
      return;
    }

    if (path === "/api/composer-preferences" && method === "GET") {
      await route.fulfill({
        json: {
          default_mode: "guided",
          banner_dismissed_at: null,
          tutorial_completed_at: null,
          updated_at: null,
        },
      });
      return;
    }

    if (path === "/api/composer-preferences" && method === "PATCH") {
      await route.fulfill({
        json: {
          default_mode: "guided",
          banner_dismissed_at: null,
          tutorial_completed_at: null,
          updated_at: "2026-08-25T12:11:00Z",
        },
      });
      return;
    }

    if (path === "/api/sessions" && method === "GET") {
      await route.fulfill({ json: [session] });
      return;
    }

    if (path === "/api/sessions" && method === "POST") {
      await route.fulfill({ json: session });
      return;
    }

    if (path === `/api/sessions/${session.id}` && method === "PATCH") {
      const body = request.postDataJSON() as Record<string, unknown>;
      await route.fulfill({
        json: { ...session, title: body.title, updated_at: "2026-08-25T12:11:00Z" },
      });
      return;
    }

    if (path === `/api/sessions/${session.id}/guided/start` && method === "POST") {
      state.requestLog.push("guided-start");
      await route.fulfill({
        json: {
          guided_session: guidedSession("step_3_transforms"),
          next_turn: proposeTurn,
          terminal: null,
          composition_state: compositionState,
        },
      });
      return;
    }

    if (path === `/api/sessions/${session.id}/guided/tutorial-sample` && method === "GET") {
      await route.fulfill({ json: { sample_urls: [] } });
      return;
    }

    if (path === `/api/sessions/${session.id}/guided` && method === "GET") {
      await route.fulfill({
        json: {
          guided_session: guidedSession("step_3_transforms"),
          next_turn: proposeTurn,
          terminal: null,
          composition_state: compositionState,
        },
      });
      return;
    }

    if (path === `/api/sessions/${session.id}/guided/respond` && method === "POST") {
      state.guidedRespondCount += 1;
      const n = state.guidedRespondCount;
      state.requestLog.push(`guided-respond:${n}`);
      // Deterministic sequencing: accept proposal → wire turn → confirm →
      // completed.
      let next: Record<string, unknown> | null;
      let sessionBody = guidedSession("step_4_wire");
      if (n === 1) {
        next = wireTurn;
      } else {
        next = null;
        sessionBody = completedSession();
      }
      const terminal = next === null ? (sessionBody.terminal as Record<string, unknown>) : null;
      await route.fulfill({
        json: {
          guided_session: sessionBody,
          next_turn: next,
          terminal,
          composition_state: compositionState,
        },
      });
      return;
    }

    if (path === `/api/sessions/${session.id}/interpretations` && method === "GET") {
      await route.fulfill({ json: { events: [] } });
      return;
    }

    if (path === `/api/sessions/${session.id}/composer/preferences` && method === "GET") {
      await route.fulfill({
        json: {
          session_id: session.id,
          trust_mode: "explicit_approve",
          density_default: "medium",
          interpretation_review_disabled: false,
          updated_at: "2026-08-25T12:00:00Z",
        },
      });
      return;
    }

    if (path === `/api/sessions/${session.id}/composer-progress` && method === "GET") {
      await route.fulfill({
        json: {
          session_id: session.id,
          request_id: null,
          phase: "idle",
          headline: "Idle.",
          evidence: [],
          likely_next: null,
          reason: "composer_idle",
          updated_at: "2026-08-25T12:00:00Z",
        },
      });
      return;
    }

    if (path === `/api/sessions/${session.id}/state` && method === "GET") {
      await route.fulfill({ json: compositionState });
      return;
    }

    if (path === `/api/sessions/${session.id}/state/versions` && method === "GET") {
      await route.fulfill({
        json: [
          {
            id: compositionState.id,
            version: compositionState.version,
            created_at: "2026-08-25T12:00:00Z",
            node_count: compositionState.nodes.length,
          },
        ],
      });
      return;
    }

    if (path === `/api/sessions/${session.id}/messages` && method === "GET") {
      await route.fulfill({ json: [] });
      return;
    }

    if (path === `/api/sessions/${session.id}/proposals` && method === "GET") {
      await route.fulfill({ json: [] });
      return;
    }

    if (path === `/api/sessions/${session.id}/validate` && method === "POST") {
      await route.fulfill({
        json: {
          is_valid: true,
          summary: "Collector pipeline is valid.",
          checks: [],
          errors: [],
          warnings: [],
          semantic_contracts: [],
        },
      });
      return;
    }

    if (path === `/api/sessions/${session.id}/audit-readiness` && method === "GET") {
      await route.fulfill({
        json: {
          session_id: session.id,
          composition_version: 1,
          checked_at: "2026-08-25T12:11:00Z",
          rows: [],
          validation_result: {
            is_valid: true,
            summary: "Collector pipeline is valid.",
            checks: [],
            errors: [],
            warnings: [],
            semantic_contracts: [],
          },
        },
      });
      return;
    }

    if (path === `/api/sessions/${session.id}/runs` && method === "GET") {
      await route.fulfill({ json: [] });
      return;
    }

    if (path === `/api/sessions/${session.id}/blobs` && method === "GET") {
      await route.fulfill({ json: [] });
      return;
    }

    if (path === "/api/tutorial/orphans" && method === "DELETE") {
      await route.fulfill({ json: { deleted_count: 0 } });
      return;
    }

    if (path === "/api/tutorial/run" && method === "POST") {
      await route.fulfill({
        json: {
          run_id: "run-1",
          output: {
            source_data_hash: "c0llect0rhash",
            rows: [{ doc: "a", section_count: 3 }],
            discarded_row_count: 0,
          },
          seeded_from_cache: false,
          cache_key: null,
        },
      });
      return;
    }

    await route.continue();
  });
}

test.describe("guided collector authoring (mocked wire-contract canary)", () => {
  test("propose (collector projection) → wire review → confirm → completed", async ({
    page,
  }) => {
    const state: FixtureState = { guidedRespondCount: 0, requestLog: [] };
    await installCollectorRoutes(page, state);

    await page.goto("/");
    await page.getByRole("button", { name: "Let's go" }).click();
    await expect(page.getByLabel(/guided composer/i)).toBeVisible();

    // ── Proposal turn: the collector projection renders for review ──────────
    // The strict decoder admitted the collector node shape (or this heading
    // never mounts), and the review card names the node kind, its plugin, the
    // opener (by ordinal label), and the closed policy in plain words.
    await expect(page.getByText("node-2 · collector · batch_stats")).toBeVisible();
    await expect(
      page.getByText(/Collects every row expanded by node-1 and releases the group as one batch, requiring every member to arrive\./),
    ).toBeVisible();
    // Topology rendering: the read-only proposal graph renders the collector
    // node as its own kind (badge/kind class), never as a gate or transform.
    await expect(page.locator('[data-node-kind="collector"]')).toBeVisible();

    // ── Accept the proposal → wire review ───────────────────────────────────
    await page.getByRole("button", { name: "Review wiring", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Review wiring" })).toBeVisible();

    // Edge rows render post-M1 from/to naming for the collector edges.
    await expect(
      page.getByRole("listitem", { name: /Explode step .* to Collector step/ }),
    ).toBeVisible();
    await expect(
      page.getByRole("listitem", { name: /Collector step .* to Summary output/ }),
    ).toBeVisible();
    await expect(page.getByRole("listitem", { name: /from_id|to_id/ })).toHaveCount(0);

    // Collector behavior details: opener named by label, closed policy, and
    // the batch-in cardinality claim.
    await expect(page.getByText("Closes the row group opened by Collector step")).toHaveCount(0);
    await expect(page.getByText("Closes the row group opened by Explode step")).toBeVisible();
    await expect(page.getByText("Policy: require all")).toBeVisible();
    await expect(page.getByText(/Cardinality: batch → zero or many/)).toBeVisible();

    // Advisory: the wire-stage warning for the collector shape renders.
    await expect(page.getByText(/one lost section row fails the whole document group/i)).toBeVisible();

    // ── Confirm → completed (turn sequencing pin) ───────────────────────────
    await page.getByRole("button", { name: "Confirm wiring", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: /Running your pipeline/i }),
    ).toBeVisible();

    expect(state.requestLog).toEqual([
      "guided-start",
      "guided-respond:1",
      "guided-respond:2",
    ]);
  });
});
