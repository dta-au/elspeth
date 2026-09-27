// Non-mocked built-bundle smoke: a real plural-sources composition state must
// hydrate into the ordinary freeform Composer without crashing its authoring
// pane. YAML import gives this test a committed graph without provider spend.
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
    expect(imported.ok(), `state import failed: ${imported.status()} ${(await imported.text()).slice(0, 500)}`).toBe(true);

    const composer = new ComposerPage(page);
    await composer.goto(sessionId);
    await composer.waitForChatReady();
    await expect(page.getByLabel("Chat panel", { exact: true })).toBeVisible();
    await expect(page.getByText(/Chat panel encountered an error/i)).toHaveCount(0);
    await page.reload();
    await composer.waitForChatReady();
    await expect(page.getByLabel("Chat panel", { exact: true })).toBeVisible();
    await expect(page.getByText(/Chat panel encountered an error/i)).toHaveCount(0);
  } finally {
    if (sessionId !== undefined) await deleteSession(ctx, sessionId);
    await ctx.dispose();
  }
});
