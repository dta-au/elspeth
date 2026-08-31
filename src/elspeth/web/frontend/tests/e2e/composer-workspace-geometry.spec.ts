import { expect, test } from "@playwright/test";

import { authedContext, setShowAdvanced, tokenFromStorageState } from "./helpers/api";
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
      await expect(composer.catalogButton()).toHaveCount(0);
      break;
    case "validation-audit-issues": {
      await expect(composer.checksTab()).toHaveAccessibleName(
        "Checks: 25 issues",
      );
      const beforeAuthoring = await boxWidth(composer.authoringPane());
      const beforeArtifact = await boxWidth(composer.artifactRegion());
      await composer.checksTab().click();
      await expect(
        page.getByRole("region", { name: "Audit readiness" }),
      ).toBeVisible();
      // Selecting the Checks tab swaps panel content only — the pane grid
      // must not move (the retired drawer reserved a strip; the tab reserves
      // nothing).
      expect(await boxWidth(composer.authoringPane())).toBeCloseTo(beforeAuthoring, 0);
      expect(await boxWidth(composer.artifactRegion())).toBeCloseTo(beforeArtifact, 0);
      await expectIntendedPaneScrollers(page, {});
      await composer.artifactTab("Graph").click();
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
      await expect(composer.checksTab()).toHaveAccessibleName("Checks: Ready");
      const invoker = composer.runPipeline();
      await expect(invoker).toBeEnabled();
      await invoker.focus();
      await invoker.click();
      const dialog = page.getByRole("alertdialog", { name: "Run pipeline" });
      await expect(dialog).toBeVisible();
      await expectDialogGeometry(page, dialog);
      await dialog.getByRole("button", { name: "Cancel" }).click();
      await expect(invoker).toBeFocused();
      break;
    }
  }
}

/**
 * The workspace's bottom bar row (elspeth-9c94a58500): ONE grid item spanning
 * both columns that paints the bottom rule and fill once, hosting the collapse
 * control in the authoring column and the action bar in the artifact column.
 * It replaced a mirrored band the authoring pane painted as a ::before to meet
 * the bar across the seam — first token-sized, then observer-sized
 * (elspeth-215c989bed, elspeth-97db9c22e5). Returned as boxes so a test can
 * state the row's contract directly: full width, exactly the bar's height,
 * and no second band anywhere.
 */
async function workspaceBottomRow(page: ComposerPage["page"]): Promise<{
  workspace: { left: number; right: number; bottom: number };
  row: { top: number; bottom: number; left: number; right: number; borderTop: number };
  bar: { top: number; bottom: number; height: number };
  collapse: { top: number; bottom: number } | null;
  authoringBandContent: string;
} | null> {
  return page.evaluate(() => {
    const workspace = document.querySelector('[data-testid="composer-workspace"]');
    const row = document.querySelector('[data-workspace-part="action-bar"]');
    const bar = document.querySelector(".workspace-action-bar");
    const pane = document.querySelector(".workspace-authoring-pane");
    const collapse = document.querySelector(
      'button[aria-label="Collapse authoring pane"]',
    );
    if (workspace === null || row === null || bar === null || pane === null) {
      return null;
    }
    const workspaceBox = workspace.getBoundingClientRect();
    const rowBox = row.getBoundingClientRect();
    const barBox = bar.getBoundingClientRect();
    const collapseBox = collapse?.getBoundingClientRect() ?? null;
    return {
      workspace: {
        left: workspaceBox.left,
        right: workspaceBox.right,
        bottom: workspaceBox.bottom,
      },
      row: {
        top: rowBox.top,
        bottom: rowBox.bottom,
        left: rowBox.left,
        right: rowBox.right,
        borderTop: Number.parseFloat(getComputedStyle(row).borderTopWidth),
      },
      bar: { top: barBox.top, bottom: barBox.bottom, height: barBox.height },
      collapse:
        collapseBox === null || collapseBox.height === 0
          ? null
          : { top: collapseBox.top, bottom: collapseBox.bottom },
      authoringBandContent: getComputedStyle(pane, "::before").content,
    };
  });
}

