import { expect, test, type Locator, type Page } from "@playwright/test";

import {
  deleteWorkspaceScenario,
  installWorkspaceScenario,
} from "./helpers/workspace-fixtures";
import { ComposerPage } from "./page-objects/composer-page";
import { setupWorkspaceScenario } from "./helpers/workspace-setup";

// The graph's accessible component list is a 1px clip that reveals on
// :focus-within (inspector.css .graph-a11y-list) — a deliberate keyboard-only
// disclosure (elspeth-57c6fba409 closed not_a_bug). Nothing else pins that
// path end to end: unit tests never measure geometry, so a CSS change that
// dropped the :focus-within block would leave every vitest suite green while
// making the list unreachable for sighted keyboard users (elspeth-d1feee1e67).
//
// Coverage gap, stated rather than silently accepted: this spec pins the
// list's reveal-on-focus and the focus hand-off to the config panel. It does
// NOT assert that the focused <button> has a visible focus INDICATOR (the
// stylesheet comment's WCAG 2.4.7 claim) and does NOT assert WCAG 2.4.11
// Focus Not Obscured. Neither is claimed here.
//
// Roving-tabindex / ArrowDown navigation is NOT implemented (plain <button>s,
// no roving tabindex) and is not asserted below — parked as an APG
// enhancement, not a regression this spec guards against.

const VIEWPORT = { width: 1536, height: 760 };
const MAX_TABS_TO_REACH_LIST = 40;
const COLLAPSED_CLIP = "rect(0px, 0px, 0px, 0px)";
const REVEALED_CLIP = "auto";

async function expectCollapsed(list: Locator): Promise<void> {
  await expect
    .poll(async () => (await list.boundingBox())?.width ?? 0)
    .toBeLessThanOrEqual(1);
  await expect
    .poll(async () => (await list.boundingBox())?.height ?? 0)
    .toBeLessThanOrEqual(1);
  // clip is the other half of the collapse and geometry cannot see it:
  // getBoundingClientRect returns the LAYOUT box, and `clip` affects only
  // the painted region. Without this, dropping `clip: auto` from the
  // :focus-within block hides the list with every geometry assertion above
  // still green.
  await expect
    .poll(() => list.evaluate((el) => getComputedStyle(el).clip))
    .toBe(COLLAPSED_CLIP);
}

async function expectRevealed(list: Locator): Promise<void> {
  await expect
    .poll(async () => (await list.boundingBox())?.width ?? 0)
    .toBeGreaterThan(1);
  await expect
    .poll(async () => (await list.boundingBox())?.height ?? 0)
    .toBeGreaterThan(1);
  await expect
    .poll(() => list.evaluate((el) => getComputedStyle(el).clip))
    .toBe(REVEALED_CLIP);
}

async function tabUntilFocusWithin(page: Page, container: Locator): Promise<void> {
  for (let i = 0; i < MAX_TABS_TO_REACH_LIST; i += 1) {
    if (await container.evaluate((el) => el.contains(document.activeElement))) return;
    await page.keyboard.press("Tab");
  }
  throw new Error(`focus did not enter the component list within ${MAX_TABS_TO_REACH_LIST} Tab presses`);
}

