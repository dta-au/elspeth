import { join } from "node:path";

import {
  expect,
  test,
  type APIRequestContext,
  type Locator,
} from "@playwright/test";

import {
  authedContext,
  createSession,
  deleteSession,
  seedCompositionState,
  tokenFromStorageState,
  uploadBlob,
} from "./helpers/api";

const SOURCE_FILENAME = "workspace-geometry.csv";

function sourcePath(sessionId: string, blobId: string): string {
  const dataDir = process.env.PLAYWRIGHT_E2E_DATA_DIR;
  if (!dataDir) {
    throw new Error("PLAYWRIGHT_E2E_DATA_DIR is required for workspace geometry");
  }
  return join(dataDir, "blobs", sessionId, `${blobId}_${SOURCE_FILENAME}`);
}

async function seedPopulatedComposition(
  ctx: APIRequestContext,
  sessionId: string,
): Promise<void> {
  const blob = await uploadBlob(ctx, sessionId, SOURCE_FILENAME, "id\n1\n");
  await seedCompositionState(ctx, sessionId, {
    version: 1,
    metadata: { name: "Workspace geometry", description: "" },
    sources: {
      source: {
        plugin: "csv",
        on_success: "results",
        options: {
          path: sourcePath(sessionId, blob.id),
          blob_ref: blob.id,
          schema: { mode: "observed" },
        },
        on_validation_failure: "discard",
      },
    },
    nodes: [],
    edges: [],
    outputs: [
      {
        name: "results",
        plugin: "csv",
        options: {
          path: "outputs/workspace-geometry.csv",
          schema: { mode: "observed" },
        },
        on_write_failure: "discard",
      },
    ],
  });
}

async function box(locator: Locator): Promise<{ width: number; height: number }> {
  const bounds = await locator.boundingBox();
  return {
    width: bounds?.width ?? 0,
    height: bounds?.height ?? 0,
  };
}

const TASK_5_VIEWPORTS = [
  { width: 1280, height: 720 },
  { width: 1536, height: 760 },
] as const;

