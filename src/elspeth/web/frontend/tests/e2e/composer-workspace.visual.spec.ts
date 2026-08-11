import { expect, test, type Locator, type Page } from "@playwright/test";

import { expectDialogGeometry } from "./helpers/workspace-assertions";
import {
  deleteWorkspaceScenario,
  installWorkspaceScenario,
  type WorkspaceScenario,
} from "./helpers/workspace-fixtures";
import { ComposerPage } from "./page-objects/composer-page";
import { setupWorkspaceScenario } from "./helpers/workspace-setup";

const FIXED_BROWSER_TIME = new Date("2026-08-11T08:00:30.000Z");

function expectedTerminalStatuses(scenario: WorkspaceScenario): {
  validation: string;
  audit: string;
} {
  if (scenario === "validation-audit-issues") {
    return {
      validation: "Validation: 24 errors",
      audit: "Audit: 2 issues",
    };
  }
  return {
    validation: "Validation: Passed",
    audit: "Audit: Ready",
  };
}

async function settleGraphViewport(page: Page): Promise<void> {
  const activePanel = page.locator(
    '.artifact-workspace-panel:not([hidden])',
  );
  await activePanel.locator(".react-flow__controls-fitview").click();
  await activePanel.locator(".react-flow__viewport").evaluate(
    (viewport) =>
      new Promise<void>((resolve) => {
        let previousTransform = "";
        let stableFrames = 0;
        const sample = (): void => {
          const transform = viewport.getAttribute("style") ?? "";
          stableFrames = transform === previousTransform ? stableFrames + 1 : 0;
          previousTransform = transform;
          if (stableFrames >= 2) {
            resolve();
            return;
          }
          requestAnimationFrame(sample);
        };
        requestAnimationFrame(sample);
      }),
  );
}

async function settleAuthoringScroll(
  owner: Locator,
  position: "top" | "bottom",
): Promise<void> {
  await owner.evaluate((element, requestedPosition) => {
    element.style.scrollBehavior = "auto";
    element.scrollTop =
      requestedPosition === "top" ? 0 : element.scrollHeight;
  }, position);
  await expect
    .poll(async () =>
      owner.evaluate((element, requestedPosition) => {
        if (requestedPosition === "top") return element.scrollTop === 0;
        return (
          Math.abs(
            element.scrollHeight - element.clientHeight - element.scrollTop,
          ) <= 1
        );
      }, position),
    )
    .toBe(true);
}

async function openStableVisualScenario(
  page: Page,
  scenario: WorkspaceScenario,
  viewport: { width: number; height: number },
  expectedNodeCount: number,
): Promise<{ composer: ComposerPage; sessionId: string }> {
  await page.setViewportSize(viewport);
  const { sessionId, value: composer } = await setupWorkspaceScenario(
    () => installWorkspaceScenario(page, scenario),
    async (createdSessionId) => {
      const createdComposer = new ComposerPage(page);
      await createdComposer.goto(createdSessionId);
      await createdComposer.waitForChatReady();
      await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
      await page.evaluate(async () => {
        await document.fonts.ready;
      });
      const terminalStatuses = expectedTerminalStatuses(scenario);
      await expect(createdComposer.validationStatus()).toHaveAccessibleName(
        terminalStatuses.validation,
      );
      await expect(createdComposer.auditStatus()).toHaveAccessibleName(
        terminalStatuses.audit,
      );
      await expect(page.locator(".react-flow__node")).toHaveCount(
        expectedNodeCount,
      );
      await settleGraphViewport(page);
      return createdComposer;
    },
    (createdSessionId) => deleteWorkspaceScenario(page, createdSessionId),
  );
  return { composer, sessionId };
}

test.describe("Composer workspace visual baselines", () => {
  test.use({
    colorScheme: "dark",
    contextOptions: { reducedMotion: "reduce" },
  });

  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      window.localStorage.setItem("elspeth_theme", "dark");
    });
    await page.clock.setFixedTime(FIXED_BROWSER_TIME);
  });

  for (const viewport of [
    { width: 1920, height: 900 },
    { width: 1536, height: 760 },
    { width: 1280, height: 720 },
    { width: 2560, height: 1280 },
  ] as const) {
    test(`populated freeform at ${viewport.width}x${viewport.height}`, async ({
      page,
    }) => {
      const { composer, sessionId } = await openStableVisualScenario(
        page,
        "populated-long-transcript",
        viewport,
        2,
      );
      try {
        await expect(page.getByText(/Composer turn 56:/)).toBeAttached();
        await settleAuthoringScroll(
          composer.authoringPane().locator(".chat-panel-messages"),
          "bottom",
        );
        const fractionalEdgeLabel = composer
          .workspace()
          .locator(".react-flow__edge-text")
          .filter({ hasText: "success" });
        await expect(fractionalEdgeLabel).toHaveText("success");
        await expect(composer.workspace()).toHaveScreenshot(
          `populated-freeform-${viewport.width}x${viewport.height}.png`,
          {
            animations: "disabled",
            caret: "hide",
            // React Flow centers this one word on a fractional transform.
            // Mask only that glyph raster; every surrounding pixel remains
            // an exact geometry and appearance oracle.
            mask: [fractionalEdgeLabel],
            maskColor: "#0f2d35",
          },
        );
      } finally {
        await deleteWorkspaceScenario(page, sessionId);
      }
    });
  }

  test("guided decision at 1536x760", async ({ page }) => {
    const { composer, sessionId } = await openStableVisualScenario(
      page,
      "active-guided-decision",
      { width: 1536, height: 760 },
      2,
    );
    try {
      await expect(
        composer.authoringPane().getByRole("log", { name: "Guided wizard step" }),
      ).toBeVisible();
      await settleAuthoringScroll(
        composer.authoringPane().locator(".guided-authoring-scroll"),
        "top",
      );
      await expect(composer.workspace()).toHaveScreenshot(
        "guided-decision-1536x760.png",
        { animations: "disabled", caret: "hide" },
      );
    } finally {
      await deleteWorkspaceScenario(page, sessionId);
    }
  });

  test("inspector open at 1280x720", async ({ page }) => {
    const { composer, sessionId } = await openStableVisualScenario(
      page,
      "validation-audit-issues",
      { width: 1280, height: 720 },
      2,
    );
    try {
      await expect(composer.validationStatus()).toHaveAccessibleName(
        "Validation: 24 errors",
      );
      await composer.validationStatus().click();
      await expect(composer.inspector()).toBeVisible();
      await expect(composer.workspace()).toHaveScreenshot(
        "inspector-open-1280x720.png",
        { animations: "disabled", caret: "hide" },
      );
    } finally {
      await deleteWorkspaceScenario(page, sessionId);
    }
  });

  test("tall dialog full page at 1280x720", async ({ page }) => {
    const { composer, sessionId } = await openStableVisualScenario(
      page,
      "tall-confirmation-dialog",
      { width: 1280, height: 720 },
      82,
    );
    try {
      await expect(composer.runPipeline()).toBeEnabled();
      await composer.runPipeline().click();
      const dialog = page.getByRole("alertdialog", { name: "Run pipeline?" });
      await expect(dialog).toBeVisible();
      await expectDialogGeometry(page, dialog);
      await expect(page).toHaveScreenshot("tall-dialog-1280x720.png", {
        animations: "disabled",
        caret: "hide",
      });
    } finally {
      await deleteWorkspaceScenario(page, sessionId);
    }
  });
});
