// Non-mocked built-bundle smoke: an uploaded CSV source and sink must hydrate
// into the ordinary freeform Composer after YAML import and reload.
import { expect, test } from "@playwright/test";

import {
  authedContext,
  createSession,
  deleteSession,
  tokenFromStorageState,
  uploadBlob,
} from "./helpers/api";
import { ComposerPage } from "./page-objects/composer-page";

const YAML = `sources:
  source:
    plugin: csv
    on_success: result
    options:
      path: uploaded.csv
      schema:
        mode: observed
    on_validation_failure: discard
sinks:
  result:
    plugin: csv
    options:
      path: outputs/release-smoke.csv
    on_write_failure: discard
`;

async function assertHydratedSourceGraph(composer: ComposerPage): Promise<void> {
  await composer.artifactTab("Spec").click();
  await expect(composer.page.getByRole("article", { name: "Source source" })).toBeVisible();
  await expect(composer.page.getByRole("article", { name: "Output result" })).toBeVisible();
  await composer.artifactTab("Workflow").click();
  await expect(composer.page.locator(".react-flow__node")).toHaveCount(2);
}

test("built freeform Composer hydrates a committed source graph after reload", async ({ page }) => {
  const token = tokenFromStorageState(await page.context().storageState());
  const ctx = await authedContext(token);
  let sessionId: string | undefined;
  try {
    const session = await createSession(ctx, "release-smoke");
    sessionId = session.id;
    const blob = await uploadBlob(ctx, sessionId, "release-smoke-orders.csv", "id,name,value\n1,widget,42\n");
    const imported = await ctx.post(`/api/sessions/${sessionId}/state/yaml`, {
      data: { yaml: YAML, source_blob_ids: { source: blob.id } },
    });
    const importedState = await imported.json() as { version: number };
    expect(imported.ok(), `state import failed: ${imported.status()} ${JSON.stringify(importedState).slice(0, 500)}`).toBe(true);

    const assertPersistedBinding = async (): Promise<void> => {
      const stateResponse = await ctx.get(`/api/sessions/${sessionId}/state`);
      expect(stateResponse.ok()).toBe(true);
      const state = await stateResponse.json() as {
        version: number;
        sources: Record<string, { options: Record<string, unknown> }>;
      };
      expect(state.version).toBe(importedState.version);
      expect(state.sources.source?.options.blob_ref).toBe(blob.id);
    };

    const composer = new ComposerPage(page);
    await composer.goto(sessionId);
    await composer.waitForChatReady();
    await assertHydratedSourceGraph(composer);
    await assertPersistedBinding();
    await expect(page.getByText(/Chat panel encountered an error/i)).toHaveCount(0);
    await page.reload();
    await composer.waitForChatReady();
    await assertHydratedSourceGraph(composer);
    await assertPersistedBinding();
    await expect(page.getByText(/Chat panel encountered an error/i)).toHaveCount(0);
  } finally {
    if (sessionId !== undefined) await deleteSession(ctx, sessionId);
    await ctx.dispose();
  }
});
