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
  owner: "authoring" | "artifact" | "inspector" | "unexpected";
  vertical: boolean;
  horizontal: boolean;
}

export interface WorkspaceControlCapabilities {
  completion: boolean;
  moreActions: boolean;
}

export async function boxWidth(locator: Locator): Promise<number> {
  return (await locator.boundingBox())?.width ?? 0;
}

export async function boxHeight(locator: Locator): Promise<number> {
  return (await locator.boundingBox())?.height ?? 0;
}

/**
 * Width of the artifact region a user can actually see (elspeth-750f92e132).
 *
 * The inspector is an absolutely-positioned drawer pinned to the pane's right
 * edge, so while it is open the pane's own bounding box overstates the usable
 * region — the old contract measured 920px where the visible strip was 408px,
 * which is how a P1 that sliced the primary button in half shipped under a
 * green gate. Subtract the horizontal overlap of a VISIBLE inspector from the
 * pane's box; with no inspector open this equals boxWidth(artifactRegion()),
 * so inspector-free specs are unchanged.
 */
export async function unoccludedArtifactWidth(composer: ComposerPage): Promise<number> {
  const pane = await composer.artifactRegion().boundingBox();
  if (pane === null) return 0;
  const inspector = composer.page.locator(".workspace-inspector");
  if ((await inspector.count()) === 0 || !(await inspector.first().isVisible())) {
    return pane.width;
  }
  const drawer = await inspector.first().boundingBox();
  if (drawer === null) return pane.width;
  const overlap = Math.max(
    0,
    Math.min(pane.x + pane.width, drawer.x + drawer.width) - Math.max(pane.x, drawer.x),
  );
  return pane.width - overlap;
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
  // The floor is asserted against the UNOCCLUDED width, not the pane's own
  // box — with the inspector drawer open the two differ by the drawer's full
  // width, and the pane-box form certified a 408px strip as 920px
  // (elspeth-750f92e132). Callers exercising an inspector-open state at a
  // viewport that cannot afford 360 + drawer + 640 are expected to fail here:
  // that failure is the contract working, not flake.
  await expect.poll(() => unoccludedArtifactWidth(composer)).toBeGreaterThanOrEqual(640);
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
        (hit === element || element.contains(hit)),
    };
  });
  expect(reachable.inside).toBe(true);
  expect(reachable.uncovered).toBe(true);
}

export async function expectPrimaryControlsInViewport(
  page: Page,
  composer: ComposerPage,
  capabilities: WorkspaceControlCapabilities,
): Promise<void> {
  for (const name of ["Graph", "Spec", "YAML", "Run"] as const) {
    await expectControlReachable(composer.artifactTab(name));
  }
  await expectControlReachable(composer.validationStatus());
  await expectControlReachable(composer.auditStatus());

  const completionControls = [
    composer.saveForReview(),
    composer.runPipeline(),
    composer.importYaml(),
  ];
  for (const control of completionControls) {
    await expect(control).toHaveCount(capabilities.completion ? 1 : 0);
    if (capabilities.completion) await expectControlReachable(control);
  }
  await expect(composer.moreActions()).toHaveCount(
    capabilities.moreActions ? 1 : 0,
  );
  if (capabilities.moreActions) {
    await expectControlReachable(composer.moreActions());
  }

  await composer.validationStatus().click();
  await expect(composer.inspector()).toBeVisible();
  for (const name of ["Validation", "Audit"] as const) {
    await expectControlReachable(composer.inspectorTab(name));
  }
  await expectControlReachable(composer.closeInspector());
  await composer.closeInspector().click();

  await expectControlReachable(composer.collapseAuthoring());
  await composer.collapseAuthoring().click();
  await expectControlReachable(composer.restoreAuthoring());
  await composer.restoreAuthoring().click();
  await expect(composer.collapseAuthoring()).toBeFocused();
  expect(page.viewportSize()).not.toBeNull();
}

