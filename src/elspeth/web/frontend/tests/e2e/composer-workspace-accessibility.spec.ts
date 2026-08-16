import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Locator, type Page } from "@playwright/test";

import {
  deleteWorkspaceScenario,
  installWorkspaceScenario,
  type WorkspaceScenario,
} from "./helpers/workspace-fixtures";
import { ComposerPage } from "./page-objects/composer-page";
import { setupWorkspaceScenario } from "./helpers/workspace-setup";

const ACCESSIBILITY_VIEWPORT = { width: 1536, height: 760 };
const AXE_TAGS = [
  "wcag2a",
  "wcag2aa",
  "wcag21a",
  "wcag21aa",
  "wcag22aa",
] as const;

interface DeferredSignal {
  promise: Promise<void>;
  release: () => void;
}

function deferredSignal(): DeferredSignal {
  let release: (() => void) | undefined;
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  if (release === undefined) throw new Error("failed to initialize signal");
  return { promise, release };
}

async function openScenario(
  page: Page,
  scenario: WorkspaceScenario,
  viewport = ACCESSIBILITY_VIEWPORT,
): Promise<{ composer: ComposerPage; sessionId: string }> {
  await page.setViewportSize(viewport);
  const { sessionId, value: composer } = await setupWorkspaceScenario(
    () => installWorkspaceScenario(page, scenario),
    async (createdSessionId) => {
      const createdComposer = new ComposerPage(page);
      await createdComposer.goto(createdSessionId);
      await createdComposer.waitForChatReady();
      return createdComposer;
    },
    (createdSessionId) => deleteWorkspaceScenario(page, createdSessionId),
  );
  return { composer, sessionId };
}

async function expectUncoveredWhenFocused(
  locator: Locator,
): Promise<void> {
  await locator.scrollIntoViewIfNeeded();
  await expect(locator).toBeEnabled();
  await locator.click({ trial: true });
  await locator.focus();
  await expect(locator).toBeFocused();
  const geometry = await locator.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    const centerX = bounds.left + bounds.width / 2;
    const centerY = bounds.top + bounds.height / 2;
    const hit = document.elementFromPoint(centerX, centerY);
    return {
      insideViewport:
        bounds.width > 0 &&
        bounds.height > 0 &&
        bounds.left >= 0 &&
        bounds.top >= 0 &&
        bounds.right <= document.documentElement.clientWidth &&
        bounds.bottom <= document.documentElement.clientHeight,
      uncovered:
        hit !== null && (hit === element || element.contains(hit)),
    };
  });
  expect(geometry).toEqual({ insideViewport: true, uncovered: true });
}

async function expectNoTargetAxeViolations(page: Page): Promise<void> {
  const results = await new AxeBuilder({ page })
    .withTags([...AXE_TAGS])
    .analyze();
  expect(results.violations).toEqual([]);
}

