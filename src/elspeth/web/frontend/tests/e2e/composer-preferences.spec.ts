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

  test("mode preferences are available from the account menu at narrow width", async ({ page }) => {
    const composer = new ComposerPage(page);
    await composer.goto();
    await composer.createSession("Discovery");
    await page.setViewportSize({ width: 600, height: 900 });
    const requests: string[] = [];
    page.on("request", (request) => {
      if (request.method() === "POST" && /\/guided\//.test(request.url())) requests.push(request.url());
    });
    await expect(page.getByRole("button", { name: "Composer options", exact: true })).toHaveCount(0);
    const account = page.getByRole("button", { name: /account/i });
    await account.focus();
    await page.keyboard.press("Enter");
    const preferences = page.getByRole("button", { name: /composer preferences/i });
    await preferences.focus();
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog", { name: /composer preferences/i });
    await expect(dialog.getByRole("radio", { name: /guided/i })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Switch to guided", exact: true })).toHaveCount(0);
    expect(requests).toEqual([]);
  });

  // Guided mode is being retired: the preference was its only entry point, so
  // the option is disabled. It cannot be chosen, no write is sent, and a new
  // session stays Freeform.
  test("the Guided preference is disabled and new sessions stay Freeform", async ({ page }) => {
    const composer = new ComposerPage(page);
    await composer.goto();
    await composer.createSession("Existing Freeform session");
    const preferenceWrites: string[] = [];
    page.on("request", (request) => {
      if (request.method() === "PATCH" && request.url().includes("/api/composer-preferences")) preferenceWrites.push(request.url());
    });
    await page.getByRole("button", { name: /account/i }).click();
    await page.getByRole("button", { name: /composer preferences/i }).click();
    const dialog = page.getByRole("dialog", { name: /composer preferences/i });
    const radios = dialog.getByRole("radio");
    await expect(radios.nth(0)).toHaveAccessibleName(/freeform/i);
    await expect(radios.nth(0)).toBeChecked();
    await expect(dialog.getByRole("radio", { name: /guided/i })).toBeDisabled();
    await expect(dialog.getByText(/guided mode is being retired/i)).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByLabel("Chat panel", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: /session switcher/i }).click();
    await page.getByRole("menuitem", { name: "+ New session" }).click();
    await expect(page.getByLabel("Chat panel", { exact: true })).toBeVisible();
    await expect(page.getByLabel("Guided composer", { exact: true })).toHaveCount(0);
    expect(preferenceWrites).toEqual([]);
    const ctx = await authedContext(tokenFromStorageState(await page.context().storageState()));
    try {
      expect(await getDefaultMode(ctx)).toBe("freeform");
    } finally {
      await ctx.dispose();
    }
  });
});
