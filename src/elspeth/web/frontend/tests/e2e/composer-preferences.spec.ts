// Uses only the isolated Playwright deployment and its disposable account.
import { expect, test } from "@playwright/test";

import { authedContext, getDefaultMode, setDefaultMode, tokenFromStorageState } from "./helpers/api";
import { ComposerPage } from "./page-objects/composer-page";

test.describe("composer preferences", () => {
  test.beforeEach(async ({ page }) => {
    const ctx = await authedContext(tokenFromStorageState(await page.context().storageState()));
    try {
      await setDefaultMode(ctx, "freeform");
    } finally {
      await ctx.dispose();
    }
  });

  test("Freeform opens directly and survives reload and a new session", async ({ page }) => {
    const composer = new ComposerPage(page);
    await composer.goto();
    await composer.createSession("Freeform default");
    await expect(page.getByLabel("Chat panel", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Switch to guided", exact: true })).toHaveCount(0);
    await page.reload();
    await composer.waitForChatReady();
    await composer.createSession("Next Freeform session");
    const ctx = await authedContext(tokenFromStorageState(await page.context().storageState()));
    try {
      expect(await getDefaultMode(ctx)).toBe("freeform");
    } finally {
      await ctx.dispose();
    }
  });

  test("Guided is deliberately discovered with keyboard at narrow width", async ({ page }) => {
    const composer = new ComposerPage(page);
    await composer.goto();
    await composer.createSession("Discovery");
    await page.setViewportSize({ width: 600, height: 900 });
    const requests: string[] = [];
    page.on("request", (request) => {
      if (request.method() === "POST" && /\/guided\//.test(request.url())) requests.push(request.url());
    });
    const options = page.getByRole("button", { name: "Composer options", exact: true });
    await options.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("button", { name: "Switch to guided", exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(options).toBeFocused();
    await expect(page.getByRole("button", { name: "Switch to guided", exact: true })).toHaveCount(0);
    expect(requests).toEqual([]);
  });

  test("an explicit Guided preference affects new sessions and survives reload", async ({ page }) => {
    const composer = new ComposerPage(page);
    await composer.goto();
    await composer.createSession("Existing Freeform session");
    await page.getByRole("button", { name: /account/i }).click();
    await page.getByRole("button", { name: /composer preferences/i }).click();
    const dialog = page.getByRole("dialog", { name: /composer preferences/i });
    const radios = dialog.getByRole("radio");
    await expect(radios.nth(0)).toHaveAccessibleName(/freeform/i);
    await expect(radios.nth(0)).toBeChecked();
    const saved = page.waitForResponse((response) => response.url().includes("/api/composer-preferences") && response.request().method() === "PATCH" && response.ok());
    await dialog.getByRole("radio", { name: /guided/i }).click();
    await saved;
    await page.keyboard.press("Escape");
    await expect(page.getByLabel("Chat panel", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: /session switcher/i }).click();
    await page.getByRole("menuitem", { name: "+ New session" }).click();
    await expect(page.getByLabel("Guided composer", { exact: true })).toBeVisible();
    await page.reload();
    await expect(page.getByLabel("Guided composer", { exact: true })).toBeVisible();
    const ctx = await authedContext(tokenFromStorageState(await page.context().storageState()));
    try {
      expect(await getDefaultMode(ctx)).toBe("guided");
    } finally {
      await ctx.dispose();
    }
  });
});