export async function expectIntendedPaneScrollers(
  page: Page,
  options: {
    transcriptMustScroll?: boolean;
    inspectorMustScroll?: boolean;
  } = {},
): Promise<void> {
  const report = await page.getByTestId("composer-workspace").evaluate(
    (workspace): { candidates: ScrollCandidate[]; outerOverflow: ScrollCandidate[] } => {
      const result: ScrollCandidate[] = [];
      const describe = (
        element: HTMLElement,
        vertical: boolean,
        horizontal: boolean,
      ): ScrollCandidate => {
        const style = getComputedStyle(element);
        const owner = element.matches(
          ".chat-panel-messages, .guided-authoring-scroll",
        )
          ? "authoring"
          : element.matches(".artifact-workspace-panel:not([hidden])")
            ? "artifact"
            : element.matches(".workspace-inspector-body") &&
                element.closest<HTMLElement>(".workspace-inspector")?.hidden === false
              ? "inspector"
              : "unexpected";
        return {
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
          owner,
          vertical,
          horizontal,
        };
      };
      for (const element of workspace.querySelectorAll<HTMLElement>("*")) {
        if (
          element.hidden ||
          element.closest("[hidden]") !== null ||
          element.getClientRects().length === 0
        ) {
          continue;
        }
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
        result.push(describe(element, vertical, horizontal));
      }
      const outerElements = new Set<HTMLElement>([
        document.documentElement as HTMLElement,
        document.body,
        ...document.querySelectorAll<HTMLElement>(".app-root, .app-main"),
        workspace as HTMLElement,
      ]);
      for (let ancestor = workspace.parentElement; ancestor !== null; ancestor = ancestor.parentElement) {
        outerElements.add(ancestor as HTMLElement);
      }
      const outerOverflow = [...outerElements].flatMap((element) => {
        const vertical = element.scrollHeight > element.clientHeight + 1;
        const horizontal = element.scrollWidth > element.clientWidth + 1;
        return vertical || horizontal
          ? [describe(element, vertical, horizontal)]
          : [];
      });
      return { candidates: result, outerOverflow };
    },
  );
  const { candidates, outerOverflow } = report;
  expect(
    outerOverflow,
    `outer desktop surfaces must not scroll: ${JSON.stringify(outerOverflow)}`,
  ).toEqual([]);
  const unexpected = candidates.filter((candidate) => candidate.owner === "unexpected");
  expect(unexpected, `unexpected routine scrollers: ${JSON.stringify(unexpected)}`).toEqual([]);

  const unexpectedHorizontal = candidates.filter(
    (candidate) => candidate.horizontal && candidate.owner !== "artifact",
  );
  expect(
    unexpectedHorizontal,
    `only the active artifact body may scroll horizontally: ${JSON.stringify(unexpectedHorizontal)}`,
  ).toEqual([]);
  for (const owner of ["authoring", "artifact", "inspector"] as const) {
    const verticalOwners = candidates.filter(
      (candidate) => candidate.owner === owner && candidate.vertical,
    );
    expect(
      verticalOwners.length,
      `at most one visible vertical scroll owner is allowed for ${owner}: ${JSON.stringify(verticalOwners)}`,
    ).toBeLessThanOrEqual(1);
  }

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
  page: Page,
  composer: ComposerPage,
  viewportWidth: number,
): Promise<void> {
  const separator = composer.separator();
  const expectedDefault = viewportWidth < 1536 ? 360 : 420;
  const expectedMaximum = Math.min(640, viewportWidth - 640);
  await expect.poll(() => boxWidth(composer.authoringPane())).toBe(expectedDefault);
  const separatorBounds = await separator.boundingBox();
  if (separatorBounds === null) throw new Error("workspace separator has no pointer target");
  const startX = separatorBounds.x + separatorBounds.width / 2;
  const startY = separatorBounds.y + separatorBounds.height / 2;
  await page.mouse.move(startX, startY);
  await page.mouse.down();
  await page.mouse.move(viewportWidth, startY, { steps: 8 });
  await page.mouse.up();
  await expect.poll(() => boxWidth(composer.authoringPane())).toBe(expectedMaximum);
  await expect.poll(() => unoccludedArtifactWidth(composer)).toBeGreaterThanOrEqual(640);
  await separator.focus();
  await separator.press("Home");
  await expect.poll(() => boxWidth(composer.authoringPane())).toBe(360);
  await separator.press("End");
  await expect.poll(() => boxWidth(composer.authoringPane())).toBe(expectedMaximum);
  await expect.poll(() => unoccludedArtifactWidth(composer)).toBeGreaterThanOrEqual(640);
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
      bodyHorizontalOverflow: body.scrollWidth - body.clientWidth,
      maxContentHorizontalOverflow: Math.max(
        0,
        ...[...body.querySelectorAll<HTMLElement>("*")].map(
          (content) => content.scrollWidth - content.clientWidth,
        ),
      ),
      titleVisible: (() => {
        const titleBounds = title.getBoundingClientRect();
        return (
          titleBounds.top >= bounds.top &&
          titleBounds.left >= bounds.left &&
          titleBounds.right <= bounds.right
        );
      })(),
      actionsVisible: (() => {
        const actionBounds = actions.getBoundingClientRect();
        return (
          actionBounds.bottom <= bounds.bottom &&
          actionBounds.left >= bounds.left &&
          actionBounds.right <= bounds.right
        );
      })(),
    };
  });
  expect(geometry.top).toBeGreaterThanOrEqual(16);
  expect(geometry.bottom).toBeLessThanOrEqual(page.viewportSize()!.height - 16);
  expect(geometry.bodyOverflowY).toBe("auto");
  expect(geometry.bodyScrolls).toBe(true);
  expect(geometry.bodyHorizontalOverflow).toBeLessThanOrEqual(0);
  expect(geometry.maxContentHorizontalOverflow).toBeLessThanOrEqual(0);
  expect(geometry.titleVisible).toBe(true);
  expect(geometry.actionsVisible).toBe(true);
}
