import { expect, test, type Page } from "@playwright/test";
import { join } from "node:path";
import { authedContext, createSession, deleteSession, seedCompositionState, tokenFromStorageState, uploadBlob, type CompositionStateSeed } from "./helpers/api";
import { ComposerPage } from "./page-objects/composer-page";

// React Flow drops a controlled node's measured size and handle bounds whenever it
// receives a new node object without `measured`, and hides that node's edges until
// it re-measures. The graph rebuilds every node object on each validation result,
// selection or route toggle, so the whole edge layer used to blank and redraw.
// Observe the edge layer from first paint and require that, once drawn, it never
// empties; and that a route toggle, which changes each node's handle set, still
// draws every edge. stage_2 sends success and error to one sink, so that pair is
// drawn on parallel handles that the toggle removes and restores.
async function watchEdgeLayer(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const state = { drawn: false, blanks: 0, last: -1 };
    (window as unknown as { __edgeLayer: typeof state }).__edgeLayer = state;
    new MutationObserver(() => {
      const count = document.querySelectorAll(".react-flow__edge[data-testid]").length;
      if (count === state.last) return;
      if (state.drawn && count === 0 && document.querySelector(".react-flow__node")) state.blanks += 1;
      if (count > 0) state.drawn = true;
      state.last = count;
    }).observe(document, { subtree: true, childList: true });
  });
}

const edgeBlanks = (page: Page) => page.evaluate(() => (window as unknown as { __edgeLayer: { blanks: number } }).__edgeLayer.blanks);
const edgeIds = (page: Page) => page.locator(".react-flow__edge[data-testid]").evaluateAll(
  (elements) => elements.map((element) => element.getAttribute("data-testid")).sort(),
);

test("graph edges stay drawn while the view loads, selects and toggles routes", async ({ page }) => {
  const ctx = await authedContext(tokenFromStorageState(await page.context().storageState()));
  const session = await createSession(ctx, "Graph edge stability");
  const blob = await uploadBlob(ctx, session.id, "synthetic.csv", "value,keep\nsynthetic,true\n");
  const dataDir = process.env.PLAYWRIGHT_E2E_DATA_DIR;
  if (!dataDir) throw new Error("Isolated harness data directory is required");
  const state: CompositionStateSeed = {
    version: 1,
    metadata: { name: "Graph edge stability", description: "Rendering fixture; never executed" },
    sources: { source: { plugin: "csv", on_success: "input_0", on_validation_failure: "quarantine", options: { path: join(dataDir, "blobs", session.id, `${blob.id}_synthetic.csv`), blob_ref: blob.id, schema: { mode: "observed" } } } },
    nodes: [
      { id: "stage_0", node_type: "transform", plugin: "field_mapper", input: "input_0", on_success: "input_1", on_error: "shared_sink", condition: null, routes: null, fork_to: null, branches: null, policy: null, merge: null, options: { mapping: { value: "value" }, schema: { mode: "observed" } } },
      { id: "stage_1", node_type: "gate", plugin: null, input: "input_1", on_success: null, on_error: "discard", condition: "row.get('keep') == True", routes: { "true": "input_2", "false": "shared_sink" }, fork_to: null, branches: null, policy: null, merge: null, options: {} },
      { id: "stage_2", node_type: "transform", plugin: "field_mapper", input: "input_2", on_success: "shared_sink", on_error: "shared_sink", condition: null, routes: null, fork_to: null, branches: null, policy: null, merge: null, options: { mapping: { value: "value" }, schema: { mode: "observed" } } },
    ],
    edges: [],
    outputs: ["shared_sink", "quarantine"].map((name) => ({ name, plugin: "json", on_write_failure: "discard", options: { path: `outputs/${name}.json`, schema: { mode: "observed" } } })),
  };
  try {
    await seedCompositionState(ctx, session.id, state);
    await watchEdgeLayer(page);
    await page.setViewportSize({ width: 1600, height: 1000 });
    const composer = new ComposerPage(page);
    await composer.goto(session.id);
    await composer.waitForChatReady();
    await expect(page.locator('.react-flow__node[data-id="shared_sink"]')).toBeVisible();
    await page.waitForLoadState("networkidle");
    await expect.poll(async () => (await edgeIds(page)).length).toBeGreaterThan(3);
    const topology = await edgeIds(page);

    await page.locator('.react-flow__node[data-id="stage_0"]').click();
    await page.locator('.react-flow__node[data-id="stage_2"]').click();
    const failureRoutes = page.getByText("Show quarantine and discard routes");
    await failureRoutes.click();
    await expect.poll(async () => (await edgeIds(page)).length).toBeLessThan(topology.length);
    await failureRoutes.click();
    await expect.poll(() => edgeIds(page)).toEqual(topology);

    expect(await edgeBlanks(page)).toBe(0);
  } finally {
    await deleteSession(ctx, session.id);
  }
});
