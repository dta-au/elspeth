import { expect, test } from "@playwright/test";

import type { CompositionState } from "@/types";
import { deleteWorkspaceScenario, installWorkspaceScenario } from "./helpers/workspace-fixtures";
import { ComposerPage } from "./page-objects/composer-page";

// Rendering-only fixture: the standard hermetic config starts local services
// with provider keys blanked. Never submit a composition or execute a run.
test("shows fork sink edges, document details and configured ADI fallback in the browser", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1600, height: 1000 });
  const sessionId = await installWorkspaceScenario(page, "populated-long-transcript");
  const blobId = "f976fd8b-4432-4f8f-bbc3-2d8a9f2114e0";
  const payloadRef = "a".repeat(64);
  let schemaFailures = 0;
  let localValidationResponses = 0;
  const prohibitedRequests: string[] = [];
  // Fail closed if this rendering test accidentally begins paid/runtime work.
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST" && path === `/api/sessions/${sessionId}/validate`) {
      // Mounting the workspace requests readiness automatically. Keep this
      // projection local and explicitly unavailable, never validate fake blobs.
      localValidationResponses += 1;
      await route.fulfill({ status: 503, json: { detail: "Rendering fixture has no executable validation" } });
    } else if (request.method() === "POST" && /\/(compose|runs|execute|preview)(\/|$)/.test(path)) {
      prohibitedRequests.push(path);
      await route.abort("blockedbyclient");
    } else {
      await route.fallback();
    }
  });
  await page.route("**/api/catalog/transforms/azure_document_intelligence/schema", async (route) => {
    schemaFailures += 1;
    await route.fulfill({ status: 503, json: { detail: "Schema unavailable in rendering fixture" } });
  });
  // Keep the real local session/auth lifecycle while projecting an explicit
  // display fixture over its state response. This does not persist fake blobs
  // or claim these ADI credentials authorize pipeline execution.
  await page.route(`**/api/sessions/${sessionId}/state`, async (route) => {
    const response = await route.fetch();
    const original = await response.json() as CompositionState;
    const projected: CompositionState = {
      ...original,
      sources: { source: { plugin: "blob_rows", on_success: "documents", on_validation_failure: "discard", options: {
        blobs: [{ blob_id: blobId, payload_ref: payloadRef, filename: "report.pdf", mime_type: "application/pdf", size_bytes: 42 }],
        schema: { mode: "observed" },
      } } },
      nodes: [
        { id: "read_document", node_type: "transform", plugin: "azure_document_intelligence", input: "documents", on_success: "gate_in", on_error: "discard", options: {
          model_id: "prebuilt-layout", source_mode: "base64", schema: { mode: "observed" },
        } },
        { id: "route_rows", node_type: "gate", plugin: null, input: "gate_in", on_success: null, on_error: "discard", condition: "True", routes: { true: "fork", false: "fork" }, fork_to: ["sink_a", "sink_b"], options: {} },
      ],
      edges: [],
      outputs: ["sink_a", "sink_b"].map((name) => ({ name, plugin: "json", on_write_failure: "discard", options: { path: `${name}.json`, schema: { mode: "observed" } } })),
    };
    await route.fulfill({ response, json: projected });
  });
  try {
    const composer = new ComposerPage(page);
    await composer.goto(sessionId);
    await composer.waitForChatReady();
    await composer.artifactTab("Workflow").click();
    for (const sink of ["sink_a", "sink_b"]) {
      const edge = page.locator(`.react-flow__edge[data-testid*="route_rows"][data-testid*="${sink}"] .react-flow__edge-path`);
      await expect(edge).toHaveCount(1);
      await expect(edge).toBeVisible();
      await expect(edge).toHaveAttribute("d", /[MLC]/);
    }
    await page.getByRole("button", { name: "Hide quarantine and discard routes", exact: true }).click();
    for (const sink of ["sink_a", "sink_b"]) {
      await expect(page.locator(`.react-flow__edge[data-testid*="route_rows"][data-testid*="${sink}"] .react-flow__edge-path`)).toBeVisible();
    }
    await page.locator('.react-flow__node[data-id="source"]').click();
    const sourcePanel = page.getByRole("complementary", { name: "source configuration", exact: true });
    await expect(sourcePanel.getByRole("heading", { name: "source", exact: true })).toBeVisible();
    await expect(sourcePanel.locator("ol")).toHaveText("report.pdf — application/pdf, 42 bytes");
    const metadata = sourcePanel.locator("details").filter({ has: page.locator("summary", { hasText: "Document metadata" }) });
    await expect(metadata).not.toHaveAttribute("open");
    await expect(metadata.locator("code")).not.toBeVisible();
    await metadata.locator("summary").click();
    await expect(metadata.locator("code")).toBeVisible();
    const stored = JSON.parse(await metadata.locator("code").innerText());
    expect(stored).toEqual([{ blob_id: blobId, payload_ref: payloadRef, filename: "report.pdf", mime_type: "application/pdf", size_bytes: 42 }]);

    await sourcePanel.screenshot({ path: testInfo.outputPath("document-metadata.png") });

    await page.locator('.react-flow__node[data-id="read_document"]').click();
    const adiPanel = page.getByRole("complementary", { name: "read_document configuration", exact: true });
    await expect(adiPanel.getByRole("heading", { name: "read_document", exact: true })).toBeVisible();
    for (const text of ["Model", "prebuilt-layout", "Source mode", "base64", "Row schema", "observed"]) {
      await expect(adiPanel.getByText(text, { exact: true })).toBeVisible();
    }
    // The catalog is lazy: first prove the unloaded fallback, then load the
    // real local catalog and prove a schema fetch failure keeps those values.
    await page.getByRole("button", { name: "Plugin catalog", exact: true }).click();
    await expect.poll(() => schemaFailures).toBeGreaterThan(0);
    const catalog = page.getByRole("dialog", { name: "Plugin catalog", exact: true });
    const catalogOpener = page.getByRole("button", { name: "Plugin catalog", exact: true });
    const primaryNotice = page.getByTestId("app-notice-primary");
    // The local readiness/schema refusal produces a real notice. Its reserved
    // viewport band must not intercept the drawer's ordinary pointer Close.
    await expect(primaryNotice).toBeVisible();
    await expect(primaryNotice).toHaveAttribute("role", /^(alert|status)$/);
    for (const width of [1600, 600]) {
      await page.setViewportSize({ width, height: 1000 });
      const close = catalog.getByRole("button", { name: "Close plugin catalog", exact: true });
      await expect(close).toBeVisible();
      const noticeBox = await primaryNotice.boundingBox();
      const closeBox = await close.boundingBox();
      expect(noticeBox).not.toBeNull();
      expect(closeBox).not.toBeNull();
      expect(closeBox!.y).toBeGreaterThanOrEqual(noticeBox!.y + noticeBox!.height);
      expect(await close.evaluate((button) => {
        const bounds = button.getBoundingClientRect();
        const hit = document.elementFromPoint(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
        return hit !== null && button.contains(hit);
      })).toBe(true);
      await close.click();
      await expect(catalog).not.toBeVisible();
      await expect(catalogOpener).toBeFocused();
      await expect(primaryNotice).toBeVisible();
      await catalogOpener.click();
      await expect(catalog).toBeVisible();
    }
    await page.keyboard.press("Tab");
    expect(await catalog.evaluate((dialog) => dialog.contains(document.activeElement))).toBe(true);
    await page.keyboard.press("Escape");
    await expect(catalog).not.toBeVisible();
    await expect(catalogOpener).toBeFocused();
    await page.setViewportSize({ width: 1600, height: 1000 });
    for (const text of ["prebuilt-layout", "base64", "observed"]) {
      await expect(adiPanel.getByText(text, { exact: true })).toBeVisible();
    }
    expect(prohibitedRequests).toEqual([]);
    await testInfo.attach("rendering-request-controls", {
      body: JSON.stringify({ schemaFailures, localValidationResponses, prohibitedRequests }), contentType: "application/json",
    });
    await page.locator(".graph-view-shell").screenshot({ path: testInfo.outputPath("document-fork-inspector.png") });
  } catch (error) {
    await page.screenshot({ path: testInfo.outputPath("display-before-cleanup.png"), fullPage: true });
    throw error;
  } finally {
    await deleteWorkspaceScenario(page, sessionId);
  }
});
