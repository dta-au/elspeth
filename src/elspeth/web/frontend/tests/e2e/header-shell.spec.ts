import { expect, test } from "@playwright/test";

import { ComposerPage } from "./page-objects/composer-page";
import { deleteWorkspaceScenario, installWorkspaceScenario } from "./helpers/workspace-fixtures";

test.describe("header-owned shell", () => {
  for (const theme of ["dark", "light"] as const) {
    for (const viewport of [{ width: 1440, height: 900 }, { width: 1280, height: 720 }]) {
      test(`all five header pills share a height with long model names in ${theme} at ${viewport.width}x${viewport.height}`, async ({ page }) => {
        await page.setViewportSize(viewport);
        await page.addInitScript((selectedTheme) => {
          window.localStorage.setItem("elspeth_theme", selectedTheme);
        }, theme);
        // A seeded composition: the version selector only renders once the
        // session has composition state, and the test needs all five pills.
        const sessionId = await installWorkspaceScenario(page, "populated-long-transcript");
        const longModel = `provider/${"very-long-model-name-".repeat(8)}4.6`;
        await page.route("**/api/system/status", async (route) => {
          await route.fulfill({ json: {
            composer_available: false,
            composer_model: longModel,
            composer_advisor_model: "anthropic/claude-sonnet-4.6",
            composer_provider: "playwright-route",
            composer_reason: "Provider unavailable in this deterministic test.",
            composer_missing_keys: [],
            composer_timeout_seconds: 180,
          } });
        });
        const composer = new ComposerPage(page);
        try {
          await composer.goto(sessionId);
          await composer.waitForChatReady();
          await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
          const header = page.getByRole("banner");
          const chips = header.locator(".chat-model-chip");
          await expect(chips).toHaveCount(2);
          await expect(chips.nth(1)).toHaveText("Advisor: Claude Sonnet 4.6");
          const controls = [
            header.getByRole("button", { name: /Session switcher:/ }),
            header.getByRole("button", { name: /Composition history/ }),
            chips.nth(0),
            chips.nth(1),
            header.getByRole("button", { name: /account menu/i }),
          ];
          const headerBox = await header.boundingBox();
          expect(headerBox).not.toBeNull();
          for (const control of controls) {
            await expect(control).toBeVisible();
            const box = await control.boundingBox();
            expect(box).not.toBeNull();
            expect(box!.height).toBe(36);
            expect(box!.y).toBeGreaterThanOrEqual(headerBox!.y);
            expect(headerBox!.y + headerBox!.height - box!.y - box!.height).toBeGreaterThanOrEqual(4);
          }
          await expect(chips.nth(0)).toHaveAttribute("title", longModel);
          const truncation = await chips.nth(0).evaluate((chip) => ({
            overflow: getComputedStyle(chip).overflow,
            textOverflow: getComputedStyle(chip).textOverflow,
            whiteSpace: getComputedStyle(chip).whiteSpace,
            clipped: chip.scrollWidth > chip.clientWidth,
          }));
          expect(truncation).toEqual({ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", clipped: true });
        } finally {
          await deleteWorkspaceScenario(page, sessionId);
        }
      });
    }
  }

  test("renders the header-owned session and account controls without the legacy sidebar", async ({
    page,
  }) => {
    const composer = new ComposerPage(page);
    await composer.goto();

    await expect(page.getByRole("banner")).toBeVisible();
    await expect(
      page.getByRole("button", { name: /session switcher/i }),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: /account menu/i })).toBeVisible();
    await expect(page.getByLabel("Sessions sidebar")).toHaveCount(0);

    await page.getByRole("button", { name: /account menu/i }).click();
    await expect(
      page.getByRole("button", { name: /switch to (light|dark) theme/i }),
    ).toBeVisible();
  });
});