test.describe("Composer workspace integration geometry", () => {
  for (const viewport of TASK_5_VIEWPORTS) {
    test(`persistent artifact is operable at ${viewport.width}x${viewport.height}`, async ({
      page,
    }) => {
      await page.setViewportSize(viewport);
      const token = tokenFromStorageState(await page.context().storageState());
      const ctx = await authedContext(token);
      let sessionId: string | undefined;
      try {
        const session = await createSession(
          ctx,
          `workspace ${viewport.width}x${viewport.height}`,
        );
        sessionId = session.id;
        await seedPopulatedComposition(ctx, sessionId);
        await page.goto(`/#/${sessionId}`);

        const authoring = page.getByRole("region", { name: "Authoring pane" });
        const artifact = page.getByRole("region", { name: "Pipeline artifact" });
        const activePanel = page.getByRole("tabpanel", { name: "Graph" });
        await expect(authoring).toBeVisible();
        await expect(artifact).toBeVisible();
        await expect(activePanel).toBeVisible();
        await expect
          .poll(async () => (await box(artifact)).width)
          .toBeGreaterThanOrEqual(640);
        await expect
          .poll(async () => (await box(activePanel)).height)
          .toBeGreaterThanOrEqual(420);

        const validation = page.getByRole("button", { name: /^Validation: / });
        const moreActions = page.getByRole("button", { name: "More actions" });
        await expect(validation).toBeVisible();
        await expect(moreActions).toBeVisible();
        for (const control of [validation, moreActions]) {
          const bounds = await control.boundingBox();
          expect(bounds).not.toBeNull();
          expect(bounds!.x).toBeGreaterThanOrEqual(0);
          expect(bounds!.y).toBeGreaterThanOrEqual(0);
          expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(
            viewport.width,
          );
          expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(
            viewport.height,
          );
        }

        const overflow = await page.evaluate(
          () =>
            document.documentElement.scrollWidth -
            document.documentElement.clientWidth,
        );
        expect(overflow).toBeLessThanOrEqual(0);

        if (viewport.width === 1280 && viewport.height === 720) {
          const invoker = page.getByRole("button", {
            name: `Session switcher: workspace ${viewport.width}x${viewport.height}`,
          });
          await invoker.focus();
          await page.evaluate(() => {
            document.dispatchEvent(
              new KeyboardEvent("keydown", {
                key: "?",
                shiftKey: true,
                bubbles: true,
              }),
            );
          });
          const shortcuts = page.getByRole("dialog", {
            name: "Keyboard Shortcuts",
          });
          await expect(shortcuts).toBeVisible();
          await expect(
            shortcuts.getByRole("heading", { name: "Keyboard Shortcuts" }),
          ).toBeVisible();
          const close = shortcuts.getByRole("button", { name: "Close" });
          await expect(close).toBeVisible();
          const body = shortcuts.locator(":scope > .confirm-dialog-body");
          const scrollGeometry = await body.evaluate((element) => ({
            overflowY: window.getComputedStyle(element).overflowY,
            clientHeight: element.clientHeight,
            scrollHeight: element.scrollHeight,
          }));
          expect(scrollGeometry.overflowY).toBe("auto");
          expect(scrollGeometry.scrollHeight).toBeGreaterThan(
            scrollGeometry.clientHeight,
          );
          const editing = shortcuts.getByRole("region", { name: "Editing" });
          await editing.scrollIntoViewIfNeeded();
          await expect(editing).toBeVisible();
          await close.click();
          await expect(shortcuts).toBeHidden();
          await expect(invoker).toBeFocused();
        }
      } finally {
        if (sessionId !== undefined) await deleteSession(ctx, sessionId);
        await ctx.dispose();
      }
    });
  }

  test("multiple notices stay reachable without consuming the 1280x720 workspace", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 720 });
    await page.route("**/api/system/status", async (route) => {
      await route.fulfill({
        json: {
          composer_available: false,
          composer_model: "deterministic-e2e",
          composer_provider: "playwright-route",
          composer_reason:
            "The Composer provider is unavailable while its operator checks the configured credentials and deployment policy. "
            .repeat(70),
          composer_missing_keys: [],
          composer_timeout_seconds: 180,
        },
      });
    });
    await page.route("**/api/composer-preferences", async (route) => {
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({
          detail:
            "The preferences service could not load this account's saved Composer settings. "
            .repeat(40),
        }),
      });
    });

    const token = tokenFromStorageState(await page.context().storageState());
    const ctx = await authedContext(token);
    let sessionId: string | undefined;
    try {
      const session = await createSession(ctx, "workspace notice geometry");
      sessionId = session.id;
      await seedPopulatedComposition(ctx, sessionId);
      await page.goto(`/#/${sessionId}`);

      const workspace = page.getByTestId("composer-workspace");
      const primary = page.getByTestId("app-notice-primary");
      await expect(primary).toHaveCount(1);
      await expect(primary).toContainText("Preferences:");
      const more = page.getByRole("button", { name: "1 more notice" });
      await expect(more).toBeVisible();
      const workspaceBefore = await box(workspace);

      const header = page.getByRole("banner");
      const main = page.locator("#composer-main");
      const version = page.getByRole("button", {
        name: /Composition history \(currently v\d+\)/,
      });
      const primaryBounds = await primary.boundingBox();
      const headerBounds = await header.boundingBox();
      const mainBounds = await main.boundingBox();
      const versionBounds = await version.boundingBox();
      expect(primaryBounds).not.toBeNull();
      expect(headerBounds).not.toBeNull();
      expect(mainBounds).not.toBeNull();
      expect(versionBounds).not.toBeNull();
      expect(headerBounds!.y).toBeGreaterThanOrEqual(
        primaryBounds!.y + primaryBounds!.height,
      );
      expect(mainBounds!.y).toBeGreaterThanOrEqual(
        headerBounds!.y + headerBounds!.height,
      );
      const headerControls = [
        page.getByRole("button", { name: /Session switcher:/ }),
        version,
        page.getByRole("button", { name: "account menu" }),
      ];
      for (const control of headerControls) {
        const bounds = await control.boundingBox();
        expect(bounds).not.toBeNull();
        expect(bounds!.height).toBeGreaterThanOrEqual(36);
        expect(bounds!.y).toBeGreaterThanOrEqual(headerBounds!.y);
        expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(
          headerBounds!.y + headerBounds!.height,
        );
      }
      const versionHitTarget = await version.evaluate((element) => {
        const bounds = element.getBoundingClientRect();
        const hit = document.elementFromPoint(
          bounds.left + bounds.width / 2,
          bounds.top + bounds.height / 2,
        );
        return hit === element || (hit !== null && element.contains(hit));
      });
      expect(versionHitTarget).toBe(true);

      await more.click();
      const popover = page.getByRole("region", { name: "All notices" });
      await expect(popover).toBeVisible();
      await expect(popover).toContainText("Couldn't load your preferences");
      await expect(popover).toContainText("Composer provider is unavailable");
      const popoverBounds = await popover.boundingBox();
      expect(popoverBounds).not.toBeNull();
      expect(popoverBounds!.y).toBeGreaterThanOrEqual(16);
      expect(popoverBounds!.y + popoverBounds!.height).toBeLessThanOrEqual(704);

      const noticeList = popover.locator(".app-notice-list");
      const scrollGeometry = await noticeList.evaluate((element) => ({
        overflowY: window.getComputedStyle(element).overflowY,
        clientHeight: element.clientHeight,
        scrollHeight: element.scrollHeight,
      }));
      expect(scrollGeometry.overflowY).toBe("auto");
      expect(scrollGeometry.scrollHeight).toBeGreaterThan(
        scrollGeometry.clientHeight,
      );

      const openSecrets = popover.getByRole("button", {
        name: "Open secrets settings",
      });
      await expect(openSecrets).toBeVisible();
      await openSecrets.click();
      await expect(
        page.getByRole("dialog", { name: "Secrets settings" }),
      ).toBeVisible();

      const workspaceAfter = await box(workspace);
      expect(Math.abs(workspaceAfter.height - workspaceBefore.height)).toBeLessThanOrEqual(1);
    } finally {
      if (sessionId !== undefined) await deleteSession(ctx, sessionId);
      await ctx.dispose();
    }
  });

  test("a resolving notice action focuses the surviving primary notice", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 720 });
    let backendRecovered = false;
    await page.route("**/api/system/status", async (route) => {
      if (!backendRecovered) {
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          body: JSON.stringify({ detail: "Temporarily unavailable" }),
        });
        return;
      }
      await route.fulfill({
        json: {
          composer_available: true,
          composer_model: "deterministic-e2e",
          composer_provider: "playwright-route",
          composer_reason: null,
          composer_missing_keys: [],
          composer_timeout_seconds: 180,
        },
      });
    });
    await page.route("**/api/composer-preferences", async (route) => {
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Preferences remain unavailable" }),
      });
    });

    const token = tokenFromStorageState(await page.context().storageState());
    const ctx = await authedContext(token);
    let sessionId: string | undefined;
    try {
      const session = await createSession(ctx, "workspace notice focus");
      sessionId = session.id;
      await seedPopulatedComposition(ctx, sessionId);
      await page.goto(`/#/${sessionId}`);

      const more = page.getByRole("button", { name: "1 more notice" });
      await expect(more).toBeVisible();
      await more.click();
      const popover = page.getByRole("region", { name: "All notices" });
      backendRecovered = true;
      await popover.getByRole("button", { name: "Retry connection" }).click();

      const primary = page.getByTestId("app-notice-primary");
      await expect(primary).toContainText("Preferences:");
      await expect(more).toHaveCount(0);
      await expect(popover).toHaveCount(0);
      await expect(primary).toBeFocused();
    } finally {
      if (sessionId !== undefined) await deleteSession(ctx, sessionId);
      await ctx.dispose();
    }
  });

  test("skip link preserves a collapsed narrow Pipeline workspace and active session", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 900, height: 720 });
    await page.addInitScript(() => {
      window.localStorage.setItem(
        "elspeth_composer_workspace_layout_v1",
        JSON.stringify({
          version: 1,
          preferredAuthoringWidth: 420,
          authoringCollapsed: true,
        }),
      );
    });
    const token = tokenFromStorageState(await page.context().storageState());
    const ctx = await authedContext(token);
    let sessionId: string | undefined;
    try {
      const session = await createSession(ctx, "workspace skip target");
      sessionId = session.id;
      await seedPopulatedComposition(ctx, sessionId);
      await page.goto(`/#/${sessionId}`);

      const workspace = page.getByTestId("composer-workspace");
      await expect(workspace).toHaveAttribute("data-layout-mode", "narrow");
      await expect(workspace).toHaveAttribute(
        "data-authoring-collapsed",
        "true",
      );
      const pipelineView = page.getByRole("tab", { name: "Pipeline" });
      await pipelineView.click();
      await expect(pipelineView).toHaveAttribute("aria-selected", "true");
      await expect(
        page.getByRole("region", { name: "Authoring pane" }),
      ).toBeHidden();

      const sessionSwitcher = page.getByRole("button", {
        name: "Session switcher: workspace skip target",
      });
      await expect(sessionSwitcher).toBeVisible();
      const skipLink = page.getByRole("link", { name: "Skip to main content" });
      await skipLink.focus();
      await skipLink.press("Enter");

      const main = page.locator("#composer-main");
      await expect(main).toBeVisible();
      await expect(main).not.toHaveAttribute("hidden", "");
      await expect(main).not.toHaveAttribute("inert", "");
      await expect(main).toBeFocused();
      await expect(page).toHaveURL(new RegExp(`#/${sessionId}$`));
      await expect(sessionSwitcher).toBeVisible();
      await expect(pipelineView).toHaveAttribute("aria-selected", "true");
      await expect(workspace).toHaveAttribute(
        "data-authoring-collapsed",
        "true",
      );
    } finally {
      if (sessionId !== undefined) await deleteSession(ctx, sessionId);
      await ctx.dispose();
    }
  });
});