test.describe("Composer workspace browser accessibility", () => {
  test("preserves semantic DOM order and matching desktop visual order", async ({
    page,
  }) => {
    const { composer, sessionId } = await openScenario(
      page,
      "populated-long-transcript",
    );
    try {
      const parts = await composer.workspace()
        .locator(":scope > [data-workspace-part]")
        .evaluateAll((elements) =>
          elements.map((element) => element.getAttribute("data-workspace-part")),
        );
      // The bottom bar row is its own part since elspeth-9c94a58500: it
      // follows both panes in DOM order because it sits BELOW both of them,
      // spanning the two columns — so the sequence still reads as the visual
      // order, left to right across the panes and then down to the bar.
      expect(parts).toEqual([
        "view-tabs",
        "authoring",
        "separator",
        "artifact",
        "action-bar",
        "inspector",
      ]);

      const authoring = await composer.authoringPane().boundingBox();
      // Measure the zero-width grid boundary, not the separator's intentional
      // 24px pointer target that straddles it for motor accessibility.
      const separator = await composer.workspace()
        .locator(':scope > [data-workspace-part="separator"]')
        .boundingBox();
      const artifact = await composer.artifactRegion().boundingBox();
      const bottomBar = await composer.workspace()
        .locator(':scope > [data-workspace-part="action-bar"]')
        .boundingBox();
      expect(authoring).not.toBeNull();
      expect(separator).not.toBeNull();
      expect(artifact).not.toBeNull();
      expect(bottomBar).not.toBeNull();
      expect(authoring!.x + authoring!.width).toBeLessThanOrEqual(separator!.x);
      expect(separator!.x + separator!.width).toBeLessThanOrEqual(artifact!.x);
      expect(artifact!.x).toBeGreaterThan(authoring!.x);
      expect(bottomBar!.y).toBeGreaterThanOrEqual(authoring!.y + authoring!.height - 1);
      expect(bottomBar!.y).toBeGreaterThanOrEqual(artifact!.y + artifact!.height - 1);
      expect(bottomBar!.x).toBeLessThanOrEqual(authoring!.x);
      expect(bottomBar!.x + bottomBar!.width).toBeGreaterThanOrEqual(
        artifact!.x + artifact!.width,
      );
    } finally {
      await deleteWorkspaceScenario(page, sessionId);
    }
  });

  test("uses a focus-following roving artifact tab pattern", async ({ page }) => {
    const { composer, sessionId } = await openScenario(
      page,
      "populated-long-transcript",
    );
    try {
      const graph = composer.artifactTab("Graph");
      const spec = composer.artifactTab("Spec");
      const run = composer.artifactTab("Run");
      await expect(spec).toBeEnabled();
      await graph.focus();
      await graph.press("ArrowRight");
      await expect(spec).toBeFocused();
      await expect(spec).toHaveAttribute("aria-selected", "true");
      await spec.press("End");
      await expect(run).toBeFocused();
      await expect(run).toHaveAttribute("aria-selected", "true");
      const runPanelId = await run.getAttribute("aria-controls");
      expect(runPanelId).toBe("artifact-panel-run");
      await expect(page.locator(`#${runPanelId}`)).toHaveAttribute(
        "aria-labelledby",
        "artifact-tab-run",
      );
      await run.press("Home");
      await expect(graph).toBeFocused();
      await graph.press("ArrowLeft");
      await expect(run).toBeFocused();
    } finally {
      await deleteWorkspaceScenario(page, sessionId);
    }
  });

  test("publishes separator values and resizes by keyboard", async ({ page }) => {
    const { composer, sessionId } = await openScenario(
      page,
      "populated-long-transcript",
    );
    try {
      const separator = composer.separator();
      await expect(separator).toHaveAttribute("aria-orientation", "vertical");
      await expect(separator).toHaveAttribute("aria-valuemin", "360");
      await expect(separator).toHaveAttribute("aria-valuemax", "640");
      await expect(separator).toHaveAttribute("aria-valuenow", "420");
      await separator.focus();
      await separator.press("ArrowRight");
      await expect(separator).toHaveAttribute("aria-valuenow", "436");
      await separator.press("Shift+ArrowRight");
      await expect(separator).toHaveAttribute("aria-valuenow", "484");
      await separator.press("Home");
      await expect(separator).toHaveAttribute("aria-valuenow", "360");
      await separator.press("End");
      await expect(separator).toHaveAttribute("aria-valuenow", "640");
    } finally {
      await deleteWorkspaceScenario(page, sessionId);
    }
  });

  test("collapses and restores authoring with exact focus transfer", async ({
    page,
  }) => {
    const { composer, sessionId } = await openScenario(
      page,
      "populated-long-transcript",
    );
    try {
      const collapse = composer.collapseAuthoring();
      await collapse.focus();
      await collapse.press("Enter");
      const restore = composer.restoreAuthoring();
      await expect(restore).toBeFocused();
      await expect(composer.authoringPane()).toBeHidden();
      await expect(composer.separator()).toBeHidden();
      await restore.press("Enter");
      await expect(composer.collapseAuthoring()).toBeFocused();
      await expect(composer.authoringPane()).toBeVisible();
      await expect(composer.separator()).toBeVisible();
    } finally {
      await deleteWorkspaceScenario(page, sessionId);
    }
  });

  test("restores focus from the inspector and a confirmation dialog", async ({
    page,
  }) => {
    const { composer, sessionId } = await openScenario(
      page,
      "populated-long-transcript",
      { width: 1280, height: 720 },
    );
    try {
      await expect(page.locator(".react-flow__node")).toHaveCount(2);
      const validation = composer.validationStatus();
      await expect(validation).toHaveAccessibleName("Validation: Passed");
      await validation.focus();
      await validation.press("Enter");
      await expect(composer.inspectorTab("Validation")).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(validation).toBeFocused();

      const run = composer.runPipeline();
      await expect(run).toBeEnabled();
      await run.focus();
      await run.press("Enter");
      const dialog = page.getByRole("alertdialog", { name: "Run pipeline" });
      await expect(dialog).toBeVisible();
      await expect(
        dialog.getByRole("button", { name: "Run pipeline" }),
      ).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(dialog).toBeHidden();
      await expect(run).toBeFocused();
    } finally {
      await deleteWorkspaceScenario(page, sessionId);
    }
  });

  test("keeps focused controls visible above sticky workspace regions", async ({
    page,
  }) => {
    const { composer, sessionId } = await openScenario(
      page,
      "populated-long-transcript",
      { width: 1280, height: 720 },
    );
    try {
      for (const control of [
        composer.chatInput(),
        composer.artifactTab("Graph"),
        composer.validationStatus(),
        composer.runPipeline(),
        composer.collapseAuthoring(),
      ]) {
        await expectUncoveredWhenFocused(control);
      }
    } finally {
      await deleteWorkspaceScenario(page, sessionId);
    }
  });

  test("gives busy and error states stable accessible names", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 720 });
    const sessionId = await installWorkspaceScenario(
      page,
      "validation-audit-issues",
    );
    const auditGate = deferredSignal();
    await page.route(
      `**/api/sessions/${sessionId}/audit-readiness`,
      async (route) => {
        await auditGate.promise;
        await route.fallback();
      },
    );
    const composer = new ComposerPage(page);
    try {
      await composer.goto(sessionId);
      await composer.waitForChatReady();
      await expect(composer.auditStatus()).toHaveAccessibleName(
        "Audit: Checking",
      );
      await composer.auditStatus().click();
      const auditRegion = page.getByRole("region", { name: "Audit readiness" });
      await expect(auditRegion).toHaveAttribute("aria-busy", "true");
      await expect(auditRegion).toContainText("Checking audit readiness");

      auditGate.release();
      await expect(composer.validationStatus()).toHaveAccessibleName(
        "Validation: 24 errors",
      );
      await expect(composer.auditStatus()).toHaveAccessibleName(
        "Audit: 2 issues",
      );
      await expect(
        auditRegion.getByRole("button", {
          name: /Error.*Validation.*24 validation issues require attention/i,
        }),
      ).toBeVisible();
    } finally {
      auditGate.release();
      await deleteWorkspaceScenario(page, sessionId);
    }
  });

  test("has no target Axe violations when populated and with the inspector open", async ({
    page,
  }) => {
    const { composer, sessionId } = await openScenario(
      page,
      "validation-audit-issues",
      { width: 1280, height: 720 },
    );
    try {
      await expect(page.locator(".react-flow__node")).toHaveCount(2);
      await expect(composer.validationStatus()).toHaveAccessibleName(
        "Validation: 24 errors",
      );
      await page.evaluate(async () => {
        await document.fonts.ready;
      });
      await expectNoTargetAxeViolations(page);

      await composer.validationStatus().click();
      await expect(composer.inspector()).toBeVisible();
      await expect(composer.inspectorTab("Validation")).toBeFocused();
      await expectNoTargetAxeViolations(page);
    } finally {
      await deleteWorkspaceScenario(page, sessionId);
    }
  });
});
