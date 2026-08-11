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
  setRunHistoryRequestPhase,
  WORKSPACE_SCENARIOS,
  workspaceScenarioTelemetry,
  type WorkspaceScenario,
} from "./helpers/workspace-fixtures";
import { ComposerPage } from "./page-objects/composer-page";

interface RunPollingProbe {
  activeIntervalIds: Set<number>;
}

async function installRunPollingClockProbe(page: ComposerPage["page"]): Promise<void> {
  await page.clock.install();
  const currentBrowserTime = await page.evaluate(() => Date.now());
  await page.clock.pauseAt(currentBrowserTime + 100);
  await page.addInitScript(() => {
    const originalSetInterval = window.setInterval.bind(window);
    const originalClearInterval = window.clearInterval.bind(window);
    const activeIntervalIds = new Set<number>();
    const probedWindow = window as typeof window & {
      __workspaceRunPollingProbe?: RunPollingProbe;
    };
    probedWindow.__workspaceRunPollingProbe = { activeIntervalIds };
    window.setInterval = ((
      handler: TimerHandler,
      timeout?: number,
      ...arguments_: unknown[]
    ) => {
      const intervalId = originalSetInterval(handler, timeout, ...arguments_);
      if (timeout === 3_000) activeIntervalIds.add(intervalId);
      return intervalId;
    }) as typeof window.setInterval;
    window.clearInterval = ((intervalId?: number) => {
      if (intervalId !== undefined) activeIntervalIds.delete(intervalId);
      originalClearInterval(intervalId);
    }) as typeof window.clearInterval;
  });
}

