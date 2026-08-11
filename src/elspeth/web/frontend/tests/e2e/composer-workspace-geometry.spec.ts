import { expect, test } from "@playwright/test";

import {
  boxWidth,
  expectDesktopWorkspaceGeometry,
  expectDialogGeometry,
  expectIntendedPaneScrollers,
  expectNoDocumentHorizontalOverflow,
  expectPrimaryControlsInViewport,
  expectResizeGeometry,
} from "./helpers/workspace-assertions";
import {
  deleteWorkspaceScenario,
  DESKTOP_VIEWPORTS,
  installWorkspaceScenario,
  recoverWorkspaceNoticeBackend,
  releasePendingAcknowledgement,
  resetWorkspaceScenarioTelemetry,
  WORKSPACE_SCENARIOS,
  workspaceScenarioTelemetry,
  type WorkspaceScenario,
} from "./helpers/workspace-fixtures";
import { ComposerPage } from "./page-objects/composer-page";

async function assertScenario(
  scenario: WorkspaceScenario,
  composer: ComposerPage,
): Promise<void> {
  const { page } = composer;
  switch (scenario) {
    case "empty-freeform":
      await expect(
        composer.activeArtifactPanel().getByText(
          "No pipeline to visualise. Start a conversation to build one.",
        ),
      ).toBeVisible();
      await expect(composer.chatInput()).toBeVisible();
      break;
    case "populated-long-transcript":
      await expect(page.getByRole("log", { name: "Conversation" })).toBeVisible();
      await expect(page.getByText(/Composer turn 56:/)).toBeAttached();
      await expectIntendedPaneScrollers(page, { transcriptMustScroll: true });
      await expectResizeGeometry(page, composer, page.viewportSize()!.width);
      break;
    case "active-guided-decision":
      await expect(
        composer.authoringPane().getByRole("log", { name: "Step chat history" }),
      ).toBeVisible();
      await expect(
        composer.authoringPane().getByRole("log", { name: "Guided wizard step" }),
      ).toBeVisible();
      await expect(page.getByTestId("completion-bar")).toHaveCount(0);
      await expect(composer.moreActions()).toHaveCount(0);
      break;
    case "validation-audit-issues": {
      await expect(composer.validationStatus()).toHaveAccessibleName(
        "Validation: 24 errors",
      );
      const beforeAuthoring = await boxWidth(composer.authoringPane());
      const beforeArtifact = await boxWidth(composer.artifactRegion());
      await composer.validationStatus().click();
      await expect(composer.inspector()).toBeVisible();
      expect(await boxWidth(composer.authoringPane())).toBeCloseTo(beforeAuthoring, 0);
      expect(await boxWidth(composer.artifactRegion())).toBeCloseTo(beforeArtifact, 0);
      await expectIntendedPaneScrollers(page, { inspectorMustScroll: true });
      await composer.inspector().getByRole("button", { name: "Close" }).click();
      break;
    }
    case "pending-acknowledgement":
      await composer.collapseAuthoring().click();
      releasePendingAcknowledgement(page);
      await expect(page.locator("#workspace-collapsed-status")).toContainText(
        "1 decision to acknowledge",
      );
      await composer.restoreAuthoring().click();
      await expect(page.getByTestId("acknowledgement-card")).toBeVisible();
      break;
    case "active-completed-run":
      await composer.artifactTab("Run").click();
      await expect(page.getByRole("button", { name: "Runs (1)" })).toBeVisible();
      await expect(page.getByText("Pipeline running.", { exact: true })).toBeVisible();
      await page.getByRole("button", { name: "Runs (1)" }).click();
      await expect(page.getByText("completed", { exact: true })).toBeVisible();
      await page.getByRole("button", { name: "Close runs" }).click();
      await composer.artifactTab("Graph").click();
      break;
    case "multiple-notices":
      await expect(page.getByTestId("app-notice-primary")).toHaveCount(1);
      await expect(page.getByRole("button", { name: "1 more notice" })).toBeVisible();
      await page.getByRole("button", { name: "1 more notice" }).click();
      await expect(page.getByRole("region", { name: "All notices" })).toBeVisible();
      await expect
        .poll(() =>
          page.locator(".app-notice-list").evaluate((element) =>
            element.scrollHeight - element.clientHeight,
          ),
        )
        .toBeGreaterThan(0);
      await page.keyboard.press("Escape");
      break;
    case "tall-confirmation-dialog": {
      const invoker = composer.runPipeline();
      await expect(invoker).toBeEnabled();
      await invoker.focus();
      await invoker.click();
      const dialog = page.getByRole("alertdialog", { name: "Run pipeline?" });
      await expect(dialog).toBeVisible();
      await expectDialogGeometry(page, dialog);
      await dialog.getByRole("button", { name: "Cancel" }).click();
      await expect(invoker).toBeFocused();
      break;
    }
  }
}

