// WCAG 1.4.10 reflow geometry (elspeth-7aa9787996).
//
// The shell pins itself to the viewport at every level (html/body
// height:100% + overflow:hidden, .app-root 100%, .app-main flex:1, grid
// minmax(0,1fr)). That is the right model on tall viewports — the inner
// conversation log scrolls — but nothing floors the chat track, so at
// 400%-zoom-equivalent (320x256) and short phone viewports the wrapped
// header plus the stacked side rail can consume the height and collapse the
// conversation and composer to (near) zero with no document scroll path.
//
// These tests assert OUTCOMES, not mechanism: at each reflow viewport the
// conversation log keeps a usable height and the composer textarea remains
// reachable (directly or via a real vertical scroll path).

import { expect, test } from "@playwright/test";

import { ComposerPage } from "./page-objects/composer-page";
import { expectNoDocumentHorizontalOverflow } from "./helpers/workspace-assertions";
import {
  deleteWorkspaceScenario,
  installWorkspaceScenario,
} from "./helpers/workspace-fixtures";

// Minimum usable conversation-log height. ~3 lines of text plus padding —
// below this the transcript is unreadable even if technically visible.
const MIN_LOG_HEIGHT = 120;

// Minimum single-line composer textarea height.
const MIN_INPUT_HEIGHT = 32;

const REFLOW_VIEWPORTS = [
  // WCAG 1.4.10 vertical-content target: 320 CSS px wide; 256px high is the
  // 400%-zoom equivalent of a 1280x1024 desktop.
  { width: 320, height: 256 },
  // Common small phone.
  { width: 375, height: 667 },
] as const;

test.describe("composer reflow geometry (elspeth-7aa9787996)", () => {
  for (const viewport of REFLOW_VIEWPORTS) {
    test(`conversation and composer stay operable at ${viewport.width}x${viewport.height}`, async ({
      page,
    }) => {
      const composer = new ComposerPage(page);
      const sessionId = await installWorkspaceScenario(page, "empty-freeform");
      try {
        // Load a server-created session at desktop size, wait until its
        // identity is committed in the shell, then resize — the realistic
        // order for a user zooming to 400% mid-session. Waiting for the
        // session-labelled switcher prevents late session hydration from
        // replacing the ChatInput while geometry is being measured.
        await composer.goto(sessionId);
        await expect(
          page.getByRole("button", {
            name: "Session switcher: workspace empty-freeform",
          }),
        ).toBeVisible();
        await composer.waitForChatReady();
        await page.setViewportSize(viewport);
        await expect(composer.workspace()).toHaveAttribute(
          "data-layout-mode",
          "narrow",
        );

        const log = page.getByRole("log", { name: "Conversation" });

        // The conversation log must keep a readable height. scrollIntoView
        // first: with a shell-level scroll path the log may legitimately
        // start off-screen below a wrapped header. Poll the boxes — a one-shot
        // boundingBox() can catch a React re-render instant and return null
        // even though the element is (and stays) visible.
        await log.scrollIntoViewIfNeeded();
        await expect(log).toBeVisible();
        await expect
          .poll(
            async () => (await log.boundingBox())?.height ?? 0,
            { message: `conversation log height at ${viewport.width}x${viewport.height}` },
          )
          .toBeGreaterThanOrEqual(MIN_LOG_HEIGHT);

        // The composer textarea must be reachable and usable.
        const textareaBeforeSwitch = page.locator(".chat-input-textarea");
        await expect(textareaBeforeSwitch).toBeVisible();
        await textareaBeforeSwitch.scrollIntoViewIfNeeded();
        await expect
          .poll(
            async () =>
              (await page.locator(".chat-input-textarea").boundingBox())?.height ?? 0,
            { message: `composer textarea height at ${viewport.width}x${viewport.height}` },
          )
          .toBeGreaterThanOrEqual(MIN_INPUT_HEIGHT);

        // And it must actually accept focus + input at this geometry.
        await textareaBeforeSwitch.click();
        await textareaBeforeSwitch.fill("reflow probe");
        await expect(textareaBeforeSwitch).toHaveValue("reflow probe");

        const composeTab = composer.composeViewTab();
        const pipelineTab = composer.pipelineViewTab();
        await expect(composeTab).toBeVisible();
        await expect(pipelineTab).toBeVisible();
        await pipelineTab.click();
        await expect(pipelineTab).toHaveAttribute("aria-selected", "true");
        await expect(composer.separator()).toBeHidden();
        await expectNoDocumentHorizontalOverflow(page);

        await composeTab.click();
        await expect(composeTab).toHaveAttribute("aria-selected", "true");
        const textareaAfterSwitch = page.locator(".chat-input-textarea");
        await expect(textareaAfterSwitch).toBeVisible();
        await expect(textareaAfterSwitch).toHaveValue("reflow probe");
      } finally {
        await deleteWorkspaceScenario(page, sessionId);
      }
    });
  }
});