async function activeRunPollingIntervals(page: ComposerPage["page"]): Promise<number> {
  return await page.evaluate(() => {
    const probedWindow = window as typeof window & {
      __workspaceRunPollingProbe?: RunPollingProbe;
    };
    const probe = probedWindow.__workspaceRunPollingProbe;
    if (probe === undefined) throw new Error("run polling probe is not installed");
    return probe.activeIntervalIds.size;
  });
}

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
      expect(
        workspaceScenarioTelemetry(page).tallDialogLivePreflightChecked,
      ).toBe(true);
      await expect(composer.validationStatus()).toHaveAccessibleName(
        "Validation: Passed",
      );
      await expect(composer.auditStatus()).toHaveAccessibleName("Audit: Ready");
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

  test("Keyboard Shortcuts stays scrollable and restores its exact invoker", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 720 });
    const sessionId = await installWorkspaceScenario(page, "empty-freeform");
    const composer = new ComposerPage(page);
    try {
      await composer.goto(sessionId);
      await composer.waitForChatReady();
      const invoker = page.getByRole("button", {
        name: "Session switcher: workspace empty-freeform",
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

      const dialog = page.getByRole("dialog", { name: "Keyboard Shortcuts" });
      await expect(dialog).toBeVisible();
      await expect(
        dialog.getByRole("heading", { name: "Keyboard Shortcuts" }),
      ).toBeVisible();
      const close = dialog.getByRole("button", { name: "Close" });
      await expect(close).toBeVisible();
      const body = dialog.locator(":scope > .confirm-dialog-body");
      const scrollGeometry = await body.evaluate((element) => ({
        overflowY: window.getComputedStyle(element).overflowY,
        clientHeight: element.clientHeight,
        scrollHeight: element.scrollHeight,
      }));
      expect(scrollGeometry.overflowY).toBe("auto");
      expect(scrollGeometry.scrollHeight).toBeGreaterThan(
        scrollGeometry.clientHeight,
      );
      const editing = dialog.getByRole("region", { name: "Editing" });
      await editing.scrollIntoViewIfNeeded();
      await expect(editing).toBeVisible();
      await close.click();
      await expect(dialog).toBeHidden();
      await expect(invoker).toBeFocused();
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
      const header = page.getByRole("banner");
      const main = page.locator("#composer-main");
      const version = page.getByRole("button", {
        name: /Composition history \(currently v\d+\)/,
      });
      const primaryBounds = await primary.boundingBox();
      const headerBounds = await header.boundingBox();
      const mainBounds = await main.boundingBox();
      expect(primaryBounds).not.toBeNull();
      expect(headerBounds).not.toBeNull();
      expect(mainBounds).not.toBeNull();
      expect(headerBounds!.y).toBeGreaterThanOrEqual(
        primaryBounds!.y + primaryBounds!.height,
      );
      expect(mainBounds!.y).toBeGreaterThanOrEqual(
        headerBounds!.y + headerBounds!.height,
      );
      for (const control of [
        page.getByRole("button", { name: /Session switcher:/ }),
        version,
        page.getByRole("button", { name: "account menu" }),
      ]) {
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
  }, testInfo) => {
    await page.setViewportSize({ width: 1280, height: 720 });
    const sessionId = await installWorkspaceScenario(page, "active-completed-run");
    const composer = new ComposerPage(page);
    try {
      await installRunPollingClockProbe(page);
      await composer.goto(sessionId);
      await composer.waitForChatReady();
      const expectedRunsUrl = new URL(
        `/api/sessions/${sessionId}/runs`,
        page.url(),
      ).href;
      const mountRequests = workspaceScenarioTelemetry(page).runHistoryRequestLog;
      expect(mountRequests).toHaveLength(2);
      expect(mountRequests).toEqual(
        mountRequests.map((request) => ({
          url: expectedRunsUrl,
          timestamp: request.timestamp,
          phase: "mount",
        })),
      );
      await expect.poll(() => activeRunPollingIntervals(page)).toBe(0);

      setRunHistoryRequestPhase(page, "activate-run");
      await composer.artifactTab("Run").click();
      await expect(page.getByText("Pipeline running.", { exact: true })).toBeVisible();
      await expect.poll(() => activeRunPollingIntervals(page)).toBe(1);
      await expect
        .poll(() => workspaceScenarioTelemetry(page).runHistoryRequestLog)
        .toHaveLength(4);
      const activationRequests = workspaceScenarioTelemetry(page)
        .runHistoryRequestLog.slice(2);
      expect(activationRequests).toEqual(
        activationRequests.map((request) => ({
          url: expectedRunsUrl,
          timestamp: request.timestamp,
          phase: "activate-run",
        })),
      );

      setRunHistoryRequestPhase(page, "poll-tick");
      const activeRequestCount = workspaceScenarioTelemetry(page).runHistoryRequests;
      await page.clock.fastForward(3_000);
      await expect
        .poll(() => workspaceScenarioTelemetry(page).runHistoryRequests, {
          timeout: 5_000,
        })
        .toBe(activeRequestCount + 1);
      expect(workspaceScenarioTelemetry(page).runHistoryRequestLog.at(-1)).toMatchObject({
        url: expectedRunsUrl,
        phase: "poll-tick",
      });

      setRunHistoryRequestPhase(page, "before-next-tick");
      await page.clock.fastForward(2_999);
      expect(workspaceScenarioTelemetry(page).runHistoryRequests).toBe(
        activeRequestCount + 1,
      );

      setRunHistoryRequestPhase(page, "inactive");
      await composer.artifactTab("Graph").click();
      await expect.poll(() => activeRunPollingIntervals(page)).toBe(0);
      await page.clock.fastForward(6_001);
      expect(workspaceScenarioTelemetry(page).runHistoryRequests).toBe(
        activeRequestCount + 1,
      );
      await testInfo.attach("run-history-request-provenance", {
        body: JSON.stringify(
          workspaceScenarioTelemetry(page).runHistoryRequestLog,
          null,
          2,
        ),
        contentType: "application/json",
      });
    } finally {
      await deleteWorkspaceScenario(page, sessionId);
    }
  });

  test("long Run content keeps the artifact panel as the only scroll owner", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 720 });
    const sessionId = await installWorkspaceScenario(page, "active-completed-run");
    const composer = new ComposerPage(page);
    try {
      await composer.goto(sessionId);
      await composer.waitForChatReady();
      await composer.artifactTab("Run").click();
      await expect(composer.artifactTab("Run")).toHaveAttribute("aria-selected", "true");
      const results = page.getByRole("region", { name: "Pipeline run results" });
      await expect(results).toBeVisible();
      await results.evaluate((element) => {
        const tall = document.createElement("div");
        tall.style.height = "1600px";
        tall.dataset.testid = "long-run-probe";
        element.append(tall);
      });
      const styles = await results.evaluate((element) => {
        const computed = getComputedStyle(element);
        return {
          maxHeight: computed.maxHeight,
          overflowY: computed.overflowY,
          marginTop: computed.marginTop,
          marginRight: computed.marginRight,
          marginBottom: computed.marginBottom,
          marginLeft: computed.marginLeft,
        };
      });
      expect(styles).toEqual({
        maxHeight: "none",
        overflowY: "visible",
        marginTop: "0px",
        marginRight: "0px",
        marginBottom: "0px",
        marginLeft: "0px",
      });
      await expect.poll(() => composer.activeArtifactPanel().evaluate(
        (element) => element.scrollHeight > element.clientHeight,
      )).toBe(true);
      const scrollOwnership = await page.evaluate(() => {
        const results = document.querySelector<HTMLElement>(
          '[aria-label="Pipeline run results"]',
        );
        const panel = document.querySelector<HTMLElement>(
          ".artifact-workspace-panel:not([hidden])",
        );
        if (results === null || panel === null) {
          throw new Error("Run scroll ownership surfaces are missing");
        }
        results.scrollTop = 300;
        panel.scrollTop = 300;
        const root = document.scrollingElement;
        if (root === null) throw new Error("document scrolling element is missing");
        return {
          resultsScrollTop: results.scrollTop,
          panelScrollTop: panel.scrollTop,
          documentScrollTop: root.scrollTop,
          documentScrollHeight: root.scrollHeight,
          documentClientHeight: root.clientHeight,
          documentOverflowY: getComputedStyle(root).overflowY,
        };
      });
      expect(scrollOwnership.resultsScrollTop).toBe(0);
      expect(scrollOwnership.panelScrollTop).toBeGreaterThan(0);
      expect(scrollOwnership.documentScrollTop).toBe(0);
      expect(scrollOwnership.documentScrollHeight).toBeLessThanOrEqual(
        scrollOwnership.documentClientHeight,
      );
      expect(scrollOwnership.documentOverflowY).toBe("hidden");
      await expect(composer.artifactTab("Run")).toHaveAttribute("aria-selected", "true");
    } finally {
      await deleteWorkspaceScenario(page, sessionId);
    }
  });

  test("collapsed restore status reserves space above the artifact toolbar", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 720 });
    const sessionId = await installWorkspaceScenario(page, "pending-acknowledgement");
    const composer = new ComposerPage(page);
    try {
      await composer.goto(sessionId);
      await composer.waitForChatReady();
      await composer.collapseAuthoring().click();
      const restore = composer.restoreAuthoring();
      const toolbar = page.locator(".artifact-workspace-toolbar");
      await expect(restore).toBeVisible();
      await expect(restore).toBeFocused();
      const [restoreBox, toolbarBox] = await Promise.all([
        restore.boundingBox(),
        toolbar.boundingBox(),
      ]);
      expect(restoreBox).not.toBeNull();
      expect(toolbarBox).not.toBeNull();
      expect(restoreBox!.y + restoreBox!.height).toBeLessThanOrEqual(toolbarBox!.y);
      const hit = await page.evaluate(({ x, y }) => {
        const element = document.elementFromPoint(x, y);
        return element?.getAttribute("aria-label") ?? element?.textContent ?? null;
      }, {
        x: restoreBox!.x + restoreBox!.width / 2,
        y: restoreBox!.y + restoreBox!.height / 2,
      });
      expect(hit).toContain("Restore authoring pane");
    } finally {
      await deleteWorkspaceScenario(page, sessionId);
    }
  });

  test("Ctrl+/ restores collapsed Compose before focusing the chat input", async ({ page }) => {
    await page.setViewportSize({ width: 720, height: 720 });
    const sessionId = await installWorkspaceScenario(page, "empty-freeform");
    const composer = new ComposerPage(page);
    try {
      await composer.goto(sessionId);
      await composer.waitForChatReady();
      await composer.collapseAuthoring().click();
      await composer.pipelineViewTab().click();
      await page.keyboard.press("Control+/");
      await expect(composer.workspace()).not.toHaveAttribute(
        "data-authoring-collapsed",
      );
      await expect(composer.composeViewTab()).toHaveAttribute("aria-selected", "true");
      await expect(composer.chatInput()).toBeFocused();
    } finally {
      await deleteWorkspaceScenario(page, sessionId);
    }
  });

  test("command palette closes after Show Graph and leaves Graph selected and focused", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 720 });
    const sessionId = await installWorkspaceScenario(page, "populated-long-transcript");
    const composer = new ComposerPage(page);
    try {
      await composer.goto(sessionId);
      await composer.waitForChatReady();
      await composer.artifactTab("Spec").click();
      // Playwright treats an uppercase chord token as Shift+K; the product
      // shortcut is the unshifted Ctrl+K key event (KeyboardEvent.key === "k").
      await page.keyboard.press("Control+k");
      const palette = page.getByRole("dialog", { name: "Command palette" });
      await expect(palette).toBeVisible();
      await palette.getByRole("combobox", { name: "Search commands" }).fill(
        "Show Graph",
      );
      await palette.getByRole("option", { name: /Show Graph/ }).click();

      await expect(palette).toBeHidden();
      await expect(composer.artifactTab("Graph")).toHaveAttribute(
        "aria-selected",
        "true",
      );
      await expect(composer.artifactTab("Graph")).toBeFocused();
    } finally {
      await deleteWorkspaceScenario(page, sessionId);
    }
  });
});