test.describe("Composer deterministic workspace geometry", () => {
  for (const viewport of DESKTOP_VIEWPORTS) {
    for (const scenario of WORKSPACE_SCENARIOS) {
      test(`${scenario} is operable at ${viewport.width}x${viewport.height}`, async ({
        page,
      }) => {
        await page.setViewportSize(viewport);
        const sessionId = await installWorkspaceScenario(page, scenario);
        const composer = new ComposerPage(page);
        try {
          await composer.goto(sessionId);
          await composer.waitForChatReady();
          await expectDesktopWorkspaceGeometry(page, composer);
          await expectNoDocumentHorizontalOverflow(page);
          await assertScenario(scenario, composer);
          await expectPrimaryControlsInViewport(page, composer, {
            completion: scenario !== "active-guided-decision",
            moreActions: scenario !== "active-guided-decision",
          });
          await expectIntendedPaneScrollers(page, {
            transcriptMustScroll: scenario === "populated-long-transcript",
          });
        } finally {
          await deleteWorkspaceScenario(page, sessionId);
        }
      });
    }
  }

  test("sub-1000 compact fallback preserves 360px authoring plus the remainder", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 980, height: 720 });
    const sessionId = await installWorkspaceScenario(page, "populated-long-transcript");
    const composer = new ComposerPage(page);
    try {
      await composer.goto(sessionId);
      await composer.waitForChatReady();
      await expect(composer.workspace()).toHaveAttribute("data-layout-mode", "compact");
      await expect.poll(() => boxWidth(composer.authoringPane())).toBe(360);
      await expect.poll(() => boxWidth(composer.artifactRegion())).toBe(620);
      await expectNoDocumentHorizontalOverflow(page);
    } finally {
      await deleteWorkspaceScenario(page, sessionId);
    }
  });

  test("long notices use a bounded internal scroll surface without consuming the workspace", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 720 });
    const sessionId = await installWorkspaceScenario(page, "multiple-notices");
    const composer = new ComposerPage(page);
    try {
      await composer.goto(sessionId);
      await composer.waitForChatReady();
      const workspaceBefore = await composer.workspace().boundingBox();
      expect(workspaceBefore).not.toBeNull();

      const primary = page.getByTestId("app-notice-primary");
      await expect(primary).toContainText("Preferences:");
      await page.getByRole("button", { name: "1 more notice" }).click();
      const popover = page.getByRole("region", { name: "All notices" });
      await expect(popover).toContainText("Couldn't load your preferences");
      await expect(popover).toContainText("Composer provider is unavailable");
      const popoverBounds = await popover.boundingBox();
      expect(popoverBounds).not.toBeNull();
      expect(popoverBounds!.y).toBeGreaterThanOrEqual(16);
      expect(popoverBounds!.y + popoverBounds!.height).toBeLessThanOrEqual(704);

      const noticeScroll = await popover.locator(".app-notice-list").evaluate((element) => ({
        overflowY: getComputedStyle(element).overflowY,
        clientHeight: element.clientHeight,
        scrollHeight: element.scrollHeight,
      }));
      expect(noticeScroll.overflowY).toBe("auto");
      expect(noticeScroll.scrollHeight).toBeGreaterThan(noticeScroll.clientHeight);
      const openSecrets = popover.getByRole("button", {
        name: "Open secrets settings",
      });
      await expect(openSecrets).toBeVisible();
      await openSecrets.click();
      await expect(page.getByRole("dialog", { name: "Secrets settings" })).toBeVisible();

      const workspaceAfter = await composer.workspace().boundingBox();
      expect(workspaceAfter).not.toBeNull();
      expect(Math.abs(workspaceAfter!.height - workspaceBefore!.height)).toBeLessThanOrEqual(1);
    } finally {
      await deleteWorkspaceScenario(page, sessionId);
    }
  });

  test("resolving a notice restores focus to the surviving primary notice", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 720 });
    const sessionId = await installWorkspaceScenario(page, "multiple-notices", {
      noticeMode: "recoverable-backend",
    });
    const composer = new ComposerPage(page);
    try {
      await composer.goto(sessionId);
      await composer.waitForChatReady();
      const more = page.getByRole("button", { name: "1 more notice" });
      await expect(more).toBeVisible();
      await more.click();
      const popover = page.getByRole("region", { name: "All notices" });
      recoverWorkspaceNoticeBackend(page);
      await popover.getByRole("button", { name: "Retry connection" }).click();

      const primary = page.getByTestId("app-notice-primary");
      await expect(primary).toContainText("Preferences:");
      await expect(more).toHaveCount(0);
      await expect(popover).toHaveCount(0);
      await expect(primary).toBeFocused();
    } finally {
      await deleteWorkspaceScenario(page, sessionId);
    }
  });

  test("run-history polling follows the active Run artifact lifecycle once", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 720 });
    const sessionId = await installWorkspaceScenario(page, "active-completed-run");
    const composer = new ComposerPage(page);
    try {
      await composer.goto(sessionId);
      await composer.waitForChatReady();
      await composer.artifactTab("Run").click();
      await expect(page.getByText("Pipeline running.", { exact: true })).toBeVisible();
      resetWorkspaceScenarioTelemetry(page);
      await expect
        .poll(() => workspaceScenarioTelemetry(page).runHistoryRequests, {
          timeout: 5_000,
        })
        .toBeGreaterThanOrEqual(1);

      await composer.artifactTab("Graph").click();
      resetWorkspaceScenarioTelemetry(page);
      await page.waitForTimeout(3_250);
      expect(workspaceScenarioTelemetry(page).runHistoryRequests).toBe(0);
    } finally {
      await deleteWorkspaceScenario(page, sessionId);
    }
  });
});