/**
 * The bottom-row contract, stated once and asserted at every step of a
 * geometry change: the row spans the workspace's full width and sits on its
 * bottom edge; the action bar starts directly under the row's rule and ends on
 * the row's bottom (the row IS the bar's height — nothing above it, nothing
 * below it, so no rule that could have derived that height exists); the
 * collapse control is centred on the bar across the seam — which is where every
 * control in the bar sits, since the bar centres its groups and the completion
 * group centres its members (elspeth-b4e88f0f8c), so on a one-line bar the
 * control's top IS the chips' top; and the authoring pane paints no band of its
 * own. Centre-to-centre, not top-to-top: a reason-tall bar puts its controls
 * at (H - 44) / 2, and equal tops would fail there while it looked right.
 */
function expectBottomRowContract(
  edges: NonNullable<Awaited<ReturnType<typeof workspaceBottomRow>>>,
): void {
  expect(Math.abs(edges.row.left - edges.workspace.left)).toBeLessThanOrEqual(1);
  expect(Math.abs(edges.row.right - edges.workspace.right)).toBeLessThanOrEqual(1);
  expect(Math.abs(edges.row.bottom - edges.workspace.bottom)).toBeLessThanOrEqual(1);
  expect(edges.row.borderTop).toBeGreaterThanOrEqual(1);
  expect(
    Math.abs(edges.row.top + edges.row.borderTop - edges.bar.top),
  ).toBeLessThanOrEqual(1);
  expect(Math.abs(edges.row.bottom - edges.bar.bottom)).toBeLessThanOrEqual(1);
  expect(edges.collapse).not.toBeNull();
  const collapseCentre = (edges.collapse!.top + edges.collapse!.bottom) / 2;
  const barCentre = (edges.bar.top + edges.bar.bottom) / 2;
  expect(Math.abs(collapseCentre - barCentre)).toBeLessThanOrEqual(1);
  expect(edges.authoringBandContent).toBe("none");
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
            catalog: scenario !== "active-guided-decision",
            importYaml: false,
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
        name: "API keys & secrets",
      });
      await expect(openSecrets).toBeVisible();
      await openSecrets.click();
      await expect(page.getByRole("dialog", { name: "API keys & secrets" })).toBeVisible();

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

  test("collapsed restore control shares the action bar's line and stays clickable", async ({
    page,
  }) => {
    // Operator decision 2026-08-15: no reserved banner row, no visible
    // "Authoring pane collapsed" text — the » icon joins the action bar's
    // own line at the bottom-left. The elementFromPoint probe is the
    // load-bearing assertion: the affordance and the opaque action bar now
    // occupy the same pixels on a shared z rung, so a stacking regression
    // paints the bar OVER the button and this probe (not the bounding box)
    // is what catches it.
    await page.setViewportSize({ width: 1280, height: 720 });
    const sessionId = await installWorkspaceScenario(page, "pending-acknowledgement");
    const composer = new ComposerPage(page);
    try {
      await composer.goto(sessionId);
      await composer.waitForChatReady();
      await composer.collapseAuthoring().click();
      const restore = composer.restoreAuthoring();
      const actionBar = page.locator(".workspace-action-bar");
      await expect(restore).toBeVisible();
      await expect(restore).toBeFocused();
      const [restoreBox, barBox] = await Promise.all([
        restore.boundingBox(),
        actionBar.boundingBox(),
      ]);
      expect(restoreBox).not.toBeNull();
      expect(barBox).not.toBeNull();
      // Same line: the button's vertical extent overlaps the action bar's.
      expect(restoreBox!.y).toBeLessThan(barBox!.y + barBox!.height);
      expect(restoreBox!.y + restoreBox!.height).toBeGreaterThan(barBox!.y);
      // …but the bar's first control starts clear of the button (the bar
      // reserves the slot via padding-left), not underneath it. With the
      // status chips retired to the Checks tab, the completion group's
      // first verb is the bar's first member.
      const firstVerbBox = await composer.saveForReview().boundingBox();
      expect(firstVerbBox).not.toBeNull();
      expect(firstVerbBox!.x).toBeGreaterThanOrEqual(
        restoreBox!.x + restoreBox!.width,
      );
      // No visible banner text: the status node stays in the tree for AT
      // (sr-only), so assert on rendered geometry, not DOM presence.
      const statusBox = await page
        .locator("#workspace-collapsed-status")
        .boundingBox();
      expect(statusBox === null || statusBox.width <= 1).toBe(true);
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

  test("1280 completion actions share one compact row with intact hit targets", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 720 });
    const sessionId = await installWorkspaceScenario(page, "populated-long-transcript");
    const composer = new ComposerPage(page);
    const ctx = await authedContext(tokenFromStorageState(await page.context().storageState()));
    try {
      await setShowAdvanced(ctx, true);
      await composer.goto(sessionId);
      await composer.waitForChatReady();
      // The bar's width now depends on Import YAML being mounted, and that
      // button appears only when fetchUserComposerPreferences() resolves
      // (preferencesStore). Without this barrier a boundingBox capture can
      // resolve against the pre-mount, two-verb bar while the button's own
      // auto-waiting locator resolves post-mount — every button assertion
      // passes and only the reason's wrapped height is wrong.
      await expect(composer.importYaml()).toBeVisible();
      await expect(composer.checksTab()).toHaveAccessibleName("Checks: Ready");
      const actionBar = composer.actionBar();
      const actions = [
        composer.saveForReview(),
        composer.runPipeline(),
        composer.importYaml(),
      ];
      const [actionBarBox, ...actionBoxes] = await Promise.all([
        actionBar.boundingBox(),
        ...actions.map((action) => action.boundingBox()),
      ]);
      expect(actionBarBox).not.toBeNull();
      expect(actionBarBox!.height).toBeLessThanOrEqual(64);
      for (const box of actionBoxes) {
        expect(box).not.toBeNull();
        expect(box!.height).toBeGreaterThanOrEqual(36);
        expect(Math.abs(box!.y - actionBoxes[0]!.y)).toBeLessThanOrEqual(1);
      }
      for (const action of actions) {
        await action.focus();
        await expect(action).toBeFocused();
        const hitLabel = await action.evaluate((element) => {
          const bounds = element.getBoundingClientRect();
          const hit = document.elementFromPoint(
            bounds.left + bounds.width / 2,
            bounds.top + bounds.height / 2,
          );
          return hit?.textContent?.trim() ?? null;
        });
        expect(hitLabel).toContain((await action.textContent())?.trim());
      }
    } finally {
      await setShowAdvanced(ctx, false);
      await deleteWorkspaceScenario(page, sessionId);
    }
  });

  /* elspeth-15e8cff7a5 and elspeth-97db9c22e5 lineage, recut for the
     Checks-tab move (the status chips left the bar for the artifact tab
     strip, so the bar's only group is the completion group).

     STATE still matters: the row above runs against a VALIDATED pipeline, so
     ExecuteButton renders no block-reason line and .completion-bar is a
     single control row — the defect family lives in the DEFAULT state of an
     unvalidated pipeline ("even blank ones"). This test exercises the gated
     state and pins the cross-element facts that remain: the reason line
     shares the verbs' line, and the application's bottom rule is ONE row
     spanning both columns with the collapse control registered against it
     (elspeth-9c94a58500 / elspeth-27dc483116). Still asserted as RENDERED
     geometry, not by snapshot — --update-snapshots rewrites only failing
     files, and a sub-threshold offset passes silently. */
  test("a gated Run keeps the reason line and the bottom rule registered with the completion buttons", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    const sessionId = await installWorkspaceScenario(page, "empty-freeform");
    const composer = new ComposerPage(page);
    const ctx = await authedContext(tokenFromStorageState(await page.context().storageState()));
    try {
      await setShowAdvanced(ctx, true);
      await composer.goto(sessionId);
      await composer.waitForChatReady();
      // The bar's width now depends on Import YAML being mounted, and that
      // button appears only when fetchUserComposerPreferences() resolves
      // (preferencesStore). Without this barrier a boundingBox capture can
      // resolve against the pre-mount, two-verb bar while the button's own
      // auto-waiting locator resolves post-mount — every button assertion
      // passes and only the reason's wrapped height is wrong.
      await expect(composer.importYaml()).toBeVisible();

      // Asserted, not assumed. Without the reason line in flow the completion
      // group is the same height as the status group and both invariants hold
      // trivially, so a green run would be evidence of nothing.
      const reason = page.locator(".side-rail-execute-reason");
      await expect(reason).toBeVisible();
      // Gated default state, asserted: an empty composition keeps the Checks
      // tab disabled (nothing to check yet), so Run's gate reason is the
      // bar's live signal here.
      await expect(composer.checksTab()).toBeDisabled();

      const [save, importYaml, run, reasonBox] = await Promise.all([
        composer.saveForReview().boundingBox(),
        composer.importYaml().boundingBox(),
        composer.runPipeline().boundingBox(),
        reason.boundingBox(),
      ]);
      for (const box of [save, importYaml, run, reasonBox]) {
        expect(box).not.toBeNull();
      }

      // One line: all three verbs share a top edge.
      expect(Math.abs(save!.y - importYaml!.y)).toBeLessThanOrEqual(1);
      expect(Math.abs(save!.y - run!.y)).toBeLessThanOrEqual(1);

      // The reason line sits to Run's LEFT, on the same line, vertically
      // centred against it (operator decision 2026-08-16). Centre-to-centre,
      // not top-to-top: the <p> is shorter than the 44px button, so equal tops
      // would be the wrong assertion and would pass while it looked wrong.
      const centre = (b: { y: number; height: number }) => b.y + b.height / 2;
      expect(Math.abs(centre(reasonBox!) - centre(run!))).toBeLessThanOrEqual(1);
      expect(reasonBox!.x + reasonBox!.width).toBeLessThanOrEqual(run!.x + 1);

      // ...and it must not buy that place by crowding the left cluster. The
      // `readiness` reason is an arbitrary-length backend string, so this is
      // the assertion that a long one has to respect.
      expect(reasonBox!.x).toBeGreaterThanOrEqual(
        importYaml!.x + importYaml!.width - 1,
      );

      // With the line inline, the bar is a single control row again.
      const barBox = await composer.actionBar().boundingBox();
      expect(barBox).not.toBeNull();
      expect(barBox!.height).toBeLessThanOrEqual(64);

      // One bottom row, full width, the bar's exact height, the collapse
      // control centred on the bar — which on this one-line bar is the
      // verbs' own top edge, asserted here as such so the registration is a
      // rendered fact and not only a consequence of the centring.
      const bottomRow = await workspaceBottomRow(page);
      expect(bottomRow).not.toBeNull();
      expectBottomRowContract(bottomRow!);
      expect(Math.abs(bottomRow!.collapse!.top - save!.y)).toBeLessThanOrEqual(1);
    } finally {
      await setShowAdvanced(ctx, false);
      await deleteWorkspaceScenario(page, sessionId);
    }
  });

  /* elspeth-b4e88f0f8c, recut when the status chips left the bar for the
     Checks tab. The completion group used to share the bar with the chips: a
     wrapping flex row broke by MAX-content and moved the whole group under
     them at 1280, doubling the bar from 61px to 113px in the DEFAULT state of
     every unvalidated session. The grid recut fixed that, and this row pinned
     the squeezed outcome — the reason wrapped within a 142px column beside
     Run. With the chips gone the group is the bar's ONLY group, the 1fr
     reason column inherits their width up to its 44ch cap, and the gating
     copy fits on ONE line at 1280 with the default 360px pane — the squeeze
     this row used to exercise cannot occur here any more (the widened-pane
     row below now owns the wrapped state). What this row still pins is the
     grid's rendered contract in the new default: one control line, the
     reason on the same line BESIDE Run, clear of the left cluster, and the
     bar no taller than a single control row. */
  test("a gated Run at 1280 keeps the bar to one control line with the reason on one line beside Run", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    const sessionId = await installWorkspaceScenario(page, "empty-freeform");
    const composer = new ComposerPage(page);
    const ctx = await authedContext(tokenFromStorageState(await page.context().storageState()));
    try {
      await setShowAdvanced(ctx, true);
      await composer.goto(sessionId);
      await composer.waitForChatReady();
      // The bar's width now depends on Import YAML being mounted, and that
      // button appears only when fetchUserComposerPreferences() resolves
      // (preferencesStore). Without this barrier a boundingBox capture can
      // resolve against the pre-mount, two-verb bar while the button's own
      // auto-waiting locator resolves post-mount — every button assertion
      // passes and only the reason's wrapped height is wrong.
      await expect(composer.importYaml()).toBeVisible();
      const reason = page.locator(".side-rail-execute-reason");
      await expect(reason).toBeVisible();
      // Gated default state, asserted the same way the 1600 row asserts it.
      await expect(composer.checksTab()).toBeDisabled();
      // The default pane at this width; a widened pane is a different, wider
      // bar-cell budget.
      await expect.poll(() => boxWidth(composer.authoringPane())).toBe(360);

      const [save, importYaml, run, reasonBox] = await Promise.all([
        composer.saveForReview().boundingBox(),
        composer.importYaml().boundingBox(),
        composer.runPipeline().boundingBox(),
        reason.boundingBox(),
      ]);
      for (const box of [save, importYaml, run, reasonBox]) {
        expect(box).not.toBeNull();
      }
      const centre = (b: { y: number; height: number }) => b.y + b.height / 2;

      // One line: all three verbs share a top edge.
      expect(Math.abs(save!.y - importYaml!.y)).toBeLessThanOrEqual(1);
      expect(Math.abs(save!.y - run!.y)).toBeLessThanOrEqual(1);

      // The reason renders on ONE line — asserted, because a wrapped reason
      // here would mean the chips' width had NOT reached the reason column
      // (measured 18px against 44px buttons). It stays beside Run, centred
      // against it, clear of Import.
      expect(reasonBox!.height).toBeLessThanOrEqual(24);
      expect(Math.abs(centre(reasonBox!) - centre(run!))).toBeLessThanOrEqual(1);
      expect(reasonBox!.x + reasonBox!.width).toBeLessThanOrEqual(run!.x + 1);
      expect(reasonBox!.x).toBeGreaterThanOrEqual(
        importYaml!.x + importYaml!.width - 1,
      );

      // And the bar is one control row: the one-line reason is shorter than
      // the 44px buttons, so nothing grew. 64 is the same bound the 1600
      // test uses; the wrapped-under state this replaces measured 112.
      const barBox = await composer.actionBar().boundingBox();
      expect(barBox).not.toBeNull();
      expect(barBox!.height).toBeLessThanOrEqual(64);
      const bottomRow = await workspaceBottomRow(page);
      expect(bottomRow).not.toBeNull();
      expectBottomRowContract(bottomRow!);
    } finally {
      await setShowAdvanced(ctx, false);
      await deleteWorkspaceScenario(page, sessionId);
    }
  });

  /* The registration contract under a reason TALLER than a control row
     (elspeth-b4e88f0f8c, recut when the status chips left the bar for the
     Checks tab). The rows above run against reasons shorter than the 44px
     buttons, so the completion group is one control row tall and every
     alignment value the bar could carry resolves identically — they cannot
     tell align-items: center from flex-start. Only a reason taller than the
     buttons exercises the registration: the bar centres its group, the group
     centres its members, and the bottom row centres the collapse control
     against the bar — with flex-start anywhere in that chain the control (or
     the verbs) would sit above the taller group's centre by half the
     difference, the elspeth-15e8cff7a5 misregistration from the other side.
     Getting a tall reason takes a real squeeze now: with the chips gone the
     1fr reason column reaches its 44ch cap at the default 360px pane, where
     this scenario's validation copy balances onto two lines (36px) — so the
     authoring pane is widened to its 640px max via the separator's keyboard
     End, which shrinks the artifact column toward ARTIFACT_MIN and the
     reason column below the cap until the copy takes three lines and the
     group outgrows the buttons. 900 tall so the density regime is not also
     toggled; TECHNICAL-MODE so the bar carries its full three verbs and the
     column stays as narrow as the product allows. */
  test("a three-line reason keeps the verbs and the collapse control on one centre", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    const sessionId = await installWorkspaceScenario(page, "validation-audit-issues");
    const composer = new ComposerPage(page);
    const ctx = await authedContext(tokenFromStorageState(await page.context().storageState()));
    try {
      await setShowAdvanced(ctx, true);
      await composer.goto(sessionId);
      await composer.waitForChatReady();
      // Both a precondition and a barrier: the three-verb bar must be mounted
      // before anything is measured, and Import YAML mounts only when
      // fetchUserComposerPreferences() resolves.
      await expect(composer.importYaml()).toBeVisible();
      await expect(composer.checksTab()).toHaveAccessibleName(
        "Checks: 25 issues",
      );
      const reason = page.locator(".side-rail-execute-reason");
      await expect(reason).toHaveText(
        "Fix the validation errors shown in the Checks tab before running.",
      );
      await expect.poll(() => boxWidth(composer.authoringPane())).toBe(360);
      // Apply the squeeze: pane to its 640 max, artifact column to its floor.
      await composer.separator().focus();
      await composer.separator().press("End");
      await expect.poll(() => boxWidth(composer.authoringPane())).toBe(640);

      const [save, run, reasonBox, collapse] = await Promise.all([
        composer.saveForReview().boundingBox(),
        composer.runPipeline().boundingBox(),
        reason.boundingBox(),
        composer.collapseAuthoring().boundingBox(),
      ]);
      for (const box of [save, run, reasonBox, collapse]) {
        expect(box).not.toBeNull();
      }
      // Asserted, not assumed: the reason is taller than the buttons here, so
      // the group is taller than a control row and the alignment is exercised.
      expect(reasonBox!.height).toBeGreaterThan(run!.height);
      const centre = (b: { y: number; height: number }) => b.y + b.height / 2;
      expect(Math.abs(centre(save!) - centre(run!))).toBeLessThanOrEqual(1);
      expect(Math.abs(centre(reasonBox!) - centre(run!))).toBeLessThanOrEqual(1);
      expect(Math.abs(centre(collapse!) - centre(run!))).toBeLessThanOrEqual(1);
      // Still one line — the reason grew downward within its column.
      expect(reasonBox!.x).toBeGreaterThan(save!.x);
      const bottomRow = await workspaceBottomRow(page);
      expect(bottomRow).not.toBeNull();
      expectBottomRowContract(bottomRow!);
    } finally {
      await setShowAdvanced(ctx, false);
      await deleteWorkspaceScenario(page, sessionId);
    }
  });

  /* A steady-state check proves the row's contract held ONCE. The reason
     this row exists is that every earlier construction held at 1600 and broke
     somewhere else: a token formula was right in the ungated state and wrong
     by 27px in the gated one; an observer was right whenever it fired. So the
     contract is asserted through a real viewport resize and back — the
     browser's own relayout — and it must hold at all three points without
     anything having to fire: the row is a content-sized grid track, so the
     bar's new height IS the row's new height in the same layout pass.

     Historically this journey crossed the bar's two-group WRAP at 1024 (the
     status chips plus the completion group could not share a line there).
     The chips left the bar for the Checks tab, so the bar holds one group
     and no longer wraps at these widths; the taller-bar state is exercised
     by the three-line-reason row above, and this row keeps the relayout
     journey itself honest. 1024 is still desktop mode (narrow is 960) and
     the height stays 900 so the density regime is not also toggled. */
  test("the bottom row is exactly the action bar's height through a resize and back, with the collapse control centred on it", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    const sessionId = await installWorkspaceScenario(page, "empty-freeform");
    const composer = new ComposerPage(page);
    try {
      await composer.goto(sessionId);
      await composer.waitForChatReady();
      const reason = page.locator(".side-rail-execute-reason");
      await expect(reason).toBeVisible();

      const wide = await workspaceBottomRow(page);
      expect(wide).not.toBeNull();
      expectBottomRowContract(wide!);

      await page.setViewportSize({ width: 1024, height: 900 });
      // Relayout has landed when the bar row's width tracks the viewport —
      // polled because a resize is asynchronous to the test.
      await expect
        .poll(async () => {
          const row = (await workspaceBottomRow(page))?.row;
          return row === undefined ? 0 : row.right - row.left;
        })
        .toBeLessThanOrEqual(1024);

      const narrowed = await workspaceBottomRow(page);
      expect(narrowed).not.toBeNull();
      expectBottomRowContract(narrowed!);

      // ...and back, so a contract that held only in one landed layout could
      // not pass.
      await page.setViewportSize({ width: 1600, height: 900 });
      await expect
        .poll(async () => {
          const row = (await workspaceBottomRow(page))?.row;
          return row === undefined ? 0 : row.right - row.left;
        })
        .toBeGreaterThan(1024);

      const restored = await workspaceBottomRow(page);
      expect(restored).not.toBeNull();
      expectBottomRowContract(restored!);
    } finally {
      await deleteWorkspaceScenario(page, sessionId);
    }
  });

  for (const width of [1280, 980] as const) {
    test(`workspace navigation/status controls use non-overlapping 36px targets at ${width}px`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 720 });
      const sessionId = await installWorkspaceScenario(page, "populated-long-transcript");
      const composer = new ComposerPage(page);
      try {
        await composer.goto(sessionId);
        await composer.waitForChatReady();
        await expect(composer.checksTab()).toHaveAccessibleName("Checks: Ready");
        // One register here now: every artifact-toolbar control runs at the
        // 36px compact rung. The 44px --size-control rows left this matrix
        // with the action-bar status chips (retired to the Checks tab); the
        // bar's own buttons keep that register via the bottom-row contract
        // tests.
        const controls: Array<[ReturnType<ComposerPage["catalogButton"]>, number]> = [
          [composer.artifactTab("Graph"), 36],
          [composer.artifactTab("Spec"), 36],
          [composer.artifactTab("YAML"), 36],
          [composer.checksTab(), 36],
          [composer.artifactTab("Run"), 36],
          [composer.catalogButton(), 36],
          [page.getByRole("button", { name: "Focus graph" }), 36],
        ];
        const boxes = await Promise.all(
          controls.map(([control]) => control.boundingBox()),
        );
        for (const [index, box] of boxes.entries()) {
          expect(box, `control ${index} at ${width}px`).not.toBeNull();
          expect(box!.height, `control ${index} at ${width}px`).toBe(
            controls[index]![1],
          );
          const hit = await controls[index]![0].evaluate((element) => {
            const bounds = element.getBoundingClientRect();
            const hitElement = document.elementFromPoint(
              bounds.left + bounds.width / 2,
              bounds.top + bounds.height / 2,
            );
            return hitElement !== null && element.contains(hitElement);
          });
          expect(hit, `control ${index} hit target at ${width}px`).toBe(true);
        }
      } finally {
        await deleteWorkspaceScenario(page, sessionId);
      }
    });
  }

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
        "Show graph",
      );
      await palette.getByRole("option", { name: /Show graph/ }).click();

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
