import { expect, test } from "@playwright/test";
import { join } from "node:path";
import { authedContext, createSession, deleteSession, seedCompositionState, tokenFromStorageState, uploadBlob, type CompositionStateSeed } from "./helpers/api";
import { ComposerPage } from "./page-objects/composer-page";

for (const { count, secondSink } of [{ count: 1, secondSink: false }, { count: 3, secondSink: false }, { count: 15, secondSink: false }, { count: 15, secondSink: true }]) {
  test(`synthetic shared sink with ${count} incoming routes${secondSink ? " across two sinks" : ""}`, async ({ page }, testInfo) => {
    const ctx = await authedContext(tokenFromStorageState(await page.context().storageState()));
    const session = await createSession(ctx, `Synthetic shared sink ${count}`);
    const blob = await uploadBlob(ctx, session.id, "synthetic.csv", "value,keep\nsynthetic,true\n");
    const dataDir = process.env.PLAYWRIGHT_E2E_DATA_DIR;
    if (!dataDir) throw new Error("Isolated harness data directory is required");
    const targetSink = (index: number) => secondSink && index % 2 ? "shared_other" : "shared_sink";
    const state: CompositionStateSeed = {
      version: 1,
      metadata: { name: "Synthetic shared sink", description: "Rendering fixture; never executed" },
      sources: { source: { plugin: "csv", on_success: "input_0", on_validation_failure: "quarantine", options: { path: join(dataDir, "blobs", session.id, `${blob.id}_synthetic.csv`), blob_ref: blob.id, schema: { mode: "observed" } } } },
      nodes: Array.from({ length: count }, (_, index) => {
        const last = index === count - 1;
        const gate = index % 3 === 1 && !last;
        return {
          id: `synthetic_stage_${index}_long_name`, node_type: gate ? "gate" as const : "transform" as const,
          plugin: gate ? null : "field_mapper", input: `input_${index}`,
          on_success: gate ? null : last ? targetSink(index) : `input_${index + 1}`,
          on_error: gate || last ? "discard" : targetSink(index),
          condition: gate ? "row.get('keep') == True" : null,
          routes: gate ? { "true": `input_${index + 1}`, "false": targetSink(index) } : null,
          fork_to: null, branches: null, policy: null, merge: null,
          options: gate ? {} : { mapping: { value: "value" }, schema: { mode: "observed" } },
        };
      }),
      edges: [],
      outputs: [...(secondSink ? ["shared_sink", "shared_other"] : ["shared_sink"]), "quarantine"].map((name) => ({ name, plugin: "json", on_write_failure: "discard", options: { path: `outputs/${name}.json`, schema: { mode: "observed" } } })),
    };
    try {
      await seedCompositionState(ctx, session.id, state);
      await page.setViewportSize({ width: 1600, height: 1000 });
      const composer = new ComposerPage(page);
      await composer.goto(session.id);
      await composer.waitForChatReady();
      const sink = page.locator('.react-flow__node[data-id="shared_sink"]');
      await expect(sink).toBeVisible();
      const viewport = page.locator(".react-flow__viewport");
      const readZoom = () => viewport.evaluate((element) => new DOMMatrixReadOnly(getComputedStyle(element).transform).a);
      if (count === 15 && !secondSink) {
        await expect.poll(readZoom).toBeLessThan(0.3);
        expect(await readZoom()).toBeGreaterThanOrEqual(0.15);
      }
      const paths = page.locator('.react-flow__edge[data-testid]');
      const before = await paths.count();
      expect(before).toBeGreaterThan(count);
      const topology = await paths.evaluateAll((elements) => elements.map((element) => element.getAttribute("data-testid")).sort());
      await page.getByText(/^Wiring \(/).click();
      const routes = page.getByRole("list", { name: "Inspect pipeline routes" });
      const arrivals = routes.getByRole("button").filter({ hasText: /→ shared_(sink|other):/ });
      await expect(arrivals).toHaveCount(count);
      for (let index = 0; index < count; index++) {
        const route = arrivals.nth(index);
        await route.focus();
        await expect(route).toHaveAttribute("aria-pressed", "true");
        await expect(page.getByRole("status").filter({ hasText: "Selected route:" })).toHaveText(/Selected route:.*shared_(sink|other)/);
      }
      await routes.getByRole("button").filter({ hasText: "→ shared_sink:" }).last().focus();
      await page.getByText(/^Wiring \(/).click();
      if (count === 15) {
        await page.locator(".react-flow__controls-zoomin").click({ clickCount: 3 });
      }
      for (let attempt = 0; attempt < 12; attempt++) {
        const canvas = await page.locator(".graph-view-diagram").boundingBox();
        const target = await sink.boundingBox();
        if (!canvas || !target) throw new Error("Synthetic graph geometry missing");
        const dx = canvas.x + canvas.width / 2 - (target.x + target.width / 2);
        const dy = canvas.y + canvas.height / 2 - (target.y + target.height / 2);
        if (Math.abs(dx) < 20 && Math.abs(dy) < 20) break;
        const startX = canvas.x + canvas.width - 30;
        const startY = canvas.y + canvas.height / 2;
        await page.mouse.move(startX, startY);
        await page.mouse.down();
        await page.mouse.move(startX + Math.max(-canvas.width / 2, Math.min(20, dx)), startY + Math.max(-canvas.height / 3, Math.min(canvas.height / 3, dy)), { steps: 5 });
        await page.mouse.up();
      }
      await expect(sink).toHaveAttribute("style", /box-shadow/);
      const selectedText = await page.getByRole("status").filter({ hasText: "Selected route:" }).innerText();
      const selectedEdgeTestId = await page.locator(".react-flow__edge.selected").getAttribute("data-testid");
      if (!selectedEdgeTestId) throw new Error("Selected synthetic edge has no identity");
      const visibleCanvas = await page.locator(".graph-view-diagram").boundingBox();
      if (!visibleCanvas) throw new Error("Synthetic canvas is not measurable");
      await page.mouse.click(visibleCanvas.x + visibleCanvas.width - 180, visibleCanvas.y + visibleCanvas.height - 20);
      await expect(page.getByRole("status").filter({ hasText: "Selected route:" })).toHaveCount(0);
      await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
      const clickPoint = await page.getByTestId(selectedEdgeTestId).locator(".react-flow__edge-interaction").evaluate((element) => {
        const path = element as SVGPathElement;
        const matrix = path.getScreenCTM();
        if (!matrix) throw new Error("Synthetic edge has no screen geometry");
        const wrapper = path.closest(".react-flow__edge");
        for (const fraction of [0.2, 0.4, 0.6, 0.8, 0.9, 0.95]) {
          const point = path.getPointAtLength(path.getTotalLength() * fraction);
          const screen = new DOMPoint(point.x, point.y).matrixTransform(matrix);
          if (document.elementFromPoint(screen.x, screen.y)?.closest(".react-flow__edge") === wrapper) {
            return { x: screen.x, y: screen.y };
          }
        }
        throw new Error("Synthetic edge has no unobscured pointer target");
      });
      await page.mouse.click(clickPoint.x, clickPoint.y);
      await expect(page.getByRole("status").filter({ hasText: "Selected route:" })).toHaveText(selectedText);
      await page.locator(".graph-view-shell").screenshot({ path: testInfo.outputPath(`shared-sink-${count}${secondSink ? "-two" : ""}.png`) });
      if (count === 1) {
        const quarantine = page.locator('.react-flow__node[data-id="quarantine"]');
        await expect(quarantine).toBeVisible();
        const retainedGeometry = () => page.locator('.react-flow__node:not([data-id="quarantine"])').evaluateAll((elements) => elements.map((element) => ({
          id: element.getAttribute("data-id"), transform: (element as HTMLElement).style.transform,
        })).sort((left, right) => (left.id ?? "").localeCompare(right.id ?? "")));
        const originalGeometry = await retainedGeometry();
        const originalViewport = await viewport.evaluate((element) => getComputedStyle(element).transform);
        const hide = page.getByRole("button", { name: "Hide quarantine and discard routes" });
        await hide.focus();
        await page.keyboard.press("Space");
        await expect(page.getByRole("button", { name: "Show quarantine and discard routes" })).toHaveAttribute("aria-pressed", "false");
        await expect(quarantine).toHaveCount(0);
        await expect(sink).toBeVisible();
        await expect.poll(retainedGeometry).not.toEqual(originalGeometry);
        expect(await viewport.evaluate((element) => getComputedStyle(element).transform)).toBe(originalViewport);
        await page.keyboard.press("Enter");
        await expect(page.getByRole("button", { name: "Hide quarantine and discard routes" })).toHaveAttribute("aria-pressed", "true");
        await expect(quarantine).toBeVisible();
        await expect.poll(retainedGeometry).toEqual(originalGeometry);
        expect(await viewport.evaluate((element) => getComputedStyle(element).transform)).toBe(originalViewport);
        expect(await paths.evaluateAll((elements) => elements.map((element) => element.getAttribute("data-testid")).sort())).toEqual(topology);
      }
      if (count === 15 && !secondSink) {
        const zoomOut = page.locator(".react-flow__controls-zoomout");
        for (let step = 0; step < 20 && await zoomOut.isEnabled(); step++) await zoomOut.click();
        await expect(zoomOut).toBeDisabled();
        await expect.poll(readZoom).toBeCloseTo(0.15, 5);
        for (let step = 0; step < 5; step++) await page.locator(".react-flow__controls-zoomin").click();
        await expect.poll(readZoom).toBeGreaterThan(0.3);
        await page.locator(".react-flow__controls-fitview").click();
        await expect.poll(readZoom).toBeLessThan(0.3);
        expect(await readZoom()).toBeGreaterThanOrEqual(0.15);
      }
      await page.reload();
      await composer.waitForChatReady();
      await expect(sink).toBeVisible();
      await expect(paths).toHaveCount(before);
      expect(await paths.evaluateAll((elements) => elements.map((element) => element.getAttribute("data-testid")).sort())).toEqual(topology);
    } finally {
      await deleteSession(ctx, session.id);
      await ctx.dispose();
    }
  });
}