test.describe("graph a11y component list — keyboard path", () => {
  test("is a 1px clip until focus enters it, then reveals and opens the config panel on Enter", async ({ page }) => {
    await page.setViewportSize(VIEWPORT);
    const { sessionId, value: composer } = await setupWorkspaceScenario(
      () => installWorkspaceScenario(page, "populated-long-transcript"),
      async (createdSessionId) => {
        const created = new ComposerPage(page);
        await created.goto(createdSessionId);
        await created.waitForChatReady();
        return created;
      },
      (createdSessionId) => deleteWorkspaceScenario(page, createdSessionId),
    );
    try {
      await composer.artifactTab("Graph").click();
      const list = page.getByRole("list", { name: /pipeline components in source-to-sink order/i });
      await expect(list).toBeAttached();

      // Pointer users can never hit it: with focus elsewhere the box is ≤1px
      // and clipped to nothing.
      await composer.chatInput().focus();
      await expectCollapsed(list);

      // Tab from the Graph tab until focus lands inside the list (the reveal).
      await composer.artifactTab("Graph").focus();
      await tabUntilFocusWithin(page, list);
      await expectRevealed(list);
      const items = list.getByRole("button");
      await expect(items).toHaveCount(2); // source + results; no nodes in this scenario
      await expect(items.first()).toBeFocused();

      // Tab moves between items (plain buttons, no roving tabindex); Enter
      // activates and hands focus to the configuration panel.
      await page.keyboard.press("Tab");
      await expect(items.nth(1)).toBeFocused();
      await page.keyboard.press("Enter");
      const panel = page.getByRole("complementary", { name: /configuration$/ });
      await expect(panel).toBeVisible();
      await expect(panel).toBeFocused();
      await expect(panel).toHaveAccessibleName("results configuration");

      // Focus left the list, so it collapses again.
      await expectCollapsed(list);
    } finally {
      await deleteWorkspaceScenario(page, sessionId);
    }
  });

  // --- Negative controls -----------------------------------------------
  //
  // These two are NOT committed tests — `test.skip` means they never run in
  // CI or a normal `npx playwright test` invocation. They exist so a later
  // reviewer can reproduce, by hand, the proof that the spec above actually
  // catches the regression it exists for. Each injects its override at
  // RUNTIME via Playwright's `addStyleTag` — never by editing inspector.css
  // on disk (this is a shared checkout; a tracked-file mutation round-trip is
  // the same hazard class as `git restore`, only narrower).
  //
  // To run one by hand: change `test.skip` to `test` for that block only,
  // run `npx playwright test tests/e2e/composer-workspace-graph-keyboard.spec.ts`,
  // observe the stated failure, then revert.
  //
  // Run 1 (full override — width/height/clip together): overall test FAILS,
  // but NOT on the width/height polls as originally guessed. Verified by
  // hand 2026-08-30: with only width/height/clip forced back to collapsed
  // values, `list.boundingBox()` still measures > 1px on both axes (the
  // :focus-within rule's padding/border/scrollbar layout are untouched by
  // this override, box-sizing: border-box notwithstanding — measured 10x10px
  // here), so the width/height polls pass. The failure lands on the CLIP
  // poll: `getComputedStyle(el).clip` reads back `rect(0px, 0px, 0px, 0px)`
  // against an expected `"auto"`. In other words, geometry alone would have
  // been blind to this override too — not only to the clip-only one below.
  //
  // Run 2 (clip-only override): proves the clip assertion added above is
  // load-bearing on its own. Without it, dropping only `clip: auto` from the
  // :focus-within block would leave every geometry assertion green while a
  // sighted keyboard user still cannot see the list (clip affects the
  // painted region; getBoundingClientRect measures the layout box and does
  // not see it, and Playwright's toBeVisible() does not consider it either).
  // With the clip poll present, this control makes expectRevealed's clip
  // assertion FAIL, for the same reason as Run 1. Verified by hand 2026-08-30.
  test.skip(
    "control: full :focus-within override (width/height/clip) makes the reveal assertion fail",
    async ({ page }) => {
      await page.setViewportSize(VIEWPORT);
      const { sessionId, value: composer } = await setupWorkspaceScenario(
        () => installWorkspaceScenario(page, "populated-long-transcript"),
        async (createdSessionId) => {
          const created = new ComposerPage(page);
          await created.goto(createdSessionId);
          await created.waitForChatReady();
          return created;
        },
        (createdSessionId) => deleteWorkspaceScenario(page, createdSessionId),
      );
      try {
        await composer.artifactTab("Graph").click();
        await page.addStyleTag({
          content:
            ".graph-a11y-list:focus-within { width: 1px !important; height: 1px !important; clip: rect(0 0 0 0) !important; }",
        });
        const list = page.getByRole("list", { name: /pipeline components in source-to-sink order/i });
        await composer.artifactTab("Graph").focus();
        await tabUntilFocusWithin(page, list);
        // Expected outcome when this block is switched from test.skip to
        // test: FAILS here. Verified 2026-08-30: the failure lands on the
        // CLIP poll specifically, not the width/height polls — this override
        // does not shrink the rendered box to 1x1 (non-overridden padding/
        // border/scrollbar layout keep boundingBox() > 1px on both axes even
        // with the reveal killed), so geometry alone is blind to this
        // regression too. Only the clip assertion catches it.
        await expectRevealed(list);
      } finally {
        await deleteWorkspaceScenario(page, sessionId);
      }
    },
  );

  test.skip(
    "control: clip-only :focus-within override makes the clip assertion fail",
    async ({ page }) => {
      await page.setViewportSize(VIEWPORT);
      const { sessionId, value: composer } = await setupWorkspaceScenario(
        () => installWorkspaceScenario(page, "populated-long-transcript"),
        async (createdSessionId) => {
          const created = new ComposerPage(page);
          await created.goto(createdSessionId);
          await created.waitForChatReady();
          return created;
        },
        (createdSessionId) => deleteWorkspaceScenario(page, createdSessionId),
      );
      try {
        await composer.artifactTab("Graph").click();
        await page.addStyleTag({
          content: ".graph-a11y-list:focus-within { clip: rect(0 0 0 0) !important; }",
        });
        const list = page.getByRole("list", { name: /pipeline components in source-to-sink order/i });
        await composer.artifactTab("Graph").focus();
        await tabUntilFocusWithin(page, list);
        // Expected outcome when this block is switched from test.skip to
        // test: width/height polls PASS (layout box is unaffected by clip)
        // but the clip poll FAILS — this is the regression class geometry
        // alone cannot see.
        await expectRevealed(list);
      } finally {
        await deleteWorkspaceScenario(page, sessionId);
      }
    },
  );
});
