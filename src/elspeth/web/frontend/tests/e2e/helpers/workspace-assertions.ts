import { expect, type Locator, type Page } from "@playwright/test";

import type { ComposerPage } from "../page-objects/composer-page";

interface ScrollCandidate {
  selector: string;
  className: string;
  overflowX: string;
  overflowY: string;
  scrollWidth: number;
  clientWidth: number;
  scrollHeight: number;
  clientHeight: number;
}

export async function boxWidth(locator: Locator): Promise<number> {
  return (await locator.boundingBox())?.width ?? 0;
}

export async function boxHeight(locator: Locator): Promise<number> {
  return (await locator.boundingBox())?.height ?? 0;
}

export async function expectDesktopWorkspaceGeometry(
  page: Page,
  composer: ComposerPage,
): Promise<void> {
  await expect(composer.workspace()).toHaveAttribute("data-layout-mode", "desktop");
  await expect(composer.authoringPane()).toBeVisible();
  await expect(composer.artifactRegion()).toBeVisible();
  await expect(composer.activeArtifactPanel()).toBeVisible();
  await expect.poll(() => boxWidth(composer.authoringPane())).toBeGreaterThanOrEqual(360);
  await expect.poll(() => boxWidth(composer.authoringPane())).toBeLessThanOrEqual(640);
  await expect.poll(() => boxWidth(composer.artifactRegion())).toBeGreaterThanOrEqual(640);
  await expect.poll(() => boxHeight(composer.activeArtifactPanel())).toBeGreaterThanOrEqual(420);
  expect(page.viewportSize()).not.toBeNull();
}

export async function expectNoDocumentHorizontalOverflow(page: Page): Promise<void> {
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      ),
    )
    .toBeLessThanOrEqual(0);
}

async function expectControlReachable(control: Locator): Promise<void> {
  await expect(control).toBeVisible();
  if (await control.isEnabled()) {
    await control.focus();
    await expect(control).toBeFocused();
  }
  const reachable = await control.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    const centerX = bounds.left + bounds.width / 2;
    const centerY = bounds.top + bounds.height / 2;
    const hit = document.elementFromPoint(centerX, centerY);
    return {
      inside:
        bounds.width > 0 &&
        bounds.height > 0 &&
        bounds.left >= 0 &&
        bounds.top >= 0 &&
        bounds.right <= document.documentElement.clientWidth &&
        bounds.bottom <= document.documentElement.clientHeight,
      uncovered:
        hit !== null &&
        (hit === element || element.contains(hit) || hit.contains(element)),
    };
  });
  expect(reachable.inside).toBe(true);
  expect(reachable.uncovered).toBe(true);
}

export async function expectPrimaryControlsInViewport(
  _page: Page,
  composer: ComposerPage,
): Promise<void> {
  const controls = [
    composer.artifactTab("Graph"),
    composer.validationStatus(),
    composer.auditStatus(),
    composer.collapseAuthoring(),
  ];
  if (await composer.moreActions().isVisible()) controls.push(composer.moreActions());
  if (await composer.runPipeline().isVisible()) controls.push(composer.runPipeline());
  for (const control of controls) await expectControlReachable(control);
}

