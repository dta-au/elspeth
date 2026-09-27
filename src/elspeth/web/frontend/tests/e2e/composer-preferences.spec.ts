// The only Composer authoring surface is freeform; preferences remain useful
// for theme, detail level, and restarting the first-run tutorial.
import { expect, test } from "@playwright/test";

import { authedContext, setShowAdvanced, tokenFromStorageState } from "./helpers/api";
import { ComposerPage } from "./page-objects/composer-page";

test.describe("composer preferences", () => {
  test("freeform chat opens directly in new and resumed sessions", async ({ page }) => {
    const composer = new ComposerPage(page);
    await composer.goto();
    await composer.createSession("First session");
    await expect(page.getByLabel("Chat panel", { exact: true })).toBeVisible();
    await page.reload();
    await composer.waitForChatReady();
    await composer.createSession("Next session");
    await expect(page.getByLabel("Chat panel", { exact: true })).toBeVisible();
  });

  test("narrow account-menu preferences save detail level without a mode field", async ({ page }) => {
    const composer = new ComposerPage(page);
    await composer.goto();
    await composer.createSession("Preferences session");
    await page.setViewportSize({ width: 600, height: 900 });
    const writes: Array<Record<string, unknown>> = [];
    page.on("request", (request) => {
      if (request.method() === "PATCH" && request.url().includes("/api/composer-preferences")) {
        writes.push(request.postDataJSON() as Record<string, unknown>);
      }
    });
    await page.getByRole("button", { name: /account/i }).click();
    await page.getByRole("button", { name: /composer preferences/i }).click();
    const dialog = page.getByRole("dialog", { name: /composer preferences/i });
    await expect(dialog.getByRole("group", { name: "Theme" })).toBeVisible();
    await expect(dialog.getByRole("group", { name: "Detail level" })).toBeVisible();
    await expect(dialog.getByRole("group", { name: /default mode/i })).toHaveCount(0);
    try {
      await dialog.getByRole("radio", { name: "Show technical detail" }).check();
      await expect.poll(() => writes.length).toBeGreaterThan(0);
      expect(writes.every((body) => !("default_mode" in body))).toBe(true);
      await expect(dialog.getByRole("button", { name: "Reset tutorial" })).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
    } finally {
      const ctx = await authedContext(tokenFromStorageState(await page.context().storageState()));
      try {
        await setShowAdvanced(ctx, false);
      } finally {
        await ctx.dispose();
      }
    }
  });
});