export async function expectIntendedPaneScrollers(
  page: Page,
  options: {
    transcriptMustScroll?: boolean;
    inspectorMustScroll?: boolean;
  } = {},
): Promise<void> {
  const candidates = await page.getByTestId("composer-workspace").evaluate(
    (workspace): ScrollCandidate[] => {
      const result: ScrollCandidate[] = [];
      for (const element of workspace.querySelectorAll<HTMLElement>("*")) {
        if (element.hidden || element.closest("[hidden]")) continue;
        // Text entry controls own bounded internal scrolling after their
        // viewport-relative maximum; they are not pane-level scroll owners.
        if (
          element instanceof HTMLTextAreaElement ||
          element instanceof HTMLInputElement ||
          element instanceof HTMLSelectElement
        ) {
          continue;
        }
        const style = getComputedStyle(element);
        const vertical =
          (style.overflowY === "auto" || style.overflowY === "scroll") &&
          element.scrollHeight > element.clientHeight + 1;
        const horizontal =
          (style.overflowX === "auto" || style.overflowX === "scroll") &&
          element.scrollWidth > element.clientWidth + 1;
        if (!vertical && !horizontal) continue;
        result.push({
          selector:
            element.getAttribute("aria-label") ??
            element.getAttribute("role") ??
            element.tagName.toLowerCase(),
          className: element.className,
          overflowX: style.overflowX,
          overflowY: style.overflowY,
          scrollWidth: element.scrollWidth,
          clientWidth: element.clientWidth,
          scrollHeight: element.scrollHeight,
          clientHeight: element.clientHeight,
        });
      }
      return result;
    },
  );
  const allowedClasses = [
    "chat-panel-messages",
    "guided-authoring-scroll",
    "artifact-workspace-panel",
    "workspace-inspector-body",
  ];
  const unexpected = candidates.filter(
    (candidate) =>
      !allowedClasses.some((className) =>
        candidate.className.split(/\s+/).includes(className),
      ),
  );
  expect(unexpected, `unexpected routine scrollers: ${JSON.stringify(unexpected)}`).toEqual([]);

  if (options.transcriptMustScroll) {
    expect(
      candidates.some((candidate) =>
        candidate.className.split(/\s+/).includes("chat-panel-messages"),
      ),
    ).toBe(true);
  }
  if (options.inspectorMustScroll) {
    expect(
      candidates.some((candidate) =>
        candidate.className.split(/\s+/).includes("workspace-inspector-body"),
      ),
    ).toBe(true);
  }
}

export async function expectResizeGeometry(
  _page: Page,
  composer: ComposerPage,
  viewportWidth: number,
): Promise<void> {
  const separator = composer.separator();
  const expectedDefault = viewportWidth < 1536 ? 360 : 420;
  const expectedMaximum = Math.min(640, viewportWidth - 640);
  await expect.poll(() => boxWidth(composer.authoringPane())).toBe(expectedDefault);
  await separator.focus();
  await separator.press("Home");
  await expect.poll(() => boxWidth(composer.authoringPane())).toBe(360);
  await separator.press("End");
  await expect.poll(() => boxWidth(composer.authoringPane())).toBe(expectedMaximum);
  await expect.poll(() => boxWidth(composer.artifactRegion())).toBeGreaterThanOrEqual(640);
  await expect(separator).toHaveAttribute("aria-valuemin", "360");
  await expect(separator).toHaveAttribute("aria-valuemax", String(expectedMaximum));
}

export async function expectDialogGeometry(
  page: Page,
  dialog: Locator,
): Promise<void> {
  const geometry = await dialog.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    const body = element.querySelector<HTMLElement>(".confirm-dialog-body");
    const title = element.querySelector<HTMLElement>(".confirm-dialog-title");
    const actions = element.querySelector<HTMLElement>(".confirm-dialog-actions");
    if (body === null || title === null || actions === null) {
      throw new Error("dialog frame is missing title, body, or actions");
    }
    return {
      top: bounds.top,
      bottom: bounds.bottom,
      bodyOverflowY: getComputedStyle(body).overflowY,
      bodyScrolls: body.scrollHeight > body.clientHeight,
      titleVisible: title.getBoundingClientRect().top >= bounds.top,
      actionsVisible: actions.getBoundingClientRect().bottom <= bounds.bottom,
    };
  });
  expect(geometry.top).toBeGreaterThanOrEqual(16);
  expect(geometry.bottom).toBeLessThanOrEqual(page.viewportSize()!.height - 16);
  expect(geometry.bodyOverflowY).toBe("auto");
  expect(geometry.bodyScrolls).toBe(true);
  expect(geometry.titleVisible).toBe(true);
  expect(geometry.actionsVisible).toBe(true);
}
